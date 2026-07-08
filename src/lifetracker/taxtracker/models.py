import datetime
import re
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from lifetracker.core.models import FileExtension, FileType, database_storage


class FinancialYear(models.Model):
    """Australian financial year (FY) ending June 30 of the given year."""

    STATUS_PENDING_SUBMISSION = "pending_submission"
    STATUS_SUBMITTED = "submitted"
    STATUS_MORE_INFO_REQUESTED = "more_info_requested"
    STATUS_FINALISED = "finalised"
    STATUS_CHOICES = [
        (STATUS_PENDING_SUBMISSION, "Pending submission to tax agent"),
        (STATUS_SUBMITTED, "Submitted to tax agent"),
        (STATUS_MORE_INFO_REQUESTED, "Tax agent requests more info"),
        (STATUS_FINALISED, "Finalised"),
    ]

    year = models.PositiveSmallIntegerField(
        unique=True,
        help_text=(
            "The year in which the FY ends, e.g. 2024 for FY2024 "
            "(1 July 2023 – 30 June 2024)."
        ),
    )
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING_SUBMISSION,
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

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_status = instance.status
        return instance

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

    @property
    def current_status_transitioned_at(self):
        if (
            hasattr(self, "_prefetched_objects_cache")
            and "status_history" in self._prefetched_objects_cache
        ):
            matching = [
                history
                for history in self._prefetched_objects_cache["status_history"]
                if history.to_status == self.status
            ]
            if not matching:
                return None
            return max(
                matching,
                key=lambda history: (history.transitioned_at, history.pk),
            ).transitioned_at
        latest = (
            self.status_history.filter(to_status=self.status)
            .order_by("-transitioned_at", "-pk")
            .first()
        )
        return latest.transitioned_at if latest else None

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        status_being_saved = update_fields is None or "status" in update_fields
        previous_status = None
        if not self._state.adding and status_being_saved:
            previous_status = getattr(self, "_loaded_status", None)
            if previous_status is None and self.pk is not None:
                previous_status = (
                    FinancialYear.objects.only("status").get(pk=self.pk).status
                )
        super().save(*args, **kwargs)
        if status_being_saved:
            if previous_status != self.status:
                FinancialYearStatusHistory.objects.create(
                    financial_year=self,
                    from_status=previous_status,
                    to_status=self.status,
                )
            self._loaded_status = self.status


class FinancialYearStatusHistory(models.Model):
    """History of financial year status transitions."""

    financial_year = models.ForeignKey(
        FinancialYear,
        on_delete=models.CASCADE,
        related_name="status_history",
    )
    from_status = models.CharField(
        max_length=30,
        choices=FinancialYear.STATUS_CHOICES,
        null=True,
        blank=True,
    )
    to_status = models.CharField(
        max_length=30,
        choices=FinancialYear.STATUS_CHOICES,
    )
    transitioned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-transitioned_at", "-pk"]
        verbose_name = "Financial Year Status Transition"
        verbose_name_plural = "Financial Year Status Transitions"

    def __str__(self):
        if self.from_status:
            return (
                f"{self.financial_year}: {self.get_from_status_display()} → "
                f"{self.get_to_status_display()} @ {self.transitioned_at}"
            )
        return (
            f"{self.financial_year}: {self.get_to_status_display()} @ "
            f"{self.transitioned_at}"
        )


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
        parts.insert(0, str(self.year))
        return " > ".join(parts)

    @property
    def is_done(self):
        return self.status == self.STATUS_DONE

    def clean(self):
        if self.parent_id is not None:
            # Enforce year inheritance: child items always share their parent's year.
            try:
                parent = Item.objects.only("year_id").get(pk=self.parent_id)
                self.year_id = parent.year_id
            except Item.DoesNotExist:
                pass

            # Cycle detection only makes sense for existing items with a parent.
            if self.pk is not None:
                # If the parent_id is unchanged from the persisted value, a cycle
                # already exists in the DB and we are not making it worse — skip
                # detection so that existing cycles don't block every subsequent save
                # (e.g. inline formsets re-validate unchanged rows, which would
                # otherwise surface an invisible "Please correct the error below"
                # message when the parent field is hidden).
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
                            {
                                "parent": (
                                    "Setting this parent would create a circular"
                                    " reference."
                                )
                            }
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


class ReceivedDocument(models.Model):
    """A document received back from the tax agent/tax office for a financial year."""

    year = models.ForeignKey(
        FinancialYear,
        on_delete=models.CASCADE,
        related_name="received_documents",
    )
    title = models.CharField(max_length=255)
    date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-date", "title"]
        verbose_name = "Received Document"
        verbose_name_plural = "Received Documents"

    def __str__(self):
        return f"{self.year}: {self.title}"


# Matches YYYY[-]MM[-]DD where YYYY is not preceded by a digit.
# The separator between year/month and month/day is independently optional.
_DATE_IN_FILENAME_RE = re.compile(r"(?<!\d)(\d{4})-?(\d{2})-?(\d{2})")


def _extract_date_from_filename(name: str) -> datetime.date | None:
    """Return the first valid calendar date found in *name*, or ``None``."""
    for m in _DATE_IN_FILENAME_RE.finditer(name):
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    return None


class Attachment(models.Model):
    """A file attachment associated with an Item or a ReceivedDocument."""

    item = models.ForeignKey(
        Item,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    received_document = models.ForeignKey(
        ReceivedDocument,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
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
        related_name="attachments",
        help_text="Type of the uploaded file.",
    )
    file = models.FileField(storage=database_storage)

    class Meta:
        ordering = ["title"]
        verbose_name = "Attachment"
        verbose_name_plural = "Attachments"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(item__isnull=False, received_document__isnull=True)
                    | models.Q(item__isnull=True, received_document__isnull=False)
                ),
                name="attachment_exactly_one_parent",
            ),
        ]

    def __str__(self):
        if self.title:
            label = self.title
        elif self.file and self.file.name:
            label = Path(self.file.name).name
        else:
            label = "(no file)"
        parent = self.item or self.received_document
        return f"{label} ({parent})"

    def clean(self):
        # Check the cached related object too, not just the _id attname: when
        # an Attachment is added inline alongside a brand-new (unsaved) parent,
        # the parent's pk (and hence the child's _id) is still None at
        # validation time even though the in-memory relation is set — the FK
        # id is only populated once the parent is actually saved.
        has_item = self.item_id is not None or self.item is not None
        has_received_document = (
            self.received_document_id is not None or self.received_document is not None
        )
        if has_item == has_received_document:
            raise ValidationError(
                "Attachment must belong to exactly one of Item or Received "
                "Document, not both or neither."
            )

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
        # Delete the stored file from the database when the attachment is removed.
        file_name = self.file.name if self.file else None
        super().delete(*args, **kwargs)
        if file_name:
            self.file.storage.delete(file_name)
