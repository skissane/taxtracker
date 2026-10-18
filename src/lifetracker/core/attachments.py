"""Shared behaviour for every module's attachment models.

An attachment model (e.g. taxtracker's ``Attachment``, tasktracker's
``TaskAttachment``) has ``title``, ``date``, ``file_type`` and ``file``
fields, the file stored via ``database_storage``. Mixing in
``AttachmentAutofillMixin`` fills the blank ones in from the uploaded file's
name on save; ``delete_stored_files`` (connected in ``CoreConfig.ready``)
removes the stored file whenever such a row is deleted.
``orphaned_stored_file_pks`` finds stored files that no row refers to any more.
"""

import datetime
import re
from pathlib import Path

from django.apps import apps
from django.db import models

from lifetracker.core.models import DatabaseStorage, DBStoredFile, FileExtension

# Matches YYYY[-]MM[-]DD where YYYY is not preceded by a digit.
# The separator between year/month and month/day is independently optional.
_DATE_IN_FILENAME_RE = re.compile(r"(?<!\d)(\d{4})-?(\d{2})-?(\d{2})")


def extract_date_from_filename(name: str) -> datetime.date | None:
    """Return the first valid calendar date found in *name*, or ``None``."""
    for m in _DATE_IN_FILENAME_RE.finditer(name):
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    return None


def file_type_for_filename(name):
    """Return the ``FileType`` registered for *name*'s extension, or ``None``."""
    ext = Path(name).suffix.lstrip(".").lower()
    if not ext:
        return None
    fe = FileExtension.objects.select_related("file_type").filter(extension=ext)
    fe = fe.first()
    return fe.file_type if fe else None


class AttachmentAutofillMixin:
    """On save, fill a blank ``title``, ``file_type`` and ``date`` from the
    uploaded file's name. Put it before ``models.Model`` in the bases."""

    def save(self, *args, **kwargs):
        if self.file:
            name = Path(self.file.name).name
            if not self.title:
                self.title = name
            if not self.file_type_id:
                self.file_type = file_type_for_filename(name)
            if self.date is None:
                self.date = extract_date_from_filename(name)
        super().save(*args, **kwargs)


def database_storage_file_fields(model):
    """Return *model*'s ``FileField``s whose storage is a ``DatabaseStorage``."""
    return [
        field
        for field in model._meta.concrete_fields
        if isinstance(field, models.FileField)
        and isinstance(field.storage, DatabaseStorage)
    ]


def delete_stored_files(sender, instance, **kwargs):
    """``post_delete`` receiver: delete the row's files from the database.

    A signal rather than a ``delete()`` override, so the files also go when
    the row is deleted by a cascade (e.g. deleting its Item or Task) or a
    ``QuerySet.delete()`` (e.g. the admin's "Delete selected" action), neither
    of which calls ``Model.delete()``.
    """
    for field in database_storage_file_fields(sender):
        file = getattr(instance, field.attname)
        if file:
            file.storage.delete(file.name)


def orphaned_stored_file_pks():
    """Return the sorted primary keys of the ``DBStoredFile``s that no
    ``DatabaseStorage`` ``FileField`` on any installed model refers to.

    Rows deleted before ``delete_stored_files`` was connected (by a cascade or
    a ``QuerySet.delete()``) left their files behind; this finds them.
    """
    referenced = set()
    for model in apps.get_models():
        for field in database_storage_file_fields(model):
            names = (
                model._base_manager.exclude(**{field.attname: ""})
                .exclude(**{f"{field.attname}__isnull": True})
                .values_list(field.attname, flat=True)
                .iterator()
            )
            for name in names:
                try:
                    referenced.add(field.storage._name_to_pk(name))
                except ValueError:
                    pass
    return [
        pk
        for pk in DBStoredFile.objects.order_by("pk").values_list("pk", flat=True)
        if pk not in referenced
    ]
