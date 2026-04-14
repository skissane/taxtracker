import io
import mimetypes
import zipfile

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connections, transaction
from django.forms import BaseInlineFormSet
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from .models import (
    Attachment,
    DBStoredFile,
    FileExtension,
    FileType,
    FinancialYear,
    Item,
    MimeType,
)

# ---------------------------------------------------------------------------
# Formsets — "at least one primary" validation
# ---------------------------------------------------------------------------


class _AtLeastOnePrimaryFormSet(BaseInlineFormSet):
    """Base formset that enforces at least one row is marked is_primary=True."""

    _primary_label = "item"

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        has_primary = False
        has_non_deleted = False
        for form in self.forms:
            if not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE", False):
                continue
            has_non_deleted = True
            if form.cleaned_data.get("is_primary"):
                has_primary = True
        if has_non_deleted and not has_primary:
            raise ValidationError(
                f"At least one {self._primary_label} must be marked as primary."
            )


class MimeTypeFormSet(_AtLeastOnePrimaryFormSet):
    _primary_label = "MIME type"


class FileExtensionFormSet(_AtLeastOnePrimaryFormSet):
    _primary_label = "file extension"


# ---------------------------------------------------------------------------
# Inlines
# ---------------------------------------------------------------------------


class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 1
    fields = ("title", "notes", "file_type", "file")
    autocomplete_fields = ("file_type",)


class ChildItemInline(admin.TabularInline):
    model = Item
    fk_name = "parent"
    extra = 1
    fields = ("order", "title", "status", "notes")
    verbose_name = "Sub-item"
    verbose_name_plural = "Sub-items"
    show_change_link = True


# ---------------------------------------------------------------------------
# Item Admin
# ---------------------------------------------------------------------------


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("title", "year_link", "parent", "status", "order")
    list_filter = ("year", "status", "parent")
    search_fields = ("title", "notes")
    list_select_related = ("year", "parent")
    inlines = [ChildItemInline, AttachmentInline]
    fields = ("year", "parent", "order", "title", "status", "notes")

    @admin.display(description="Year")
    def year_link(self, obj):
        url = reverse("admin:tracker_financialyear_change", args=[obj.year_id])
        return format_html('<a href="{}">{}</a>', url, obj.year)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("year", "parent")

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))
        # Child items cannot have a different year from their parent — hide the field.
        if obj and obj.parent_id:
            fields = [f for f in fields if f != "year"]
        return fields

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and "year" in form.changed_data and obj.parent_id is None:
            # BFS to collect all descendant IDs (one query per depth level),
            # then a single bulk update — avoids N individual saves.
            ids_to_update = []
            current_level = [obj.pk]
            while current_level:
                children = list(
                    Item.objects.filter(parent_id__in=current_level).values_list(
                        "pk", flat=True
                    )
                )
                ids_to_update.extend(children)
                current_level = children
            if ids_to_update:
                Item.objects.filter(pk__in=ids_to_update).update(year_id=obj.year_id)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        # Batch-fetch year_id for all parent IDs to avoid N+1 queries.
        parent_ids = {
            i.parent_id for i in instances if isinstance(i, Item) and i.parent_id
        }
        year_by_parent_id = {}
        if parent_ids:
            year_by_parent_id = dict(
                Item.objects.filter(pk__in=parent_ids).values_list("pk", "year_id")
            )
        for instance in instances:
            # Auto-inherit year from parent for inline-created child items.
            if isinstance(instance, Item) and instance.parent_id:
                instance.year_id = year_by_parent_id.get(
                    instance.parent_id, instance.parent.year_id
                )
            instance.save()
        formset.save_m2m()
        for obj in formset.deleted_objects:
            obj.delete()


# ---------------------------------------------------------------------------
# Attachment Admin
# ---------------------------------------------------------------------------


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("title", "item", "file_type", "file")
    list_filter = ("item__year", "file_type")
    search_fields = ("title", "notes", "item__title", "file_type__short_name")
    list_select_related = ("item", "item__year", "file_type")
    autocomplete_fields = ("file_type",)

    def get_urls(self):
        custom = [
            path(
                "file/<int:pk>/",
                self.admin_site.admin_view(self.serve_file_view),
                name="tracker_attachment_serve_file",
            ),
        ]
        return custom + super().get_urls()

    def serve_file_view(self, request, pk):
        obj = get_object_or_404(DBStoredFile, pk=pk)
        content = bytes(obj.content)
        content_type, _ = mimetypes.guess_type(obj.filename)
        if not content_type:
            content_type = "application/octet-stream"
        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'inline; filename="{obj.filename}"'
        return response


# ---------------------------------------------------------------------------
# FinancialYear Admin
# ---------------------------------------------------------------------------


def _build_zip(fy):
    """Build a ZIP file for all attachments of a FinancialYear.

    The ZIP contains:
    - A folder hierarchy mirroring the item hierarchy.
    - An ``index.md`` at the root summarising everything.
    """
    buf = io.BytesIO()

    # Pre-fetch all items and attachments for this year.
    items = list(
        fy.items.select_related("parent")
        .prefetch_related("attachments")
        .order_by("order", "title")
    )

    # Build an id → item dict for quick lookup.
    item_map = {item.pk: item for item in items}

    def folder_path(item):
        """Return a POSIX path string representing the item hierarchy."""
        parts = []
        current = item
        while True:
            parts.insert(0, current.title)
            if current.parent_id is None:
                break
            current = item_map.get(current.parent_id, current.parent)
        return "/".join(parts)

    index_lines = [
        f"# {fy} – Attachment Index\n",
        f"**Period:** {fy.start_date} to {fy.end_date}\n",
        f"**Lodgement date:** {fy.effective_lodgement_date}"
        + (" (default)" if fy.lodgement_date_override is None else " (agent override)")
        + "\n\n",
    ]

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            if not item.attachments.exists():
                continue
            fp = folder_path(item)
            index_lines.append(f"## {fp}\n")
            if item.notes:
                index_lines.append(f"{item.notes}\n\n")
            for attachment in item.attachments.all():
                # Sanitise filename.
                safe_name = attachment.file.name.split("/")[-1]
                zip_path = f"{fp}/{safe_name}"
                try:
                    with attachment.file.open("rb") as fh:
                        zf.writestr(zip_path, fh.read())
                except OSError:
                    # File missing on disk – skip but record in index.
                    safe_name = f"[MISSING] {safe_name}"
                    zip_path = None

                index_lines.append(f"- **{attachment.title}**")
                if attachment.file_type:
                    index_lines[-1] += f" ({attachment.file_type})"
                if zip_path:
                    index_lines[-1] += f" → `{zip_path}`"
                index_lines[-1] += "\n"
                if attachment.notes:
                    index_lines.append(f"  {attachment.notes}\n")
            index_lines.append("\n")

        zf.writestr("index.md", "".join(index_lines))

    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# FileType Admin
# ---------------------------------------------------------------------------


class MimeTypeInline(admin.TabularInline):
    model = MimeType
    extra = 1
    fields = ("mime_type", "is_primary")
    formset = MimeTypeFormSet


class FileExtensionInline(admin.TabularInline):
    model = FileExtension
    extra = 1
    fields = ("extension", "is_primary")
    formset = FileExtensionFormSet


@admin.register(FileType)
class FileTypeAdmin(admin.ModelAdmin):
    list_display = ("short_name", "full_name", "primary_mime_type", "primary_extension")
    search_fields = ("short_name", "full_name")
    inlines = [MimeTypeInline, FileExtensionInline]

    @admin.display(description="Primary MIME Type")
    def primary_mime_type(self, obj):
        mt = obj.mime_types.filter(is_primary=True).first()
        return mt.mime_type if mt else "—"

    @admin.display(description="Primary Extension")
    def primary_extension(self, obj):
        ext = obj.file_extensions.filter(is_primary=True).first()
        return ext.extension if ext else "—"


@admin.register(MimeType)
class MimeTypeAdmin(admin.ModelAdmin):
    list_display = ("mime_type", "file_type", "is_primary")
    list_filter = ("file_type", "is_primary")
    search_fields = ("mime_type",)
    list_select_related = ("file_type",)


@admin.register(FileExtension)
class FileExtensionAdmin(admin.ModelAdmin):
    list_display = ("extension", "file_type", "is_primary")
    list_filter = ("file_type", "is_primary")
    search_fields = ("extension",)
    list_select_related = ("file_type",)


@admin.register(FinancialYear)
class FinancialYearAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "period",
        "lodgement_date_display",
        "days_until_lodgement_display",
        "summary_link",
        "download_zip_link",
    )
    fields = ("year", "notes", "lodgement_date_override")
    search_fields = ("year",)

    # ------------------------------------------------------------------
    # Custom URLs
    # ------------------------------------------------------------------

    def get_urls(self):
        custom = [
            path(
                "<int:pk>/summary/",
                self.admin_site.admin_view(self.summary_view),
                name="tracker_financialyear_summary",
            ),
            path(
                "<int:pk>/download-zip/",
                self.admin_site.admin_view(self.download_zip_view),
                name="tracker_financialyear_download_zip",
            ),
            path(
                "<int:pk>/copy-to-new-year/",
                self.admin_site.admin_view(self.copy_to_new_year_view),
                name="tracker_financialyear_copy_to_new_year",
            ),
            path(
                "download-db-backup/",
                self.admin_site.admin_view(self.download_db_backup_view),
                name="tracker_financialyear_download_db_backup",
            ),
        ]
        return custom + super().get_urls()

    # ------------------------------------------------------------------
    # List display helpers
    # ------------------------------------------------------------------

    @admin.display(description="Financial Year")
    def name(self, obj):
        return str(obj)

    @admin.display(description="Period")
    def period(self, obj):
        return f"{obj.start_date} – {obj.end_date}"

    @admin.display(description="Lodgement Date")
    def lodgement_date_display(self, obj):
        suffix = "" if obj.lodgement_date_override is None else " ★"
        return f"{obj.effective_lodgement_date}{suffix}"

    @admin.display(description="Days Until Lodgement")
    def days_until_lodgement_display(self, obj):
        d = obj.days_until_lodgement
        if d > 0:
            return f"{d} days"
        elif d == 0:
            return "Today!"
        else:
            return f"{abs(d)} days ago"

    @admin.display(description="Summary")
    def summary_link(self, obj):
        url = reverse("admin:tracker_financialyear_summary", args=[obj.pk])
        return format_html('<a href="{}">View Summary</a>', url)

    @admin.display(description="Download")
    def download_zip_link(self, obj):
        url = reverse("admin:tracker_financialyear_download_zip", args=[obj.pk])
        return format_html('<a href="{}">Download ZIP</a>', url)

    # ------------------------------------------------------------------
    # Changelist view override (inject backup URL)
    # ------------------------------------------------------------------

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["download_db_backup_url"] = reverse(
            "admin:tracker_financialyear_download_db_backup"
        )
        return super().changelist_view(request, extra_context)

    # ------------------------------------------------------------------
    # Summary view
    # ------------------------------------------------------------------

    def summary_view(self, request, pk):
        fy = get_object_or_404(FinancialYear, pk=pk)
        items = (
            fy.items.filter(parent=None)
            .prefetch_related("children", "children__children", "attachments")
            .order_by("order", "title")
        )

        def item_tree(item, depth=0):
            rows = [{"item": item, "depth": depth}]
            for child in item.children.order_by("order", "title"):
                rows.extend(item_tree(child, depth + 1))
            return rows

        tree_rows = []
        for root_item in items:
            tree_rows.extend(item_tree(root_item))

        all_items = list(fy.items.all())
        total = len(all_items)
        done = sum(1 for i in all_items if i.is_done)
        pending = total - done

        context = {
            **self.admin_site.each_context(request),
            "title": f"Status Summary – {fy}",
            "fy": fy,
            "tree_rows": tree_rows,
            "total": total,
            "done": done,
            "pending": pending,
            "opts": self.model._meta,
        }
        return render(request, "admin/tracker/financialyear/summary.html", context)

    # ------------------------------------------------------------------
    # ZIP download view
    # ------------------------------------------------------------------

    def download_zip_view(self, request, pk):
        fy = get_object_or_404(FinancialYear, pk=pk)
        buf = _build_zip(fy)
        response = HttpResponse(buf, content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{fy}_attachments.zip"'
        return response

    # ------------------------------------------------------------------
    # DB backup download view
    # ------------------------------------------------------------------

    def download_db_backup_view(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied

        engine = settings.DATABASES["default"]["ENGINE"]
        if engine != "django.db.backends.sqlite3":
            messages.error(request, "DB backup is only available for SQLite databases.")
            return redirect(reverse("admin:tracker_financialyear_changelist"))

        with connections["default"].cursor() as cursor:
            backup_bytes = cursor.connection.serialize()

        response = HttpResponse(backup_bytes, content_type="application/x-sqlite3")
        response["Content-Disposition"] = 'attachment; filename="db-backup.sqlite3"'
        return response

    # ------------------------------------------------------------------
    # Copy to new year view
    # ------------------------------------------------------------------

    def copy_to_new_year_view(self, request, pk):
        fy = get_object_or_404(FinancialYear, pk=pk)
        new_year_num = fy.year + 1

        if FinancialYear.objects.filter(year=new_year_num).exists():
            messages.error(
                request,
                f"FY{new_year_num} already exists.",
            )
            return redirect(reverse("admin:tracker_financialyear_changelist"))

        if request.method == "POST":
            with transaction.atomic():
                new_fy = FinancialYear.objects.create(year=new_year_num)
                _copy_items(fy, new_fy)
            messages.success(
                request,
                f"Created FY{new_year_num} with items copied from {fy}.",
            )
            return redirect(
                reverse("admin:tracker_financialyear_change", args=[new_fy.pk])
            )

        context = {
            **self.admin_site.each_context(request),
            "title": f"Copy {fy} to FY{new_year_num}",
            "fy": fy,
            "new_year_num": new_year_num,
            "opts": self.model._meta,
        }
        return render(
            request,
            "admin/tracker/financialyear/copy_to_new_year.html",
            context,
        )

    # ------------------------------------------------------------------
    # Change-object buttons
    # ------------------------------------------------------------------

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        extra_context["summary_url"] = reverse(
            "admin:tracker_financialyear_summary", args=[object_id]
        )
        extra_context["download_zip_url"] = reverse(
            "admin:tracker_financialyear_download_zip", args=[object_id]
        )
        extra_context["copy_to_new_year_url"] = reverse(
            "admin:tracker_financialyear_copy_to_new_year", args=[object_id]
        )
        return super().change_view(request, object_id, form_url, extra_context)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _copy_items(source_fy, dest_fy):
    """Copy the item hierarchy from *source_fy* to *dest_fy*.

    Notes, status and attachments are NOT copied.
    """
    old_to_new = {}

    # Copy root items first, then children in order so parents always exist.
    def copy_item(item, new_parent=None):
        new_item = Item.objects.create(
            year=dest_fy,
            parent=new_parent,
            title=item.title,
            order=item.order,
        )
        old_to_new[item.pk] = new_item
        for child in item.children.order_by("order", "title"):
            copy_item(child, new_parent=new_item)

    for root in source_fy.items.filter(parent=None).order_by("order", "title"):
        copy_item(root)
