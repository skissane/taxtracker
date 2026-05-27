import io
import mimetypes
import re
import zipfile
from urllib.parse import quote

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connections, transaction
from django.forms import BaseInlineFormSet
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from .archives import UnsupportedArchiveError, extract_from_archive
from .forms import AttachmentForm, ImportArchiveForm
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
    """Base formset that enforces exactly one row is marked is_primary=True.

    Rules:
    - If _nonempty_error is set and there are no non-deleted rows, raise it.
    - If exactly one non-deleted row exists and none is marked primary,
      automatically mark it as primary (and persist the change).
    - If multiple rows exist and none is marked primary, raise ValidationError.
    - If multiple rows are marked primary, raise ValidationError (prevents
      IntegrityError from the unique DB constraint).
    """

    _primary_label = "item"
    _nonempty_error = None  # if set, raised when there are no non-deleted rows
    _auto_primary_form = None  # set in clean() when auto-setting a single row

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        non_deleted_forms = [
            f
            for f in self.forms
            if f.cleaned_data and not f.cleaned_data.get("DELETE", False)
        ]
        if not non_deleted_forms:
            if self._nonempty_error:
                raise ValidationError(self._nonempty_error)
            return
        primary_forms = [
            f for f in non_deleted_forms if f.cleaned_data.get("is_primary")
        ]
        if len(primary_forms) > 1:
            raise ValidationError(
                f"Only one {self._primary_label} may be marked as primary."
            )
        if not primary_forms:
            if len(non_deleted_forms) == 1:
                # Exactly one row — auto-set it as primary.
                # We update both cleaned_data (used if the form itself is saved
                # via form.save()) and instance.is_primary (used as a fallback for
                # unchanged existing rows that form.save() may skip).
                non_deleted_forms[0].cleaned_data["is_primary"] = True
                non_deleted_forms[0].instance.is_primary = True
                self._auto_primary_form = non_deleted_forms[0]
            else:
                raise ValidationError(
                    f"At least one {self._primary_label} must be marked as primary."
                )

    def save(self, commit=True):
        instances = super().save(commit=commit)
        # For the auto-set-primary case, ensure the DB is updated even when the
        # form was not otherwise "changed" (e.g. an existing row already in the DB
        # whose is_primary was False and the user didn't explicitly tick the box).
        if commit and self._auto_primary_form is not None:
            inst = self._auto_primary_form.instance
            # clean() sets inst.is_primary = True in memory, so that value cannot
            # be used to decide whether persistence is needed. For existing rows,
            # unconditionally issue the UPDATE so unchanged forms are persisted.
            # New rows are already saved by super().save() with is_primary=True.
            if inst.pk:
                self.model.objects.filter(pk=inst.pk).update(is_primary=True)
            inst.is_primary = True
        return instances


class MimeTypeFormSet(_AtLeastOnePrimaryFormSet):
    _primary_label = "MIME type"
    _nonempty_error = "A file type must have at least one MIME type."


class FileExtensionFormSet(_AtLeastOnePrimaryFormSet):
    _primary_label = "file extension"
    _nonempty_error = "A file type must have at least one file extension."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attachment_date_warning(obj):
    """Return an HTML warning span if *obj*'s date falls outside its financial year."""
    if not obj.pk or not obj.date or not obj.item_id:
        return ""
    fy = obj.item.year
    if obj.date < fy.start_date or obj.date > fy.end_date:
        return format_html(
            '<span title="Date is outside the financial year ({} \u2013 {})"'
            ">\u26a0\ufe0f</span>",
            fy.start_date,
            fy.end_date,
        )
    return ""


# ---------------------------------------------------------------------------
# Inlines
# ---------------------------------------------------------------------------


class AttachmentInline(admin.TabularInline):
    model = Attachment
    form = AttachmentForm
    extra = 1
    fields = ("title", "date", "date_warning", "file_type", "file", "change_link")
    readonly_fields = ("date_warning", "change_link")
    autocomplete_fields = ("file_type",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("item__year")

    @admin.display(description="")
    def date_warning(self, obj):
        return _attachment_date_warning(obj)

    @admin.display(description="Edit")
    def change_link(self, obj):
        if not obj.pk:
            return "—"
        url = reverse("admin:tracker_attachment_change", args=[obj.pk])
        return format_html('<a href="{}">Edit</a>', url)


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

    # ------------------------------------------------------------------
    # Custom URLs
    # ------------------------------------------------------------------

    def get_urls(self):
        custom = [
            path(
                "<int:pk>/import-archive/",
                self.admin_site.admin_view(self.import_archive_view),
                name="tracker_item_import_archive",
            ),
        ]
        return custom + super().get_urls()

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

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        extra_context["import_archive_url"] = reverse(
            "admin:tracker_item_import_archive", args=[object_id]
        )
        return super().change_view(request, object_id, form_url, extra_context)

    # ------------------------------------------------------------------
    # Import archive view
    # ------------------------------------------------------------------

    def import_archive_view(self, request, pk):
        item = get_object_or_404(Item, pk=pk)

        # Check that the requesting user can change this Item and can add
        # new Attachments — both are required for this view to function.
        if not self.has_change_permission(request, item):
            raise PermissionDenied
        attachment_admin = self.admin_site._registry.get(Attachment)
        if attachment_admin is None or not attachment_admin.has_add_permission(request):
            raise PermissionDenied

        if request.method == "POST":
            form = ImportArchiveForm(request.POST, request.FILES)
        else:
            form = ImportArchiveForm()

        if request.method == "POST" and form.is_valid():
            uploaded = form.cleaned_data["archive"]
            file_bytes = uploaded.read()
            filename = uploaded.name

            try:
                extracted = extract_from_archive(file_bytes, filename)
            except UnsupportedArchiveError as exc:
                messages.error(request, str(exc))
                extracted = []

            if extracted:
                with transaction.atomic():
                    for pdf_filename, pdf_bytes in extracted:
                        att = Attachment(item=item)
                        # save=False: store the file in the storage backend now
                        # but don't yet write the Attachment row; att.save()
                        # below will auto-extract the title, date, and file_type
                        # from the filename before persisting to the DB.
                        att.file.save(
                            pdf_filename,
                            io.BytesIO(pdf_bytes),
                            save=False,
                        )
                        att.save()
                messages.success(
                    request,
                    f"Imported {len(extracted)} attachment(s) from '{filename}'.",
                )
            elif not messages.get_messages(request):
                messages.warning(
                    request,
                    f"No attachments could be extracted from '{filename}'.",
                )

            return redirect(reverse("admin:tracker_item_change", args=[item.pk]))

        context = {
            **self.admin_site.each_context(request),
            "title": f"Import Archive — {item}",
            "item": item,
            "form": form,
            "opts": self.model._meta,
        }
        return render(request, "admin/tracker/item/import_archive.html", context)


# ---------------------------------------------------------------------------
# Attachment Admin
# ---------------------------------------------------------------------------


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    form = AttachmentForm
    list_display = ("title", "date", "date_warning", "item", "file_type", "file")
    list_filter = ("item__year", "file_type")
    search_fields = ("title", "notes", "item__title", "file_type__short_name")
    list_select_related = ("item", "item__year", "file_type")
    autocomplete_fields = ("file_type",)
    readonly_fields = ("date_warning",)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "item",
                    "title",
                    ("date", "date_warning"),
                    "notes",
                    "file_type",
                    "file",
                ),
            },
        ),
    )

    @admin.display(description="")
    def date_warning(self, obj):
        return _attachment_date_warning(obj)

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
        filename_encoded = quote(obj.filename, safe="")
        filename_ascii = (
            obj.filename.encode("ascii", errors="ignore")
            .decode("ascii")
            .replace("\\", "\\\\")
            .replace('"', '\\"')
        )
        cd = (
            f'inline; filename="{filename_ascii}";'
            f" filename*=UTF-8''{filename_encoded}"
        )
        response["Content-Disposition"] = cd
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

    def safe_component(title):
        """Sanitize a title for safe use as a ZIP path component.

        Prevents Zip Slip by replacing path separators and eliminating ``..``
        traversal sequences.
        """
        safe = title.replace("/", "_").replace("\\", "_")
        safe = safe.strip(". ")
        safe = re.sub(r"\.{2,}", ".", safe)
        return safe or "_"

    def folder_path(item):
        """Return a POSIX path string representing the item hierarchy."""
        parts = []
        current = item
        while True:
            parts.insert(0, safe_component(current.title))
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
            attachments = list(item.attachments.all())
            if not attachments:
                continue
            fp = folder_path(item)
            index_lines.append(f"## {fp}\n")
            if item.notes:
                index_lines.append(f"{item.notes}\n\n")
            for attachment in attachments:
                # Sanitise filename.
                safe_name = attachment.file.name.split("/")[-1]
                zip_path = f"{fp}/{safe_name}"
                try:
                    with attachment.file.open("rb") as fh:
                        zf.writestr(zip_path, fh.read())
                except OSError, DBStoredFile.DoesNotExist, ValueError:
                    # File missing or stored blob reference is invalid/corrupt –
                    # skip but record in index.
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
        # Fetch all items in a single query to avoid N+1 for deep hierarchies.
        all_items = list(
            fy.items.prefetch_related("attachments").order_by("order", "title")
        )

        # Build a parent_id → [children] map (order is preserved from queryset).
        children_map = {}
        for item in all_items:
            children_map.setdefault(item.parent_id, []).append(item)

        def item_tree(item, depth=0):
            rows = [{"item": item, "depth": depth}]
            for child in children_map.get(item.pk, []):
                rows.extend(item_tree(child, depth + 1))
            return rows

        tree_rows = []
        for root_item in children_map.get(None, []):
            tree_rows.extend(item_tree(root_item))

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
        response = HttpResponse(buf.getvalue(), content_type="application/zip")
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
