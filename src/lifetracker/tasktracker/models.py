from pathlib import Path

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models

from lifetracker.core.attachments import AttachmentAutofillMixin
from lifetracker.core.models import FileType, database_storage
from lifetracker.core.trees import TreeNodeMixin

# The pk of the built-in "Generic" task type, created by a data migration.
# Its name and (lack of) content_type are pinned by both a DB-level check
# constraint and TaskType.clean() below, and it can never be deleted (see
# TaskType.delete()) — it exists as the default task_type for new tasks.
# Also exposed as TaskType.GENERIC_PK; defined at module level because a
# Meta class body can't see its enclosing class's namespace.
_GENERIC_TASK_TYPE_PK = 1


class TaskType(models.Model):
    GENERIC_PK = _GENERIC_TASK_TYPE_PK

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
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(pk=_GENERIC_TASK_TYPE_PK, name="Generic")
                    | (~models.Q(pk=_GENERIC_TASK_TYPE_PK) & ~models.Q(name="Generic"))
                ),
                name="tasktype_pk1_iff_name_generic",
                violation_error_message=(
                    "Task type id=1 must be named 'Generic', and no other "
                    "task type may be named 'Generic'."
                ),
            ),
            models.CheckConstraint(
                condition=~models.Q(pk=_GENERIC_TASK_TYPE_PK)
                | models.Q(content_type__isnull=True),
                name="tasktype_pk1_has_no_content_type",
                violation_error_message=(
                    "The built-in 'Generic' task type cannot be linked to a model."
                ),
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        is_generic_pk = self.pk == self.GENERIC_PK
        is_generic_name = self.name == "Generic"
        if is_generic_pk != is_generic_name:
            raise ValidationError(
                {
                    "name": (
                        "Task type id=1 must be named 'Generic', and no other "
                        "task type may be named 'Generic'."
                    )
                }
            )
        if is_generic_pk and self.content_type_id is not None:
            raise ValidationError(
                {
                    "content_type": (
                        "The built-in 'Generic' task type cannot be linked to a model."
                    )
                }
            )

    def delete(self, *args, **kwargs):
        if self.pk == self.GENERIC_PK:
            raise ValidationError("The built-in 'Generic' task type cannot be deleted.")
        super().delete(*args, **kwargs)


class Task(TreeNodeMixin, models.Model):
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
        choices=TreeNodeMixin.STATUS_CHOICES,
        default=TreeNodeMixin.STATUS_PENDING,
    )
    notes = models.TextField(blank=True)
    order = models.PositiveIntegerField(
        default=0,
        help_text="Order among siblings (lower numbers appear first).",
    )
    task_type = models.ForeignKey(
        TaskType,
        default=TaskType.GENERIC_PK,
        on_delete=models.PROTECT,
        related_name="tasks",
    )
    content_type = models.ForeignKey(
        ContentType,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    linked_object = GenericForeignKey("content_type", "object_id")

    class Meta:
        ordering = ["order", "title"]
        verbose_name = "Task"
        verbose_name_plural = "Tasks"

    def __str__(self):
        return self.tree_path_str()

    def clean(self):
        self.check_parent_cycle()

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
        if self.task_type_id is not None:
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


class TaskAttachment(AttachmentAutofillMixin, models.Model):
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
