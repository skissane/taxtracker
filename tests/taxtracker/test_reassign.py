import datetime
from typing import override

from django.core.files.base import ContentFile
from django.test import TestCase

from lifetracker.taxtracker.models import Attachment, FinancialYear, Item
from lifetracker.taxtracker.reassign import apply_reassignments, item_path_signature, plan_reassignments


class PlanReassignmentsTests(TestCase):
    @override
    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)
        self.root = Item.objects.create(year=self.fy, title="Root", order=1)
        self.item = Item.objects.create(year=self.fy, parent=self.root, title="Leaf", order=2)

    def _attach(self, name: str, date: datetime.date | None) -> Attachment:
        return Attachment.objects.create(
            item=self.item, title=name, date=date, file=ContentFile(name.encode(), name=name)
        )

    def test_item_path_signature_is_root_first(self) -> None:
        self.assertEqual(item_path_signature(self.item), [("Root", 1), ("Leaf", 2)])

    def test_skips_in_year_and_undated_attachments(self) -> None:
        attachments = [self._attach("in-year.pdf", datetime.date(2024, 6, 30)), self._attach("undated.pdf", None)]
        self.assertEqual(plan_reassignments(self.item, attachments), [])

    def test_warns_when_no_equivalent_item(self) -> None:
        fy2025 = FinancialYear.objects.create(year=2025)
        # Same leaf title, but under no "Root", so not the same path.
        Item.objects.create(year=fy2025, title="Leaf", order=2)
        (row,) = plan_reassignments(self.item, [self._attach("later.pdf", datetime.date(2024, 7, 1))])
        self.assertEqual(row.target_year_num, 2025)
        self.assertIsNone(row.target_item)
        self.assertFalse(row.movable)
        self.assertEqual(row.warning, "Equivalent item not found in FY2025.")

    def test_looks_up_each_year_once(self) -> None:
        fy2025 = FinancialYear.objects.create(year=2025)
        root = Item.objects.create(year=fy2025, title="Root", order=1)
        target = Item.objects.create(year=fy2025, parent=root, title="Leaf", order=2)
        attachments = [self._attach(f"{day}.pdf", datetime.date(2024, 8, day)) for day in (1, 2, 3)]
        # One query for the FY, then one per level of the path.
        with self.assertNumQueries(3):
            plan = plan_reassignments(self.item, attachments)
        self.assertEqual([row.target_item for row in plan], [target] * 3)
        self.assertEqual([row.warning for row in plan], [""] * 3)


class ApplyReassignmentsTests(TestCase):
    def test_moves_only_selected_movable_attachments(self) -> None:
        fy = FinancialYear.objects.create(year=2024)
        fy2025 = FinancialYear.objects.create(year=2025)
        item = Item.objects.create(year=fy, title="Item", order=1)
        target = Item.objects.create(year=fy2025, title="Item", order=1)
        selected, unselected = (
            Attachment.objects.create(
                item=item, title=name, date=datetime.date(2024, 8, 1), file=ContentFile(b"x", name=name)
            )
            for name in ("selected.pdf", "unselected.pdf")
        )
        unmovable = Attachment.objects.create(
            item=item, title="unmovable.pdf", date=datetime.date(2025, 8, 1), file=ContentFile(b"x", name="u.pdf")
        )
        plan = plan_reassignments(item, [selected, unselected, unmovable])

        results = apply_reassignments(plan, {selected.pk, unmovable.pk})

        self.assertEqual([row.status for row in results], ["Moved", "Not selected", "Unmovable"])
        self.assertEqual([row.status for row in plan], ["", "", ""])
        for attachment in (selected, unselected, unmovable):
            attachment.refresh_from_db()
        self.assertEqual(selected.item_id, target.pk)
        self.assertEqual(unselected.item_id, item.pk)
        self.assertEqual(unmovable.item_id, item.pk)
