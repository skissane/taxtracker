import datetime
import re
from pathlib import Path

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models

from lifetracker.core.models import FileExtension, FileType, database_storage


class TaskType(models.Model):
    name = models.CharField(max_length=100, unique=True)
    content_type = models.ForeignKey(
        ContentType,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="task_types",
        help_text=(
            "Optional model that tasks of this type may link to. "
            "Leave blank if tasks of this type never link to another object."
        ),
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Task Type"
        verbose_name_plural = "Task Types"

    def __str__(self):
        return self.name


class Task(models.Model):
    STATUS_PENDING = "pending"
    STATUS_DONE = "done"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_DONE, "Done"),
    ]

    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )
    title = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    notes = models.TextField(blank=True)
    order = models.PositiveIntegerField(
        default=0,
        help_text="Order among siblings (lower numbers appear first).",
    )
    task_type = models.ForeignKey(
        TaskType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tasks",
    )
    content_type = models.ForeignKey(
        ContentType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    linked_object = GenericForeignKey("content_type", "object_id")

    class Meta:
        ordering = ["order", "title"]
        verbose_name = "Task"
        verbose_name_plural = "Tasks"

    def __str__(self):
        parts = [self.title]
        visited = {self.pk}
        current = self
        while current.parent_id is not None:
            if current.parent_id in visited:
                parts.insert(0, "…")
                break
            visited.add(current.parent_id)
            current = current.parent
            parts.insert(0, current.title)
        return " > ".join(parts)

    @property
    def is_done(self):
        return self.status == self.STATUS_DONE

    def clean(self):
        skip_cycle_detection = False
        if self.parent_id is not None and self.pk is not None:
            try:
                original_parent_id = (
                    Task.objects.only("parent_id").get(pk=self.pk).parent_id
                )
                if original_parent_id == self.parent_id:
                    skip_cycle_detection = True
            except Task.DoesNotExist:
                pass
            if not skip_cycle_detection:
                visited = {self.pk}
                current_id = self.parent_id
                while current_id is not None:
                    if current_id in visited:
                        raise ValidationError(
                            {
                                "parent": (
                                    "Setting this parent would create a circular"
                                    " reference."
                                )
                            }
                        )
                    visited.add(current_id)
                    try:
                        ancestor = Task.objects.only("parent_id").get(pk=current_id)
                        current_id = ancestor.parent_id
                    except Task.DoesNotExist:
                        break

        if (self.content_type_id is None) != (self.object_id is None):
            raise ValidationError(
                {
                    "content_type": (
                        "Content type and object ID must either both be set or both "
                        "be blank."
                    )
                }
            )

        allowed_content_type_id = None
        if self.task_type_id is not None or self.task_type is not None:
            allowed_content_type_id = self.task_type.content_type_id

        if allowed_content_type_id is None:
            if self.content_type_id is not None or self.object_id is not None:
                raise ValidationError(
                    {
                        "content_type": (
                            "This task type does not allow linking to another object."
                        )
                    }
                )
        elif (
            self.content_type_id is not None
            and self.content_type_id != allowed_content_type_id
        ):
            raise ValidationError(
                {
                    "content_type": (
                        "Content type must match the task type's allowed linked model."
                    )
                }
            )

    def get_folder_path(self):
        """Return a list of titles representing the path from root to this task."""
        path = [self.title]
        visited = {self.pk}
        current = self
        while current.parent_id is not None:
            if current.parent_id in visited:
                break
            visited.add(current.parent_id)
            current = current.parent
            path.insert(0, current.title)
        return path


_DATE_IN_FILENAME_RE = re.compile(r"(?<!\d)(\d{4})-?(\d{2})-?(\d{2})")


def _extract_date_from_filename(name: str) -> datetime.date | None:
    for m in _DATE_IN_FILENAME_RE.finditer(name):
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    return None


class TaskAttachment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attachments")
    title = models.CharField(
        max_length=255,
        blank=True,
        help_text="Leave blank to auto-populate from the uploaded file name.",
    )
    date = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "Optional date for this attachment. "
            "Leave blank to auto-extract from the filename, "
            "or if no date can be found in the filename."
        ),
    )
    notes = models.TextField(blank=True)
    file_type = models.ForeignKey(
        FileType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="task_attachments",
        help_text="Type of the uploaded file.",
    )
    file = models.FileField(storage=database_storage)

    class Meta:
        ordering = ["title"]
        verbose_name = "Task Attachment"
        verbose_name_plural = "Task Attachments"

    def __str__(self):
        if self.title:
            label = self.title
        elif self.file and self.file.name:
            label = Path(self.file.name).name
        else:
            label = "(no file)"
        return f"{label} ({self.task})"

    def save(self, *args, **kwargs):
        if not self.title and self.file:
            self.title = Path(self.file.name).name
        if not self.file_type_id and self.file:
            ext = Path(self.file.name).suffix.lstrip(".").lower()
            if ext:
                try:
                    fe = FileExtension.objects.get(extension=ext)
                    self.file_type = fe.file_type
                except FileExtension.DoesNotExist:
                    pass
        if self.date is None and self.file:
            extracted = _extract_date_from_filename(Path(self.file.name).name)
            if extracted is not None:
                self.date = extracted
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        file_name = self.file.name if self.file else None
        super().delete(*args, **kwargs)
        if file_name:
            self.file.storage.delete(file_name)
