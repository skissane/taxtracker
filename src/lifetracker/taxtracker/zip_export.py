"""Build a financial year's attachments into a ZIP, with an ``index.md`` listing them.

Used by ``FinancialYearAdmin``'s ZIP downloads and its "View Index (Markdown)"
action; AGENTS.md's "ZIP export rules" describe the layout.
"""

import os
import re
import shutil
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import IO, Any

from django.db.models import Prefetch

from lifetracker.core.models import DBStoredFile
from lifetracker.core.trees import CYCLE_MARKER
from lifetracker.taxtracker.models import Attachment, FinancialYear, Item

_ATX_HEADING_RE = re.compile(r"(#{1,6}) (.+)")
# A fence opens with 3+ backticks or tildes, indented at most 3 spaces (a backtick
# fence's info string can't hold a backtick, or it's an inline code span); a
# closing fence uses the same character, at least as many of it, and nothing after.
_CODE_FENCE_RE = re.compile(r" {0,3}(`{3,}+(?=[^`]*$)|~{3,}+)(.*)")


def adjust_notes_headings(notes: str, item_heading_level: int) -> str:
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


def tree_rows_and_unreachable_items(items: Sequence[Item]) -> tuple[list[dict[str, Any]], list[Item]]:
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
    tree_rows, unreachable_items = tree_rows_and_unreachable_items(items)
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


def _copy_attachment(attachment: Attachment, zf: zipfile.ZipFile | None, entry_path: str) -> bool:
    """Copy *attachment*'s file into *zf* as *entry_path*, or without *zf* just check it opens.

    Returns False if the file is missing or its stored blob reference is invalid or corrupt.
    """
    try:
        if zf is not None:
            with attachment.file.open("rb") as fh, zf.open(entry_path, "w") as dest:
                shutil.copyfileobj(fh, dest)
        else:
            attachment.file.open("rb").close()
    except OSError, DBStoredFile.DoesNotExist, ValueError:
        return False
    return True


def _attachment_index_lines(attachment: Attachment, zip_path: str | None) -> list[str]:
    """Return index.md's lines for *attachment*, at *zip_path* in the ZIP, or None if its file is missing."""
    file_type = f" ({attachment.file_type})" if attachment.file_type else ""
    location = f" → `{zip_path}`" if zip_path else " [MISSING]"
    lines = [f"- **{attachment.title}**{file_type}{location}\n"]
    if attachment.notes:
        lines.append(f"  {attachment.notes}\n")
    return lines


def build_fy_index_md(fy: FinancialYear, prefix: str = "", zf: zipfile.ZipFile | None = None) -> str:
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
            copied = _copy_attachment(attachment, zf, entry_path)
            index_lines += _attachment_index_lines(attachment, entry_path if copied else None)
        if item.notes:
            adjusted_notes = adjust_notes_headings(item.notes, 2)
            index_lines.append(f"\n{adjusted_notes}\n\n")
        index_lines.append("\n")

    return "".join(index_lines)


def _write_fy_to_zip(zf: zipfile.ZipFile, fy: FinancialYear, prefix: str = "") -> None:
    """Write one FY's files and index.md into an open ZipFile under *prefix*."""
    index_md = build_fy_index_md(fy, prefix=prefix, zf=zf)
    zf.writestr(f"{prefix}index.md", index_md)


# ZIPs bigger than this are built on disk rather than in memory, as every
# attachment in them can add up to more than the Machine's RAM.
ZIP_SPOOL_MAX_SIZE = 10 * 1024 * 1024


def _zip_buffer() -> tempfile.SpooledTemporaryFile[bytes]:
    """Return a file to build a ZIP in: in memory while small, then on disk."""
    return tempfile.SpooledTemporaryFile(max_size=ZIP_SPOOL_MAX_SIZE)


def build_zip(fy: FinancialYear) -> IO[bytes]:
    """Build a ZIP file for all attachments of a FinancialYear."""
    buf = _zip_buffer()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_fy_to_zip(zf, fy)
    buf.seek(0)
    return buf


def build_multi_zip(fys: Iterable[FinancialYear]) -> IO[bytes]:
    """Build a ZIP with each FinancialYear under its own top-level directory."""
    buf = _zip_buffer()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fy in fys:
            _write_fy_to_zip(zf, fy, prefix=f"{fy}/")
    buf.seek(0)
    return buf


def build_multi_index_md(fys: Iterable[FinancialYear]) -> str:
    """Concatenate index.md content for each FY, separated by horizontal rules."""
    parts = [build_fy_index_md(fy, prefix=f"{fy}/") for fy in fys]
    return "\n\n---\n\n".join(parts)
