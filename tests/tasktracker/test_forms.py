import datetime

from django.contrib import admin
from django.test import TestCase

from lifetracker.tasktracker.forms import TaskAttachmentForm
from lifetracker.tasktracker.models import Task, TaskAttachment


class TaskAttachmentFormTests(TestCase):
    def setUp(self) -> None:
        self.task = Task.objects.create(title="Inbox", order=1)

    def test_accepts_english_month_name_dates(self) -> None:
        form = TaskAttachmentForm(data={"task": self.task.pk, "title": "T", "date": "20 July 2023"})
        self.assertNotIn("date", form.errors)
        self.assertEqual(form.cleaned_data["date"], datetime.date(2023, 7, 20))

    def test_admin_uses_it(self) -> None:
        self.assertIs(admin.site._registry[TaskAttachment].form, TaskAttachmentForm)
        task_admin = admin.site._registry[Task]
        inline = next(i for i in task_admin.inlines if i.model is TaskAttachment)
        self.assertIs(inline.form, TaskAttachmentForm)
