"""Finding an item's equivalent in another financial year, and moving out-of-year attachments to it."""

import dataclasses
from collections.abc import Iterable, Sequence

from django.db import transaction

from lifetracker.taxtracker.financial_year import financial_year_for_date
from lifetracker.taxtracker.models import Attachment, FinancialYear, Item


def item_path_signature(item: Item) -> list[tuple[str, int]]:
    """Return root→leaf signature for an item path as ``[(title, order), ...]``."""
    signature = []
    current: Item | None = item
    visited = set()
    while current is not None and current.pk not in visited:
        visited.add(current.pk)
        signature.append((current.title, current.order))
        current = current.parent
    return list(reversed(signature))


def find_equivalent_item(path_signature: list[tuple[str, int]], target_fy: FinancialYear) -> Item | None:
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


@dataclasses.dataclass(frozen=True)
class Reassignment:
    """An attachment dated outside its item's FY, and where it would move to."""

    attachment: Attachment
    target_year_num: int
    target_item: Item | None
    warning: str
    # Set by apply_reassignments(): "Moved", "Not selected" or "Unmovable".
    status: str = ""

    @property
    def movable(self) -> bool:
        return self.target_item is not None


def plan_reassignments(item: Item, attachments: Iterable[Attachment]) -> list[Reassignment]:
    """Return a Reassignment for each of *item*'s *attachments* dated outside its FY.

    Each one's target is the item with the same path (see item_path_signature()) in the FY of its date.
    """
    path_signature = item_path_signature(item)
    # FY year number -> (warning, equivalent item), as several attachments often share a year.
    targets: dict[int, tuple[str, Item | None]] = {}
    plan = []
    for attachment in attachments:
        if attachment.date is None or item.year.start_date <= attachment.date <= item.year.end_date:
            continue
        year_num = financial_year_for_date(attachment.date)
        if year_num not in targets:
            targets[year_num] = _target_in_year(path_signature, year_num)
        warning, target_item = targets[year_num]
        plan.append(Reassignment(attachment, year_num, target_item, warning))
    return plan


def _target_in_year(path_signature: list[tuple[str, int]], year_num: int) -> tuple[str, Item | None]:
    """Return ``(warning, item)``: the equivalent item in FY *year_num*, or None and why not."""
    target_fy = FinancialYear.objects.filter(year=year_num).first()
    if target_fy is None:
        return f"Financial year FY{year_num} not found.", None
    target_item = find_equivalent_item(path_signature, target_fy)
    if target_item is None:
        return f"Equivalent item not found in FY{year_num}.", None
    return "", target_item


def apply_reassignments(plan: Sequence[Reassignment], selected_ids: set[int]) -> list[Reassignment]:
    """Move each movable attachment in *plan* whose pk is in *selected_ids*, in one transaction.

    Returns *plan* with each entry's status set.
    """
    results = []
    with transaction.atomic():
        for row in plan:
            if not row.movable:
                status = "Unmovable"
            elif row.attachment.pk in selected_ids:
                row.attachment.item = row.target_item
                row.attachment.save(update_fields=["item"])
                status = "Moved"
            else:
                status = "Not selected"
            results.append(dataclasses.replace(row, status=status))
    return results
