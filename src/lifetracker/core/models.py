import os
import re
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.db import models
from django.urls import reverse
from django.utils.deconstruct import deconstructible


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
        pk = self._name_to_pk(name)
        return reverse("admin:core_dbstoredfile_serve_file", args=[pk])

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


database_storage = DatabaseStorage()


# The only allowed primary key for CloudBackupStatus, which is a single row.
CLOUD_BACKUP_STATUS_PK = 1


class IdleWatchdogStatus(models.Model):
    """No table: exists only so the admin index has an entry (under Core) for
    the idle watchdog's status page, which ``IdleWatchdogStatusAdmin`` serves
    from the watchdog's status file (see ``core/idle_watchdog.py``)."""

    class Meta:
        managed = False
        verbose_name = "Idle Watchdog"
        verbose_name_plural = "Idle Watchdog"
        default_permissions = ()


class CloudBackupStatus(models.Model):
    """Tracks the latest successful cloud backup upload."""

    last_backup_at = models.DateTimeField(null=True, blank=True)
    # 1024 is S3's maximum object key length.
    last_backup_object_key = models.CharField(max_length=1024, blank=True)
    last_backup_size = models.PositiveBigIntegerField(
        null=True, blank=True, help_text="Size of the last backup, in bytes."
    )

    class Meta:
        verbose_name = "Cloud Backup Status"
        # Shown in the admin index, where the entry leads to the backups page.
        verbose_name_plural = "Backups"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(pk=CLOUD_BACKUP_STATUS_PK),
                name="cloudbackupstatus_singleton",
            ),
        ]

    def __str__(self):
        if self.last_backup_at is None:
            return "Cloud backup status: never backed up"
        return f"Cloud backup status: last backup at {self.last_backup_at}"
