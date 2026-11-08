import datetime
from typing import override

from django.test import TestCase

from lifetracker.tasktracker.forms import TaskAttachmentForm
from lifetracker.tasktracker.models import Task


class TaskAttachmentFormTests(TestCase):
    @override
    def setUp(self) -> None:
        self.task = Task.objects.create(title="Inbox", order=1)

    def test_accepts_english_month_name_dates(self) -> None:
        form = TaskAttachmentForm(data={"task": self.task.pk, "title": "T", "date": "20 July 2023"})
        self.assertNotIn("date", form.errors)
        self.assertEqual(form.cleaned_data["date"], datetime.date(2023, 7, 20))
