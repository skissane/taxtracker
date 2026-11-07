import dataclasses
import datetime
import os
import re
import shutil
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import IO, Any

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, QuerySet
from django.http import FileResponse, HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.utils.html import format_html

from lifetracker.core.archives import select_importable_files
from lifetracker.core.attachments import extract_date_from_filename
from lifetracker.core.models import DBStoredFile
from lifetracker.core.trees import CYCLE_MARKER
from lifetracker.taxtracker.archives import (
    UnsupportedArchiveError,
    extract_from_archive_with_skips,
)
from lifetracker.taxtracker.financial_year import financial_year_for_date
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


def _item_path_signature(item: Item) -> list[tuple[str, int]]:
    """Return root→leaf signature for an item path as ``[(title, order), ...]``."""
    signature = []
    current: Item | None = item
    visited = set()
    while current is not None and current.pk not in visited:
        visited.add(current.pk)
        signature.append((current.title, current.order))
        current = current.parent
    return list(reversed(signature))


def _find_equivalent_item(path_signature: list[tuple[str, int]], target_fy: FinancialYear) -> Item | None:
    """Find the equivalent item in ``target_fy`` by matching title/order path."""
    parent_id = None
    current = None
    for title, order in path_signature:
        matches = list(
            Item.objects.filter(
                year=target_fy,
                parent_id=parent_id,
                title=title,
                order=order,
            ).order_by("pk")[:2]
        )
        if len(matches) != 1:
            return None
        current = matches[0]
        parent_id = current.pk
    return current


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

        path_signature = _item_path_signature(item)
        prev_fy = FinancialYear.objects.filter(year=item.year.year - 1).first()
        next_fy = FinancialYear.objects.filter(year=item.year.year + 1).first()

        response.context_data["import_archive_url"] = reverse("admin:taxtracker_item_import_archive", args=[item.pk])
        response.context_data["reassign_attachments_url"] = reverse(
            "admin:taxtracker_item_reassign_attachments", args=[item.pk]
        )
        response.context_data["prev_item"] = _find_equivalent_item(path_signature, prev_fy) if prev_fy else None
        response.context_data["next_item"] = _find_equivalent_item(path_signature, next_fy) if next_fy else None
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

            duplicate_names: list[str] = []
            if extracted:
                existing_names = _existing_attachment_names(item=item)
                importable = select_importable_files(extracted, existing_names, duplicate_names)

                with transaction.atomic():
                    for pdf_filename, pdf_bytes in importable:
                        _save_imported_attachment(Attachment(item=item), pdf_filename, pdf_bytes)
                if importable:
                    messages.success(
                        request,
                        f"Imported {len(importable)} attachment(s) from '{filename}'.",
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
        path_signature = _item_path_signature(item)
        target_cache = {}
        rows: list[dict[str, Any]] = []

        for attachment in attachments:
            if attachment.date is None or item.year.start_date <= attachment.date <= item.year.end_date:
                continue

            target_year_num = financial_year_for_date(attachment.date)
            target_fy = FinancialYear.objects.filter(year=target_year_num).first()

            if target_fy is None:
                target_item = None
                warning = f"Financial year FY{target_year_num} not found."
            else:
                if target_fy.pk not in target_cache:
                    target_cache[target_fy.pk] = _find_equivalent_item(path_signature, target_fy)
                target_item = target_cache[target_fy.pk]
                if target_item is None:
                    warning = f"Equivalent item not found in FY{target_year_num}."
                else:
                    warning = ""

            rows.append(
                {
                    "attachment": attachment,
                    "target_year_num": target_year_num,
                    "target_item": target_item,
                    "warning": warning,
                    "movable": target_item is not None,
                }
            )

        initial_summary = {
            "total_attachments": len(attachments),
            "correct_item_count": len(attachments) - len(rows),
            "unmovable_count": sum(1 for row in rows if not row["movable"]),
            "movable_count": sum(1 for row in rows if row["movable"]),
        }

        result_rows = []
        result_summary = None
        if request.method == "POST":
            selected_ids = {int(v) for v in request.POST.getlist("move_attachment_ids") if v.isdigit()}
            with transaction.atomic():
                for row in rows:
                    attachment = row["attachment"]
                    if row["movable"] and attachment.pk in selected_ids:
                        attachment.item = row["target_item"]
                        attachment.save(update_fields=["item"])
                        status = "Moved"
                    elif row["movable"]:
                        status = "Not selected"
                    else:
                        status = "Unmovable"

                    result_rows.append(
                        {
                            **row,
                            "status": status,
                        }
                    )

            result_summary = {
                "total_out_of_year": len(rows),
                "moved_count": sum(1 for row in result_rows if row.get("status") == "Moved"),
                "not_selected_count": sum(1 for row in result_rows if row.get("status") == "Not selected"),
                "unmovable_count": sum(1 for row in result_rows if row.get("status") == "Unmovable"),
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


_ATX_HEADING_RE = re.compile(r"(#{1,6}) (.+)")
# A fence opens with 3+ backticks or tildes, indented at most 3 spaces (a backtick
# fence's info string can't hold a backtick, or it's an inline code span); a
# closing fence uses the same character, at least as many of it, and nothing after.
_CODE_FENCE_RE = re.compile(r" {0,3}(`{3,}(?=[^`]*$)|~{3,})(.*)")


def _adjust_notes_headings(notes: str, item_heading_level: int) -> str:
    """Re-level ATX headings in *notes* so shallowest becomes item_heading_level+1.

    Lines inside fenced code blocks (``` or ~~~) aren't headings, so are left as
    they are and don't count towards the shallowest level.
    """
    # Split only after "\n", as splitlines() would also split on "\r", "\f" etc.
    lines = re.split(r"(?<=\n)", notes)
    headings = {}
    fence = None
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        fence_match = _CODE_FENCE_RE.fullmatch(content)
        if fence is None:
            if fence_match:
                fence = fence_match.group(1)
            elif heading_match := _ATX_HEADING_RE.fullmatch(content):
                headings[index] = heading_match
        elif (
            fence_match
            and fence_match.group(1)[0] == fence[0]
            and len(fence_match.group(1)) >= len(fence)
            and not fence_match.group(2).strip()
        ):
            fence = None
    if not headings:
        return notes
    min_level = min(len(m.group(1)) for m in headings.values())
    target_base = item_heading_level + 1

    for index, m in headings.items():
        new_level = len(m.group(1)) - min_level + target_base
        line_ending = lines[index][m.end() :]
        lines[index] = "#" * min(new_level, 6) + " " + m.group(2) + line_ending
    return "".join(lines)


def _safe_component(title: str) -> str:
    """Sanitize a title for safe use as a ZIP path component.

    Prevents Zip Slip by replacing path separators and eliminating ``..``
    traversal sequences.  Spaces are replaced with underscores.
    """
    safe = title.replace("/", "_").replace("\\", "_")
    safe = safe.replace(" ", "_")
    safe = safe.strip("._")
    safe = re.sub(r"\.{2,}", ".", safe)
    return safe or "_"


def _folder_path(item: Item, item_map: dict[int, Item]) -> str:
    """Return a POSIX path string representing the item hierarchy."""
    parts: list[str] = []
    current = item
    visited = set()
    while True:
        if current.pk in visited:
            parts.insert(0, _safe_component(CYCLE_MARKER))
            break
        visited.add(current.pk)
        parts.insert(0, _safe_component(current.title))
        if current.parent_id is None:
            break
        parent = item_map.get(current.parent_id) or current.parent
        assert parent is not None  # parent_id is set
        current = parent
    return "/".join(parts)


def _tree_rows_and_unreachable_items(items: Sequence[Item]) -> tuple[list[dict[str, Any]], list[Item]]:
    """Return (tree_rows, unreachable_items) for a list of items."""
    children_map: dict[Any, Any] = {}
    for item in items:
        children_map.setdefault(item.parent_id, []).append(item)

    visited = set()

    # Each item has one parent, so a walk down from the roots can't revisit an
    # item; items in a parent cycle (and their descendants) are never reached.
    def item_tree(item: Item, depth: int = 0) -> list[dict[str, Any]]:
        visited.add(item.pk)
        rows = [{"item": item, "depth": depth}]
        for child in children_map.get(item.pk, []):
            rows.extend(item_tree(child, depth + 1))
        return rows

    tree_rows = []
    for root_item in children_map.get(None, []):
        tree_rows.extend(item_tree(root_item))
    unreachable_items = [item for item in items if item.pk not in visited]
    return tree_rows, unreachable_items


def _dedupe_name(name: str, used: set[str], keep_extension: bool = False) -> str:
    """Return *name*, suffixed `` (2)``, `` (3)``... if *used* already has it.

    *used* holds casefolded names, as archives are often extracted onto
    case-insensitive filesystems; the result is added to it.
    With *keep_extension*, the suffix goes before the extension.
    """
    stem, ext = os.path.splitext(name) if keep_extension else (name, "")
    candidate = name
    counter = 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({counter}){ext}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


def _unique_folder_paths(items: Sequence[Item]) -> tuple[dict[int, str], defaultdict[str, set[str]]]:
    """Return ({item pk: folder path}, {folder path: casefolded names in it}).

    Siblings whose titles are the same after _safe_component() get suffixed
    folder names, which their descendants' paths then use, so no two items
    share a folder.
    The second dict lets attachments avoid names already taken in a folder.
    """
    tree_rows, unreachable_items = _tree_rows_and_unreachable_items(items)
    folder_paths: dict[Any, Any] = {}
    used_names = defaultdict(set)
    used_names[""].add("index.md")
    for row in tree_rows:
        item = row["item"]
        parent_path = folder_paths.get(item.parent_id, "")
        name = _dedupe_name(_safe_component(item.title), used_names[parent_path])
        folder_paths[item.pk] = f"{parent_path}/{name}" if parent_path else name
    # Items in a parent cycle get _folder_path()'s cycle-marked path; their
    # children don't follow a parent's suffix, but every path is still unique.
    item_map = {item.pk: item for item in items}
    for item in unreachable_items:
        parent_path, _, name = _folder_path(item, item_map).rpartition("/")
        name = _dedupe_name(name, used_names[parent_path])
        folder_paths[item.pk] = f"{parent_path}/{name}"
    return folder_paths, used_names


def _build_fy_index_md(fy: FinancialYear, prefix: str = "", zf: zipfile.ZipFile | None = None) -> str:
    """Return the index.md content string for one FinancialYear.

    If *zf* is given, each attachment is also written into that ZipFile.
    """
    items = list(
        fy.items.select_related("parent")
        # pk breaks ties, so duplicate names get the same suffixes every time.
        .prefetch_related(
            Prefetch(
                "attachments",
                queryset=Attachment.objects.order_by("-date", "title", "pk"),
            )
        )
        .order_by("order", "title", "pk")
    )
    folder_paths, used_names = _unique_folder_paths(items)
    items.sort(key=lambda item: folder_paths[item.pk].lower())

    index_lines = [
        f"# {fy} – Attachment Index\n",
        f"**Period:** {fy.start_date} to {fy.end_date}\n",
        f"**Lodgement date:** {fy.effective_lodgement_date}"
        + (" (default)" if fy.lodgement_date_override is None else " (agent override)")
        + "\n\n",
    ]

    for item in items:
        attachments = list(item.attachments.all())
        if not attachments and not item.notes:
            continue
        fp = folder_paths[item.pk]
        index_lines.append(f"## {fp}\n")
        for attachment in attachments:
            safe_name = _safe_component((attachment.file.name or "").split("/")[-1])
            safe_name = _dedupe_name(safe_name, used_names[fp], keep_extension=True)
            entry_path = f"{prefix}{fp}/{safe_name}"
            zip_path: str | None = entry_path
            missing = False
            try:
                if zf is not None:
                    with (
                        attachment.file.open("rb") as fh,
                        zf.open(entry_path, "w") as dest,
                    ):
                        shutil.copyfileobj(fh, dest)
                else:
                    attachment.file.open("rb").close()
            except OSError, DBStoredFile.DoesNotExist, ValueError:
                # File missing or stored blob reference is invalid/corrupt –
                # skip but record in index.
                missing = True
                zip_path = None
            index_lines.append(f"- **{attachment.title}**")
            if attachment.file_type:
                index_lines[-1] += f" ({attachment.file_type})"
            if zip_path:
                index_lines[-1] += f" → `{zip_path}`"
            elif missing:
                index_lines[-1] += " [MISSING]"
            index_lines[-1] += "\n"
            if attachment.notes:
                index_lines.append(f"  {attachment.notes}\n")
        if item.notes:
            adjusted_notes = _adjust_notes_headings(item.notes, 2)
            index_lines.append(f"\n{adjusted_notes}\n\n")
        index_lines.append("\n")

    return "".join(index_lines)


def _write_fy_to_zip(zf: zipfile.ZipFile, fy: FinancialYear, prefix: str = "") -> None:
    """Write one FY's files and index.md into an open ZipFile under *prefix*."""
    index_md = _build_fy_index_md(fy, prefix=prefix, zf=zf)
    zf.writestr(f"{prefix}index.md", index_md)


# ZIPs bigger than this are built on disk rather than in memory, as every
# attachment in them can add up to more than the Machine's RAM.
_ZIP_SPOOL_MAX_SIZE = 10 * 1024 * 1024


def _zip_buffer() -> tempfile.SpooledTemporaryFile[bytes]:
    """Return a file to build a ZIP in: in memory while small, then on disk."""
    return tempfile.SpooledTemporaryFile(max_size=_ZIP_SPOOL_MAX_SIZE)


def _zip_response(buf: IO[bytes], filename: str) -> FileResponse:
    """Stream the ZIP in *buf* as a download, closing *buf* once it's sent."""
    return FileResponse(buf, as_attachment=True, filename=filename, content_type="application/zip")


def _build_zip(fy: FinancialYear) -> IO[bytes]:
    """Build a ZIP file for all attachments of a FinancialYear."""
    buf = _zip_buffer()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_fy_to_zip(zf, fy)
    buf.seek(0)
    return buf


def _build_multi_zip(fys: Iterable[FinancialYear]) -> IO[bytes]:
    """Build a ZIP with each FinancialYear under its own top-level directory."""
    buf = _zip_buffer()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fy in fys:
            _write_fy_to_zip(zf, fy, prefix=f"{fy}/")
    buf.seek(0)
    return buf


def _build_multi_index_md(fys: Iterable[FinancialYear]) -> str:
    """Concatenate index.md content for each FY, separated by horizontal rules."""
    parts = [_build_fy_index_md(fy, prefix=f"{fy}/") for fy in fys]
    return "\n\n---\n\n".join(parts)


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
        tree_rows, unreachable_items = _tree_rows_and_unreachable_items(all_items)

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
        return _zip_response(_build_zip(fy), f"{fy}_attachments.zip")

    # ------------------------------------------------------------------
    # Multi-year ZIP download view
    # ------------------------------------------------------------------

    def download_multi_zip_view(self, request: HttpRequest) -> HttpResponseBase:
        self._check_export_permission(request)
        all_fys = list(FinancialYear.objects.order_by("year").prefetch_related("status_history"))
        error = None

        if request.method == "POST":
            selected_pks: list[int] = []
            for value in request.POST.getlist("fy_ids"):
                try:
                    pk = int(value)
                except ValueError:
                    continue
                if 0 < pk < 2**63:  # SQLite raises OverflowError for anything over 64 bits
                    selected_pks.append(pk)
            selected_fys = list(FinancialYear.objects.filter(pk__in=selected_pks).order_by("year"))
            if not selected_fys:
                error = "Please select at least one financial year."
            elif request.POST.get("action") == "view_index":
                content = _build_multi_index_md(selected_fys)
                return HttpResponse(content, content_type="text/markdown; charset=utf-8")
            else:
                buf = _build_multi_zip(selected_fys)
                years = sorted(fy.year for fy in selected_fys)
                if len(years) == 1:
                    fname = f"FY{years[0]}_attachments.zip"
                else:
                    fname = f"FY{years[0]}-FY{years[-1]}_multi_attachments.zip"
                return _zip_response(buf, fname)

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

        _, unreachable_items = _tree_rows_and_unreachable_items(list(fy.items.order_by("order", "title")))
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
    tree_rows, unreachable_items = _tree_rows_and_unreachable_items(source_items)
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
