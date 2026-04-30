import datetime
import os
import re
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.db import models
from django.utils import timezone
from django.utils.deconstruct import deconstructible


class FinancialYear(models.Model):
    """Australian financial year (FY) ending June 30 of the given year."""

    year = models.PositiveSmallIntegerField(
        unique=True,
        help_text=(
            "The year in which the FY ends, e.g. 2024 for FY2024 "
            "(1 July 2023 – 30 June 2024)."
        ),
    )
    notes = models.TextField(blank=True)
    lodgement_date_override = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "Optional override of the default lodgement date "
            "(31 October of the ending year), e.g. as advised by a tax agent."
        ),
    )

    class Meta:
        ordering = ["-year"]
        verbose_name = "Financial Year"
        verbose_name_plural = "Financial Years"

    def __str__(self):
        return f"FY{self.year}"

    @property
    def start_date(self):
        return datetime.date(self.year - 1, 7, 1)

    @property
    def end_date(self):
        return datetime.date(self.year, 6, 30)

    @property
    def default_lodgement_date(self):
        return datetime.date(self.year, 10, 31)

    @property
    def effective_lodgement_date(self):
        return self.lodgement_date_override or self.default_lodgement_date

    @property
    def days_until_lodgement(self):
        today = timezone.now().date()
        return (self.effective_lodgement_date - today).days


class Item(models.Model):
    """A tax return checklist item, optionally nested under a parent item."""

    STATUS_PENDING = "pending"
    STATUS_DONE = "done"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_DONE, "Done"),
    ]

    year = models.ForeignKey(
        FinancialYear,
        on_delete=models.CASCADE,
        related_name="items",
    )
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

    class Meta:
        ordering = ["order", "title"]
        verbose_name = "Item"
        verbose_name_plural = "Items"

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
        # Enforce year inheritance: a child item must always share its parent's year.
        if self.parent_id is not None:
            try:
                parent = Item.objects.only("year_id", "parent_id").get(
                    pk=self.parent_id
                )
                self.year_id = parent.year_id
            except Item.DoesNotExist:
                pass

        if self.parent_id is None or self.pk is None:
            return
        # If the parent_id is unchanged from the persisted value, a cycle already
        # exists in the DB and we are not making it worse — skip detection so that
        # existing cycles don't block every subsequent save (e.g. inline formsets
        # re-validate unchanged rows, which would otherwise surface an invisible
        # "Please correct the error below" message when the parent field is hidden).
        try:
            original_parent_id = (
                Item.objects.only("parent_id").get(pk=self.pk).parent_id
            )
            if original_parent_id == self.parent_id:
                return
        except Item.DoesNotExist:
            pass
        # Walk the ancestor chain; if we encounter self, there is a cycle.
        visited = {self.pk}
        current_id = self.parent_id
        while current_id is not None:
            if current_id in visited:
                raise ValidationError(
                    {"parent": "Setting this parent would create a circular reference."}
                )
            visited.add(current_id)
            try:
                ancestor = Item.objects.only("parent_id").get(pk=current_id)
                current_id = ancestor.parent_id
            except Item.DoesNotExist:
                break

    def save(self, *args, **kwargs):
        # Enforce year inheritance at the ORM level too, so direct saves (shell,
        # fixtures, scripts) also respect the invariant without requiring full_clean().
        if self.parent_id is not None:
            try:
                self.year_id = (
                    Item.objects.only("year_id").get(pk=self.parent_id).year_id
                )
            except Item.DoesNotExist:
                pass
        super().save(*args, **kwargs)

    def get_folder_path(self):
        """Return a list of titles representing the path from root to this item."""
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


class FileType(models.Model):
    """A recognised file type with associated MIME types and file extensions."""

    short_name = models.CharField(
        max_length=50,
        unique=True,
        help_text="Short human-readable name, e.g. PDF",
    )
    full_name = models.CharField(
        max_length=255,
        unique=True,
        help_text="Full human-readable name, e.g. PDF Document",
    )

    class Meta:
        ordering = ["short_name"]
        verbose_name = "File Type"
        verbose_name_plural = "File Types"

    def __str__(self):
        return self.short_name


_MIME_TYPE_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9!#$&\-^_]*"
    r"/[a-zA-Z0-9][a-zA-Z0-9!#$&\-^_.+]*$"
)


class MimeType(models.Model):
    """A MIME type associated with a FileType."""

    file_type = models.ForeignKey(
        FileType,
        on_delete=models.CASCADE,
        related_name="mime_types",
    )
    mime_type = models.CharField(
        max_length=255,
        help_text="MIME type string, e.g. application/pdf",
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="Whether this is the primary MIME type for this file type.",
    )

    class Meta:
        ordering = ["-is_primary", "mime_type"]
        verbose_name = "MIME Type"
        verbose_name_plural = "MIME Types"
        constraints = [
            models.UniqueConstraint(
                fields=["mime_type"],
                name="unique_mime_type",
            ),
            models.UniqueConstraint(
                fields=["file_type"],
                condition=models.Q(is_primary=True),
                name="unique_primary_mime_type_per_file_type",
            ),
        ]

    def __str__(self):
        return self.mime_type

    def clean(self):
        if self.mime_type and not _MIME_TYPE_RE.match(self.mime_type):
            raise ValidationError(
                {
                    "mime_type": (
                        "Invalid MIME type. Must be 'type/subtype' using only "
                        "letters, digits and !#$&-^_ (type) or !#$&-^_.+ (subtype)."
                    )
                }
            )


_EXTENSION_INVALID_RE = re.compile(r"[\s./]")


class FileExtension(models.Model):
    """A file extension associated with a FileType."""

    file_type = models.ForeignKey(
        FileType,
        on_delete=models.CASCADE,
        related_name="file_extensions",
    )
    extension = models.CharField(
        max_length=20,
        help_text="Extension without leading dot, e.g. pdf. Always stored lowercase.",
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="Whether this is the primary extension for this file type.",
    )

    class Meta:
        ordering = ["-is_primary", "extension"]
        verbose_name = "File Extension"
        verbose_name_plural = "File Extensions"
        constraints = [
            models.UniqueConstraint(
                fields=["extension"],
                name="unique_file_extension",
            ),
            models.UniqueConstraint(
                fields=["file_type"],
                condition=models.Q(is_primary=True),
                name="unique_primary_extension_per_file_type",
            ),
        ]

    def __str__(self):
        return self.extension

    def clean(self):
        if self.extension:
            self.extension = self.extension.lower()
            if _EXTENSION_INVALID_RE.search(self.extension):
                raise ValidationError(
                    {
                        "extension": (
                            "File extension must not contain whitespace, "
                            "a dot, or a slash."
                        )
                    }
                )

    def save(self, *args, **kwargs):
        if self.extension:
            self.extension = self.extension.lower()
        super().save(*args, **kwargs)


class DBStoredFile(models.Model):
    """Binary file content stored in the database."""

    filename = models.CharField(max_length=255)
    content = models.BinaryField()

    class Meta:
        verbose_name = "Stored File"
        verbose_name_plural = "Stored Files"

    def __str__(self):
        return self.filename


@deconstructible
class DatabaseStorage(Storage):
    """Django storage backend that saves file content in the database."""

    _PREFIX = "db/"

    def _open(self, name, mode="rb"):
        pk = self._name_to_pk(name)
        obj = DBStoredFile.objects.get(pk=pk)
        return ContentFile(bytes(obj.content), name=name)

    def _save(self, name, content):
        filename = os.path.basename(name)
        data = content.read()
        obj = DBStoredFile.objects.create(filename=filename, content=data)
        return f"{self._PREFIX}{obj.pk}/{filename}"

    def exists(self, name):
        try:
            pk = self._name_to_pk(name)
            return DBStoredFile.objects.filter(pk=pk).exists()
        except ValueError:
            return False

    def url(self, name):
        from django.urls import reverse

        pk = self._name_to_pk(name)
        return reverse("admin:tracker_attachment_serve_file", args=[pk])

    def delete(self, name):
        try:
            pk = self._name_to_pk(name)
            DBStoredFile.objects.filter(pk=pk).delete()
        except ValueError:
            pass

    def size(self, name):
        pk = self._name_to_pk(name)
        obj = DBStoredFile.objects.get(pk=pk)
        return len(obj.content)

    def get_available_name(self, name, max_length=None):
        # Each save creates a new record so there are no name conflicts.
        if max_length and len(name) > max_length:
            stem = Path(name).stem
            suffix = Path(name).suffix
            allowed = max_length - len(suffix)
            name = stem[:allowed] + suffix if allowed > 0 else name[:max_length]
        return name

    def _name_to_pk(self, name):
        # Expected format: "db/<pk>/<filename>"
        if not isinstance(name, str):
            raise ValueError(f"Expected str, got {type(name).__name__}: {name!r}")
        parts = name.split("/", 2)
        if len(parts) >= 2 and parts[0] == "db":
            try:
                return int(parts[1])
            except ValueError:
                raise ValueError(f"Invalid database storage path: {name!r}") from None
        raise ValueError(f"Invalid database storage path: {name!r}")


_database_storage = DatabaseStorage()


class Attachment(models.Model):
    """A file attachment associated with an Item."""

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    title = models.CharField(
        max_length=255,
        blank=True,
        help_text="Leave blank to auto-populate from the uploaded file name.",
    )
    notes = models.TextField(blank=True)
    file_type = models.ForeignKey(
        FileType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="attachments",
        help_text="Type of the uploaded file.",
    )
    file = models.FileField(storage=_database_storage)

    class Meta:
        ordering = ["title"]
        verbose_name = "Attachment"
        verbose_name_plural = "Attachments"

    def __str__(self):
        if self.title:
            label = self.title
        elif self.file and self.file.name:
            label = Path(self.file.name).name
        else:
            label = "(no file)"
        return f"{label} ({self.item})"

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
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Delete the stored file from the database when the attachment is removed.
        file_name = self.file.name if self.file else None
        super().delete(*args, **kwargs)
        if file_name:
            self.file.storage.delete(file_name)
