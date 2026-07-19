import datetime

from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase

from lifetracker.core.models import (
    DBStoredFile,
    FileExtension,
    FileType,
    database_storage,
)
from lifetracker.tasktracker.admin import TaskTypeAdmin
from lifetracker.tasktracker.models import (
    Task,
    TaskAttachment,
    TaskType,
    _extract_date_from_filename,
)


class TaskTypeModelTests(TestCase):
    def test_str(self):
        task_type = TaskType.objects.create(name="Chore")
        self.assertEqual(str(task_type), "Chore")

    def test_name_must_be_unique(self):
        TaskType.objects.create(name="Chore")
        duplicate = TaskType(name="Chore")
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_content_type_is_optional(self):
        task_type = TaskType.objects.create(name="Standalone")
        self.assertIsNone(task_type.content_type)

    def test_generic_row_created_by_migration(self):
        generic = TaskType.objects.get(pk=TaskType.GENERIC_PK)
        self.assertEqual(generic.name, "Generic")
        self.assertIsNone(generic.content_type_id)

    def test_clean_rejects_renaming_pk1(self):
        generic = TaskType.objects.get(pk=TaskType.GENERIC_PK)
        generic.name = "Not Generic"
        with self.assertRaises(ValidationError):
            generic.full_clean()

    def test_clean_rejects_other_row_named_generic(self):
        other = TaskType(name="Generic")
        with self.assertRaises(ValidationError):
            other.full_clean()

    def test_clean_rejects_content_type_on_pk1(self):
        generic = TaskType.objects.get(pk=TaskType.GENERIC_PK)
        generic.content_type = ContentType.objects.get_for_model(TaskType)
        with self.assertRaises(ValidationError):
            generic.full_clean()

    def test_db_check_constraint_rejects_renaming_pk1(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskType.objects.filter(pk=TaskType.GENERIC_PK).update(name="Not Generic")

    def test_db_check_constraint_rejects_content_type_on_pk1(self):
        ct = ContentType.objects.get_for_model(TaskType)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskType.objects.filter(pk=TaskType.GENERIC_PK).update(content_type=ct)

    def test_db_check_constraint_rejects_other_row_named_generic(self):
        other = TaskType.objects.create(name="Placeholder")
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskType.objects.filter(pk=other.pk).update(name="Generic")

    def test_delete_rejected_for_generic(self):
        generic = TaskType.objects.get(pk=TaskType.GENERIC_PK)
        with self.assertRaises(ValidationError):
            generic.delete()
        self.assertTrue(TaskType.objects.filter(pk=TaskType.GENERIC_PK).exists())

    def test_delete_allowed_for_other_task_type(self):
        other = TaskType.objects.create(name="Disposable")
        other.delete()
        self.assertFalse(TaskType.objects.filter(pk=other.pk).exists())


class TaskModelTests(TestCase):
    def setUp(self):
        self.file_type = FileType.objects.create(
            short_name="PDF",
            full_name="PDF Document",
        )
        self.file_type_content_type = ContentType.objects.get_for_model(FileType)
        self.typed_task_type = TaskType.objects.create(
            name="File-related",
            content_type=self.file_type_content_type,
        )
        self.untyped_task_type = TaskType.objects.create(name="Untyped")
        self.root = Task.objects.create(
            title="Home", order=1, task_type=self.untyped_task_type
        )
        self.child = Task.objects.create(
            parent=self.root,
            title="Laundry",
            order=1,
            task_type=self.untyped_task_type,
        )

    def test_str_root(self):
        self.assertEqual(str(self.root), "Home")

    def test_str_child(self):
        self.assertEqual(str(self.child), "Home > Laundry")

    def test_str_is_cycle_safe(self):
        a = Task.objects.create(title="A", order=2, task_type=self.untyped_task_type)
        b = Task.objects.create(title="B", order=3, task_type=self.untyped_task_type)
        Task.objects.filter(pk=a.pk).update(parent_id=b.pk)
        Task.objects.filter(pk=b.pk).update(parent_id=a.pk)
        a.refresh_from_db()
        self.assertEqual(str(a), "… > B > A")

    def test_get_folder_path_child(self):
        self.assertEqual(self.child.get_folder_path(), ["Home", "Laundry"])

    def test_is_done_false(self):
        self.assertFalse(self.root.is_done)

    def test_is_done_true(self):
        self.root.status = Task.STATUS_DONE
        self.assertTrue(self.root.is_done)

    def test_clean_rejects_direct_self_parent(self):
        self.root.parent_id = self.root.pk
        with self.assertRaises(ValidationError):
            self.root.clean()

    def test_clean_rejects_multi_level_cycle(self):
        third = Task.objects.create(
            parent=self.child,
            title="Fold",
            order=1,
            task_type=self.untyped_task_type,
        )
        self.root.parent_id = third.pk
        with self.assertRaises(ValidationError):
            self.root.clean()

    def test_clean_allows_unchanged_parent_on_existing_cycle(self):
        Task.objects.filter(pk=self.root.pk).update(parent_id=self.root.pk)
        self.root.refresh_from_db()
        self.root.clean()

    def test_matching_content_type_allowed(self):
        task = Task(
            title="Review PDF",
            task_type=self.typed_task_type,
            content_type=self.file_type_content_type,
            object_id=self.file_type.pk,
        )
        task.full_clean()

    def test_blank_content_type_allowed_when_task_type_has_one(self):
        task = Task(title="Optional link", task_type=self.typed_task_type)
        task.full_clean()

    def test_mismatched_content_type_rejected(self):
        other_content_type = ContentType.objects.get_for_model(TaskType)
        task = Task(
            title="Wrong link",
            task_type=self.typed_task_type,
            content_type=other_content_type,
            object_id=self.typed_task_type.pk,
        )
        with self.assertRaises(ValidationError):
            task.full_clean()

    def test_content_type_forbidden_when_task_type_has_none(self):
        task = Task(
            title="Forbidden link",
            task_type=self.untyped_task_type,
            content_type=self.file_type_content_type,
            object_id=self.file_type.pk,
        )
        with self.assertRaises(ValidationError):
            task.full_clean()

    def test_content_type_forbidden_when_task_type_is_none(self):
        # task_type is a required field, but clean() is defensive against
        # being called before clean_fields() has enforced that.
        task = Task(
            title="Forbidden link",
            content_type=self.file_type_content_type,
            object_id=self.file_type.pk,
        )
        with self.assertRaises(ValidationError):
            task.clean()

    def test_content_type_and_object_id_must_be_set_together(self):
        task = Task(
            title="Half link",
            task_type=self.typed_task_type,
            content_type=self.file_type_content_type,
        )
        with self.assertRaises(ValidationError):
            task.full_clean()

    def test_baseline_all_none_case_is_valid(self):
        task = Task(title="No link", task_type=self.untyped_task_type)
        task.full_clean()

    def test_task_type_defaults_to_generic_when_omitted(self):
        task = Task.objects.create(title="No type given")
        self.assertEqual(task.task_type_id, TaskType.GENERIC_PK)

    def test_task_type_explicit_none_rejected_by_clean(self):
        task = Task(title="No type", task_type=None)
        with self.assertRaises(ValidationError):
            task.full_clean()

    def test_task_type_explicit_none_rejected_at_db_level(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Task.objects.create(title="No type", task_type=None)

    def test_linked_object_resolves(self):
        task = Task.objects.create(
            title="Has link",
            task_type=self.typed_task_type,
            content_type=self.file_type_content_type,
            object_id=self.file_type.pk,
        )
        self.assertEqual(task.linked_object, self.file_type)

    def test_clean_handles_missing_self_row(self):
        task = Task(pk=99999, title="Ghost", parent=self.root)
        task.clean()

    def test_clean_handles_dangling_parent(self):
        self.root.parent_id = 99999
        self.root.clean()

    def test_get_folder_path_is_cycle_safe(self):
        a = Task.objects.create(title="A", order=2, task_type=self.untyped_task_type)
        b = Task.objects.create(title="B", order=3, task_type=self.untyped_task_type)
        Task.objects.filter(pk=a.pk).update(parent_id=b.pk)
        Task.objects.filter(pk=b.pk).update(parent_id=a.pk)
        a.refresh_from_db()
        self.assertEqual(a.get_folder_path(), ["B", "A"])


class TaskAttachmentDateTests(TestCase):
    def test_extract_iso_date(self):
        self.assertEqual(
            _extract_date_from_filename("statement-2026-01-15.pdf"),
            datetime.date(2026, 1, 15),
        )

    def test_extract_compact_date(self):
        self.assertEqual(
            _extract_date_from_filename("report20260115.pdf"),
            datetime.date(2026, 1, 15),
        )

    def test_invalid_date_skipped(self):
        self.assertIsNone(_extract_date_from_filename("2026-13-45.pdf"))

    def test_returns_first_valid_date(self):
        self.assertEqual(
            _extract_date_from_filename("2026-13-45_2025-06-30.txt"),
            datetime.date(2025, 6, 30),
        )

    def test_no_date_in_filename(self):
        self.assertIsNone(_extract_date_from_filename("nodatehere.pdf"))


class TaskAttachmentModelTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(title="Inbox", order=1)
        self.pdf_type = FileType.objects.create(
            short_name="PDF",
            full_name="PDF Document",
        )
        FileExtension.objects.create(
            file_type=self.pdf_type,
            extension="pdf",
            is_primary=True,
        )

    def _make_file(self, name="document.pdf", content=b"%PDF-1.4 test"):
        return ContentFile(content, name=name)

    def test_title_file_type_and_date_auto_populated(self):
        attachment = TaskAttachment(
            task=self.task, file=self._make_file("2026-03-15.pdf")
        )
        attachment.save()
        attachment.refresh_from_db()
        self.assertEqual(attachment.title, "2026-03-15.pdf")
        self.assertEqual(attachment.file_type, self.pdf_type)
        self.assertEqual(attachment.date, datetime.date(2026, 3, 15))

    def test_explicit_values_not_overwritten(self):
        explicit_date = datetime.date(2000, 1, 1)
        other_type = FileType.objects.create(short_name="TXT", full_name="Text File")
        attachment = TaskAttachment(
            task=self.task,
            title="Custom title",
            date=explicit_date,
            file_type=other_type,
            file=self._make_file("2026-03-15.pdf"),
        )
        attachment.save()
        attachment.refresh_from_db()
        self.assertEqual(attachment.title, "Custom title")
        self.assertEqual(attachment.date, explicit_date)
        self.assertEqual(attachment.file_type, other_type)

    def test_str(self):
        attachment = TaskAttachment(
            task=self.task,
            title="Document",
            file=self._make_file(),
        )
        self.assertEqual(str(attachment), "Document (Inbox)")

    def test_delete_removes_stored_file(self):
        attachment = TaskAttachment(task=self.task, file=self._make_file("delete.pdf"))
        attachment.save()
        file_name = attachment.file.name
        pk = attachment.file.storage._name_to_pk(file_name)
        self.assertTrue(DBStoredFile.objects.filter(pk=pk).exists())
        self.assertTrue(database_storage.exists(file_name))
        attachment.delete()
        self.assertFalse(DBStoredFile.objects.filter(pk=pk).exists())
        self.assertFalse(database_storage.exists(file_name))

    def test_task_fk_required(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskAttachment.objects.create(file=self._make_file("orphan.pdf"))

    def test_str_falls_back_to_file_name(self):
        attachment = TaskAttachment(task=self.task, file=self._make_file("photo.jpg"))
        self.assertEqual(str(attachment), "photo.jpg (Inbox)")

    def test_str_falls_back_to_no_file(self):
        attachment = TaskAttachment(task=self.task)
        self.assertEqual(str(attachment), "(no file) (Inbox)")

    def test_unknown_extension_leaves_file_type_unset(self):
        attachment = TaskAttachment(task=self.task, file=self._make_file("mystery.xyz"))
        attachment.save()
        attachment.refresh_from_db()
        self.assertIsNone(attachment.file_type)


class TaskTypeAdminGenericProtectionTests(TestCase):
    """The built-in 'Generic' TaskType (pk=1) should be uneditable and
    undeletable through the admin."""

    def setUp(self):
        self.admin = TaskTypeAdmin(TaskType, admin.site)
        self.generic = TaskType.objects.get(pk=TaskType.GENERIC_PK)
        self.other = TaskType.objects.create(name="Household")
        superuser = User.objects.create_superuser("root", "", "password")
        self.request = RequestFactory().get("/")
        self.request.user = superuser

    def test_delete_permission_denied_for_generic(self):
        self.assertFalse(self.admin.has_delete_permission(self.request, self.generic))

    def test_delete_permission_allowed_for_other(self):
        self.assertTrue(self.admin.has_delete_permission(self.request, self.other))

    def test_delete_permission_allowed_with_no_object(self):
        self.assertTrue(self.admin.has_delete_permission(self.request, None))

    def test_readonly_fields_locked_for_generic(self):
        readonly_fields = self.admin.get_readonly_fields(None, self.generic)
        self.assertIn("name", readonly_fields)
        self.assertIn("content_type", readonly_fields)

    def test_readonly_fields_unlocked_for_other(self):
        readonly_fields = self.admin.get_readonly_fields(None, self.other)
        self.assertNotIn("name", readonly_fields)
        self.assertNotIn("content_type", readonly_fields)

    def test_readonly_fields_unlocked_for_new_object(self):
        readonly_fields = self.admin.get_readonly_fields(None, None)
        self.assertNotIn("name", readonly_fields)
