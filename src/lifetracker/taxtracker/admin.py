import dataclasses
import datetime
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import IO, Any

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.http import FileResponse, HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.utils.html import format_html

from lifetracker.core.archives import select_importable_files
from lifetracker.core.attachments import extract_date_from_filename
from lifetracker.taxtracker.archives import (
    UnsupportedArchiveError,
    extract_from_archive_with_skips,
)
from lifetracker.taxtracker.forms import (
    AttachmentForm,
    ImportArchiveForm,
    ReceivedDocumentImportArchiveForm,
)
from lifetracker.taxtracker.models import (
    Attachment,
    FinancialYear,
    FinancialYearStatusHistory,
    Item,
    ReceivedDocument,
)
from lifetracker.taxtracker.reassign import (
    apply_reassignments,
    find_equivalent_item,
    item_path_signature,
    plan_reassignments,
)
from lifetracker.taxtracker.zip_export import (
    build_multi_index_md,
    build_multi_zip,
    build_zip,
    tree_rows_and_unreachable_items,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attachment_date_warning(obj: Attachment) -> str:
    """Return an HTML warning span if *obj*'s date falls outside its financial year."""
    if not obj.pk or not obj.date:
        return ""
    if obj.item is not None:
        fy = obj.item.year
    elif obj.received_document is not None:
        fy = obj.received_document.year
    else:
        return ""
    if obj.date < fy.start_date or obj.date > fy.end_date:
        return format_html(
            '<span title="Date is outside the financial year ({} – {})"'
            ' role="img" aria-label="Date is outside the financial year">'
            "⚠️</span>",
            fy.start_date,
            fy.end_date,
        )
    return ""


# Matches a leading 4-digit financial year in a filename, e.g. "2024-notice.pdf".
# The negative lookahead excludes longer digit runs like "20245-notice.pdf".
_RECEIVED_DOC_YEAR_RE = re.compile(r"^(\d{4})(?!\d)")


def _posted_pks(request: HttpRequest, name: str) -> set[int]:
    """The primary keys posted as *name*, ignoring any that aren't one."""
    pks = set()
    for value in request.POST.getlist(name):
        try:
            pk = int(value)
        except ValueError:
            continue
        if 0 < pk < 2**63:  # SQLite raises OverflowError for anything over 64 bits
            pks.add(pk)
    return pks


def _existing_attachment_names(**filters: Any) -> set[str]:
    """Return the basename of every stored file name matching *filters*."""
    return {
        Path(file_name).name
        for file_name in Attachment.objects.filter(**filters).values_list("file", flat=True)
        if file_name
    }


def _save_imported_attachment(attachment: Attachment, filename: str, file_bytes: bytes) -> None:
    """Store *file_bytes* as *attachment*'s file under *filename*, then save the row."""
    # save=False: store the file in the storage backend now but don't yet
    # write the Attachment row; attachment.save() below will auto-extract
    # the title, date, and file_type from the filename before persisting.
    attachment.file.save(filename, ContentFile(file_bytes), save=False)
    attachment.save()


def _extract_archive(
    request: HttpRequest, file_bytes: bytes, filename: str
) -> tuple[list[tuple[str, bytes]], list[str], bool]:
    """Extract an uploaded archive, reporting an unsupported one as an error message.

    Returns ``(extracted, archive_skipped_names, had_error)``.
    """
    try:
        extracted, archive_skipped_names = extract_from_archive_with_skips(file_bytes, filename)
    except UnsupportedArchiveError as exc:
        messages.error(request, str(exc))
        return [], [], True
    return extracted, archive_skipped_names, False


def _warn_skipped(request: HttpRequest, names: Iterable[str], reason: str) -> None:
    """Add a warning listing the distinct *names* skipped for *reason*, if there are any."""
    if names:
        unique_names = list(dict.fromkeys(names))
        messages.warning(request, f"Skipped {len(unique_names)} {reason}: {', '.join(unique_names)}.")


def _import_into_item(item: Item, extracted: list[tuple[str, bytes]]) -> tuple[int, list[str]]:
    """Attach each of an archive's *extracted* PDFs to *item*, in one transaction.

    Skips names already attached to it; returns how many were imported, and the skipped names.
    """
    duplicate_names: list[str] = []
    if not extracted:
        return 0, duplicate_names
    importable = select_importable_files(extracted, _existing_attachment_names(item=item), duplicate_names)
    with transaction.atomic():
        for pdf_filename, pdf_bytes in importable:
            _save_imported_attachment(Attachment(item=item), pdf_filename, pdf_bytes)
    return len(importable), duplicate_names


@dataclasses.dataclass
class _ReceivedDocumentImport:
    """What ``_import_received_documents`` did with an archive's PDFs."""

    created_count: int = 0
    no_year_names: list[str] = dataclasses.field(default_factory=list)
    missing_fy_names: list[str] = dataclasses.field(default_factory=list)
    duplicate_names: list[str] = dataclasses.field(default_factory=list)


def _import_received_documents(extracted: Sequence[tuple[str, bytes]]) -> _ReceivedDocumentImport:
    """Create a ``ReceivedDocument``, with the PDF attached, for each of *extracted*'s ``(filename, pdf_bytes)``.

    Each goes to the Financial Year its filename's leading 4-digit year names, unless a received document of that
    FY already has an attachment of the same name; all in one transaction.
    """
    result = _ReceivedDocumentImport()
    if not extracted:
        return result
    fy_cache: dict[int, FinancialYear | None] = {}
    existing_names_by_year: dict[int, set[str]] = {}

    with transaction.atomic():
        for pdf_filename, pdf_bytes in extracted:
            base_name = Path(pdf_filename).name
            match = _RECEIVED_DOC_YEAR_RE.match(base_name)
            if not match:
                result.no_year_names.append(base_name)
                continue

            year_num = int(match.group(1))
            if year_num not in fy_cache:
                fy_cache[year_num] = FinancialYear.objects.filter(year=year_num).first()
            fy = fy_cache[year_num]
            if fy is None:
                result.missing_fy_names.append(f"{base_name} (FY{year_num})")
                continue
            if year_num not in existing_names_by_year:
                existing_names_by_year[year_num] = _existing_attachment_names(received_document__year=fy)
            existing_names = existing_names_by_year[year_num]
            if base_name in existing_names:
                result.duplicate_names.append(base_name)
                continue

            doc = ReceivedDocument.objects.create(
                year=fy,
                title=base_name,
                date=extract_date_from_filename(base_name),
            )
            _save_imported_attachment(Attachment(received_document=doc), base_name, pdf_bytes)
            existing_names.add(base_name)
            result.created_count += 1
    return result


# ---------------------------------------------------------------------------
# Inlines
# ---------------------------------------------------------------------------


class AttachmentInlineBase[P: Item | ReceivedDocument](admin.TabularInline[Attachment, P]):
    """Shared attachment inline; subclasses set ``select_related_path`` (and ``fk_name`` if needed)."""

    model = Attachment
    form = AttachmentForm
    extra = 1
    fields = ("title", "date", "date_warning", "file_type", "file", "change_link")
    readonly_fields = ("date_warning", "change_link")
    autocomplete_fields = ("file_type",)
    select_related_path: str

    def get_queryset(self, request: HttpRequest) -> QuerySet[Attachment]:
        return super().get_queryset(request).select_related(self.select_related_path)

    @admin.display(description="")
    def date_warning(self, obj: Attachment) -> str:
        return _attachment_date_warning(obj)

    @admin.display(description="Edit")
    def change_link(self, obj: Attachment) -> str:
        if not obj.pk:
            return "—"
        url = reverse("admin:taxtracker_attachment_change", args=[obj.pk])
        return format_html('<a href="{}">Edit</a>', url)


class AttachmentInline(AttachmentInlineBase[Item]):
    select_related_path = "item__year"


class ReceivedAttachmentInline(AttachmentInlineBase[ReceivedDocument]):
    fk_name = "received_document"
    select_related_path = "received_document__year"


class ChildItemInline(admin.TabularInline[Item, Item]):
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
class ItemAdmin(admin.ModelAdmin[Item]):
    list_display = ("title", "year_link", "parent", "status", "order")
    list_filter = ("year", "status", "parent")
    search_fields = ("title", "notes")
    list_select_related = ("year", "parent")
    inlines = [ChildItemInline, AttachmentInline]
    fields = ("year", "parent", "order", "title", "status", "notes")

    # ------------------------------------------------------------------
    # Custom URLs
    # ------------------------------------------------------------------

    def get_urls(self) -> list[URLPattern]:
        custom = [
            path(
                "<int:pk>/import-archive/",
                self.admin_site.admin_view(self.import_archive_view),
                name="taxtracker_item_import_archive",
            ),
            path(
                "<int:pk>/reassign-attachments/",
                self.admin_site.admin_view(self.reassign_attachments_view),
                name="taxtracker_item_reassign_attachments",
            ),
        ]
        return custom + super().get_urls()

    @admin.display(description="Year")
    def year_link(self, obj: Item) -> str:
        url = reverse("admin:taxtracker_financialyear_change", args=[obj.year_id])
        return format_html('<a href="{}">{}</a>', url, obj.year)

    def get_queryset(self, request: HttpRequest) -> QuerySet[Item]:
        return super().get_queryset(request).select_related("year", "parent")

    def get_fields(self, request: HttpRequest, obj: Any = None) -> list[str | list[str] | tuple[str, ...] | tuple[()]]:
        fields = list(super().get_fields(request, obj))
        # Child items cannot have a different year from their parent — hide the field.
        if obj and obj.parent_id:
            fields = [f for f in fields if f != "year"]
        return fields

    def save_formset(self, request: HttpRequest, form: Any, formset: Any, change: Any) -> None:
        instances = formset.save(commit=False)
        # Batch-fetch year_id for all parent IDs to avoid N+1 queries.
        parent_ids = {i.parent_id for i in instances if isinstance(i, Item) and i.parent_id}
        year_by_parent_id = {}
        if parent_ids:
            year_by_parent_id = dict(Item.objects.filter(pk__in=parent_ids).values_list("pk", "year_id"))
        for instance in instances:
            # Auto-inherit year from parent for inline-created child items.
            if isinstance(instance, Item) and instance.parent_id:
                assert instance.parent is not None  # parent_id is set
                instance.year_id = year_by_parent_id.get(instance.parent_id, instance.parent.year_id)
            instance.save()
        formset.save_m2m()
        for obj in formset.deleted_objects:
            obj.delete()

    def change_view(
        self, request: HttpRequest, object_id: Any, form_url: Any = "", extra_context: Any = None
    ) -> HttpResponse:
        response = super().change_view(request, object_id, form_url, extra_context)
        if not isinstance(response, TemplateResponse) or response.context_data is None:
            return response

        item = response.context_data.get("original")
        if item is None:
            return response

        path_signature = item_path_signature(item)
        prev_fy = FinancialYear.objects.filter(year=item.year.year - 1).first()
        next_fy = FinancialYear.objects.filter(year=item.year.year + 1).first()

        response.context_data["import_archive_url"] = reverse("admin:taxtracker_item_import_archive", args=[item.pk])
        response.context_data["reassign_attachments_url"] = reverse(
            "admin:taxtracker_item_reassign_attachments", args=[item.pk]
        )
        response.context_data["prev_item"] = find_equivalent_item(path_signature, prev_fy) if prev_fy else None
        response.context_data["next_item"] = find_equivalent_item(path_signature, next_fy) if next_fy else None
        return response

    # ------------------------------------------------------------------
    # Import archive view
    # ------------------------------------------------------------------

    def import_archive_view(self, request: HttpRequest, pk: Any) -> HttpResponse:
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

            extracted, archive_skipped_names, had_error = _extract_archive(request, file_bytes, filename)

            imported_count, duplicate_names = _import_into_item(item, extracted)
            if imported_count:
                messages.success(
                    request,
                    f"Imported {imported_count} attachment(s) from '{filename}'.",
                )

            _warn_skipped(request, archive_skipped_names, "non-PDF, too large or unreadable file(s)")
            _warn_skipped(request, duplicate_names, "file(s) already attached to this item")
            if not had_error and not extracted and not archive_skipped_names:
                messages.warning(
                    request,
                    f"No attachments could be extracted from '{filename}'.",
                )

            return redirect(reverse("admin:taxtracker_item_change", args=[item.pk]))

        context = {
            **self.admin_site.each_context(request),
            "title": f"Import Archive — {item}",
            "item": item,
            "form": form,
            "opts": self.model._meta,
        }
        return render(request, "admin/taxtracker/item/import_archive.html", context)

    # ------------------------------------------------------------------
    # Reassign out-of-year attachments view
    # ------------------------------------------------------------------

    def reassign_attachments_view(self, request: HttpRequest, pk: Any) -> HttpResponse:
        item = get_object_or_404(Item.objects.select_related("year", "parent"), pk=pk)

        if not self.has_change_permission(request, item):
            raise PermissionDenied
        attachment_admin = self.admin_site._registry.get(Attachment)
        if attachment_admin is None or not attachment_admin.has_change_permission(request):
            raise PermissionDenied

        attachments = list(item.attachments.all().order_by("date", "title", "pk"))
        rows = plan_reassignments(item, attachments)
        movable_count = sum(1 for row in rows if row.movable)
        initial_summary = {
            "total_attachments": len(attachments),
            "correct_item_count": len(attachments) - len(rows),
            "unmovable_count": len(rows) - movable_count,
            "movable_count": movable_count,
        }

        result_rows = []
        result_summary = None
        if request.method == "POST":
            result_rows = apply_reassignments(rows, _posted_pks(request, "move_attachment_ids"))
            statuses = [row.status for row in result_rows]
            result_summary = {
                "total_out_of_year": len(rows),
                "moved_count": statuses.count("Moved"),
                "not_selected_count": statuses.count("Not selected"),
                "unmovable_count": statuses.count("Unmovable"),
            }

        context = {
            **self.admin_site.each_context(request),
            "title": f"Reassign Attachments — {item}",
            "item": item,
            "rows": rows,
            "initial_summary": initial_summary,
            "result_rows": result_rows,
            "result_summary": result_summary,
            "opts": self.model._meta,
        }
        return render(request, "admin/taxtracker/item/reassign_attachments.html", context)


# ---------------------------------------------------------------------------
# Attachment Admin
# ---------------------------------------------------------------------------


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin[Attachment]):
    form = AttachmentForm
    list_display = (
        "title",
        "date",
        "date_warning",
        "item",
        "received_document",
        "file_type",
        "file",
    )
    list_filter = ("item__year", "received_document__year", "file_type")
    search_fields = (
        "title",
        "notes",
        "item__title",
        "received_document__title",
        "file_type__short_name",
    )
    list_select_related = (
        "item",
        "item__year",
        "received_document",
        "received_document__year",
        "file_type",
    )
    autocomplete_fields = ("file_type",)
    readonly_fields = ("date_warning",)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "item",
                    "received_document",
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
    def date_warning(self, obj: Attachment) -> str:
        return _attachment_date_warning(obj)


# ---------------------------------------------------------------------------
# ReceivedDocument Admin
# ---------------------------------------------------------------------------


@admin.register(ReceivedDocument)
class ReceivedDocumentAdmin(admin.ModelAdmin[ReceivedDocument]):
    list_display = ("title", "year_link", "date")
    list_filter = ("year",)
    search_fields = ("title", "notes")
    list_select_related = ("year",)
    inlines = [ReceivedAttachmentInline]
    fields = ("year", "title", "date", "notes")

    @admin.display(description="Year")
    def year_link(self, obj: ReceivedDocument) -> str:
        url = reverse("admin:taxtracker_financialyear_change", args=[obj.year_id])
        return format_html('<a href="{}">{}</a>', url, obj.year)

    def get_queryset(self, request: HttpRequest) -> QuerySet[ReceivedDocument]:
        return super().get_queryset(request).select_related("year")

    # ------------------------------------------------------------------
    # Custom URLs
    # ------------------------------------------------------------------

    def get_urls(self) -> list[URLPattern]:
        custom = [
            path(
                "import-archive/",
                self.admin_site.admin_view(self.import_archive_view),
                name="taxtracker_receiveddocument_import_archive",
            ),
        ]
        return custom + super().get_urls()

    # ------------------------------------------------------------------
    # Import archive view
    # ------------------------------------------------------------------

    def import_archive_view(self, request: HttpRequest) -> HttpResponse:
        if not self.has_add_permission(request):
            raise PermissionDenied
        attachment_admin = self.admin_site._registry.get(Attachment)
        if attachment_admin is None or not attachment_admin.has_add_permission(request):
            raise PermissionDenied

        if request.method == "POST":
            form = ReceivedDocumentImportArchiveForm(request.POST, request.FILES)
        else:
            form = ReceivedDocumentImportArchiveForm()

        if request.method == "POST" and form.is_valid():
            uploaded = form.cleaned_data["archive"]
            file_bytes = uploaded.read()
            filename = uploaded.name

            extracted, archive_skipped_names, had_error = _extract_archive(request, file_bytes, filename)
            result = _import_received_documents(extracted)
            if result.created_count:
                messages.success(
                    request,
                    f"Imported {result.created_count} received document(s) from '{filename}'.",
                )

            _warn_skipped(request, archive_skipped_names, "non-PDF, too large or unreadable file(s)")
            _warn_skipped(request, result.duplicate_names, "file(s) already imported for that Financial Year")
            _warn_skipped(request, result.no_year_names, "file(s) without a leading 4-digit year")
            _warn_skipped(request, result.missing_fy_names, "file(s) with no matching Financial Year record")
            if not had_error and not extracted and not archive_skipped_names:
                messages.warning(
                    request,
                    f"No attachments could be extracted from '{filename}'.",
                )

            return redirect(reverse("admin:taxtracker_receiveddocument_changelist"))

        context = {
            **self.admin_site.each_context(request),
            "title": "Import Archive — Received Documents",
            "form": form,
            "opts": self.model._meta,
        }
        return render(
            request,
            "admin/taxtracker/receiveddocument/import_archive.html",
            context,
        )


# ---------------------------------------------------------------------------
# FinancialYear Admin
# ---------------------------------------------------------------------------


def _multi_zip_filename(fys: Sequence[FinancialYear]) -> str:
    """The download name for a ZIP of *fys*: the year, or the first and last years."""
    years = sorted(fy.year for fy in fys)
    if len(years) == 1:
        return f"FY{years[0]}_attachments.zip"
    return f"FY{years[0]}-FY{years[-1]}_multi_attachments.zip"


def _zip_response(buf: IO[bytes], filename: str) -> FileResponse:
    """Stream the ZIP in *buf* as a download, closing *buf* once it's sent."""
    return FileResponse(buf, as_attachment=True, filename=filename, content_type="application/zip")


class FinancialYearStatusHistoryInline(admin.TabularInline[FinancialYearStatusHistory, FinancialYear]):
    model = FinancialYearStatusHistory
    extra = 0
    ordering = ("-transitioned_at", "-pk")


@admin.register(FinancialYear)
class FinancialYearAdmin(admin.ModelAdmin[FinancialYear]):
    list_display = (
        "name",
        "period",
        "lodgement_date_display",
        "days_until_lodgement_display",
        "status_display",
        "status_since_display",
        "summary_link",
        "download_zip_link",
    )
    fields = ("year", "status", "notes", "lodgement_date_override")
    search_fields = ("year",)
    inlines = [FinancialYearStatusHistoryInline]

    # ------------------------------------------------------------------
    # Custom URLs
    # ------------------------------------------------------------------

    def get_urls(self) -> list[URLPattern]:
        custom = [
            path(
                "<int:pk>/summary/",
                self.admin_site.admin_view(self.summary_view),
                name="taxtracker_financialyear_summary",
            ),
            path(
                "<int:pk>/download-zip/",
                self.admin_site.admin_view(self.download_zip_view),
                name="taxtracker_financialyear_download_zip",
            ),
            path(
                "<int:pk>/copy-to-new-year/",
                self.admin_site.admin_view(self.copy_to_new_year_view),
                name="taxtracker_financialyear_copy_to_new_year",
            ),
            path(
                "download-multi-zip/",
                self.admin_site.admin_view(self.download_multi_zip_view),
                name="taxtracker_financialyear_download_multi_zip",
            ),
        ]
        return custom + super().get_urls()

    # ------------------------------------------------------------------
    # List display helpers
    # ------------------------------------------------------------------

    @admin.display(description="Financial Year")
    def name(self, obj: FinancialYear) -> str:
        return str(obj)

    @admin.display(description="Period")
    def period(self, obj: FinancialYear) -> str:
        return f"{obj.start_date} – {obj.end_date}"

    @admin.display(description="Lodgement Date")
    def lodgement_date_display(self, obj: FinancialYear) -> str:
        suffix = "" if obj.lodgement_date_override is None else " ★"
        return f"{obj.effective_lodgement_date}{suffix}"

    @admin.display(description="Days Until Lodgement")
    def days_until_lodgement_display(self, obj: FinancialYear) -> str:
        d = obj.days_until_lodgement
        if d > 0:
            return f"{d} days"
        elif d == 0:
            return "Today!"
        else:
            return f"{abs(d)} days ago"

    @admin.display(description="Summary")
    def summary_link(self, obj: FinancialYear) -> str:
        url = reverse("admin:taxtracker_financialyear_summary", args=[obj.pk])
        return format_html('<a href="{}">View Summary</a>', url)

    @admin.display(description="Download")
    def download_zip_link(self, obj: FinancialYear) -> str:
        url = reverse("admin:taxtracker_financialyear_download_zip", args=[obj.pk])
        return format_html('<a href="{}">Download ZIP</a>', url)

    _STATUS_SHORT_LABELS = {
        FinancialYear.STATUS_PENDING_SUBMISSION: "Pending",
        FinancialYear.STATUS_SUBMITTED: "Submitted",
        FinancialYear.STATUS_MORE_INFO_REQUESTED: "More Info Requested",
        FinancialYear.STATUS_FINALISED: "Finalised",
    }

    @admin.display(description="Status")
    def status_display(self, obj: FinancialYear) -> str:
        return self._STATUS_SHORT_LABELS.get(obj.status, obj.get_status_display())

    @admin.display(description="Status Since")
    def status_since_display(self, obj: FinancialYear) -> str | datetime.date:
        dt = obj.current_status_transitioned_at
        return timezone.localdate(dt) if dt else "—"

    def get_queryset(self, request: HttpRequest) -> QuerySet[FinancialYear]:
        return super().get_queryset(request).prefetch_related("status_history")

    # ------------------------------------------------------------------
    # Changelist view override (inject multi-year ZIP URL)
    # ------------------------------------------------------------------

    def changelist_view(self, request: HttpRequest, extra_context: Any = None) -> HttpResponse:
        extra_context = extra_context or {}
        extra_context["download_multi_zip_url"] = reverse("admin:taxtracker_financialyear_download_multi_zip")
        return super().changelist_view(request, extra_context)

    # ------------------------------------------------------------------
    # Summary view
    # ------------------------------------------------------------------

    def summary_view(self, request: HttpRequest, pk: Any) -> HttpResponse:
        fy = get_object_or_404(FinancialYear, pk=pk)
        if not self.has_view_permission(request, fy):
            raise PermissionDenied
        # Fetch all items in a single query to avoid N+1 for deep hierarchies.
        all_items = list(fy.items.prefetch_related("attachments").order_by("order", "title"))
        tree_rows, unreachable_items = tree_rows_and_unreachable_items(all_items)

        total = len(all_items)
        done = sum(1 for i in all_items if i.is_done)
        pending = total - done

        # Fetch previous and next financial years for navigation.
        prev_fy = FinancialYear.objects.filter(year=fy.year - 1).first()
        next_fy = FinancialYear.objects.filter(year=fy.year + 1).first()

        context = {
            **self.admin_site.each_context(request),
            "title": f"Status Summary – {fy}",
            "fy": fy,
            "tree_rows": tree_rows,
            "unreachable_items": unreachable_items,
            "total": total,
            "done": done,
            "pending": pending,
            "prev_fy": prev_fy,
            "next_fy": next_fy,
            "opts": self.model._meta,
        }
        return render(request, "admin/taxtracker/financialyear/summary.html", context)

    # ------------------------------------------------------------------
    # ZIP download view
    # ------------------------------------------------------------------

    def _check_export_permission(self, request: HttpRequest, fy: FinancialYear | None = None) -> None:
        """Raise PermissionDenied unless the user can view what a ZIP exports.

        A ZIP (or its index) holds the year's items, their notes and every
        attachment's file, so it needs view permission on all three models,
        not just FinancialYear.
        """
        if not self.has_view_permission(request, fy):
            raise PermissionDenied
        for model in (Item, Attachment):
            model_admin = self.admin_site._registry.get(model)
            if model_admin is None or not model_admin.has_view_permission(request):
                raise PermissionDenied

    def download_zip_view(self, request: HttpRequest, pk: Any) -> FileResponse:
        fy = get_object_or_404(FinancialYear, pk=pk)
        self._check_export_permission(request, fy)
        return _zip_response(build_zip(fy), f"{fy}_attachments.zip")

    # ------------------------------------------------------------------
    # Multi-year ZIP download view
    # ------------------------------------------------------------------

    def download_multi_zip_view(self, request: HttpRequest) -> HttpResponseBase:
        self._check_export_permission(request)
        all_fys = list(FinancialYear.objects.order_by("year").prefetch_related("status_history"))
        error = None

        if request.method == "POST":
            selected_fys = list(FinancialYear.objects.filter(pk__in=_posted_pks(request, "fy_ids")).order_by("year"))
            if not selected_fys:
                error = "Please select at least one financial year."
            elif request.POST.get("action") == "view_index":
                content = build_multi_index_md(selected_fys)
                return HttpResponse(content, content_type="text/markdown; charset=utf-8")
            else:
                return _zip_response(build_multi_zip(selected_fys), _multi_zip_filename(selected_fys))

        context = {
            **self.admin_site.each_context(request),
            "title": "Download Multi-Year ZIP",
            "all_fys": all_fys,
            "default_selected_statuses": {
                FinancialYear.STATUS_PENDING_SUBMISSION,
                FinancialYear.STATUS_MORE_INFO_REQUESTED,
            },
            "error": error,
            "opts": self.model._meta,
        }
        return render(
            request,
            "admin/taxtracker/financialyear/download_multi_zip.html",
            context,
        )

    # ------------------------------------------------------------------
    # Copy to new year view
    # ------------------------------------------------------------------

    def copy_to_new_year_view(self, request: HttpRequest, pk: Any) -> HttpResponse:
        fy = get_object_or_404(FinancialYear, pk=pk)
        if not self.has_view_permission(request, fy):
            raise PermissionDenied
        if not self.has_add_permission(request):
            raise PermissionDenied
        item_admin = self.admin_site._registry.get(Item)
        if item_admin is None or not item_admin.has_add_permission(request):
            raise PermissionDenied
        new_year_num = fy.year + 1

        if FinancialYear.objects.filter(year=new_year_num).exists():
            messages.error(
                request,
                f"FY{new_year_num} already exists.",
            )
            return redirect(reverse("admin:taxtracker_financialyear_changelist"))

        if request.method == "POST":
            try:
                with transaction.atomic():
                    new_fy = FinancialYear.objects.create(year=new_year_num)
                    skipped_items = _copy_items(fy, new_fy)
            except IntegrityError:
                messages.error(
                    request,
                    f"FY{new_year_num} already exists.",
                )
                return redirect(reverse("admin:taxtracker_financialyear_changelist"))
            if skipped_items:
                messages.warning(
                    request,
                    f"{len(skipped_items)} item(s) were not copied because they are not reachable from a root item.",
                )
            messages.success(
                request,
                f"Created FY{new_year_num} with items copied from {fy}.",
            )
            return redirect(reverse("admin:taxtracker_financialyear_change", args=[new_fy.pk]))

        _, unreachable_items = tree_rows_and_unreachable_items(list(fy.items.order_by("order", "title")))
        context = {
            **self.admin_site.each_context(request),
            "title": f"Copy {fy} to FY{new_year_num}",
            "fy": fy,
            "new_year_num": new_year_num,
            "unreachable_items": unreachable_items,
            "opts": self.model._meta,
        }
        return render(
            request,
            "admin/taxtracker/financialyear/copy_to_new_year.html",
            context,
        )

    # ------------------------------------------------------------------
    # Change-object buttons
    # ------------------------------------------------------------------

    def change_view(
        self, request: HttpRequest, object_id: Any, form_url: Any = "", extra_context: Any = None
    ) -> HttpResponse:
        # Let the admin look the object up first, so a malformed or unknown ID
        # gets its usual redirect rather than reaching the queries below; a
        # successful POST redirects too.
        response = super().change_view(request, object_id, form_url, extra_context)
        if not isinstance(response, TemplateResponse) or response.context_data is None:
            return response

        # Reverse with fy.pk, not object_id: the admin accepts e.g. "+1", which <int:pk> URLs don't.
        context_data = response.context_data
        fy = context_data["original"]
        context_data["summary_url"] = reverse("admin:taxtracker_financialyear_summary", args=[fy.pk])
        context_data["download_zip_url"] = reverse("admin:taxtracker_financialyear_download_zip", args=[fy.pk])
        context_data["copy_to_new_year_url"] = reverse("admin:taxtracker_financialyear_copy_to_new_year", args=[fy.pk])
        context_data["prev_fy"] = FinancialYear.objects.filter(year=fy.year - 1).first()
        context_data["next_fy"] = FinancialYear.objects.filter(year=fy.year + 1).first()
        return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _copy_items(source_fy: FinancialYear, dest_fy: FinancialYear) -> list[Item]:
    """Copy the item hierarchy from *source_fy* to *dest_fy*.

    Notes, status and attachments are NOT copied.
    """
    source_items = list(source_fy.items.order_by("order", "title"))
    tree_rows, unreachable_items = tree_rows_and_unreachable_items(source_items)
    old_to_new: dict[Any, Any] = {}
    for row in tree_rows:
        item = row["item"]
        new_item = Item.objects.create(
            year=dest_fy,
            parent=old_to_new.get(item.parent_id),
            title=item.title,
            order=item.order,
        )
        old_to_new[item.pk] = new_item
    return unreachable_items
