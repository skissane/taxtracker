import datetime
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from lifetracker.core.attachments import AttachmentAutofillMixin
from lifetracker.core.models import FileType, database_storage
from lifetracker.core.trees import TreeNodeMixin


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
        help_text=("The year in which the FY ends, e.g. 2024 for FY2024 (1 July 2023 – 30 June 2024)."),
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
    def from_db(cls, db, field_names, values, *, fetch_mode=None):
        instance = super().from_db(db, field_names, values, fetch_mode=fetch_mode)
        instance._loaded_status = instance.status
        return instance

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        if fields is None or "status" in fields:
            self._loaded_status = self.status

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
        today = timezone.localdate()
        return (self.effective_lodgement_date - today).days

    @property
    def current_status_transitioned_at(self):
        if hasattr(self, "_prefetched_objects_cache") and "status_history" in self._prefetched_objects_cache:
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
        latest = self.status_history.filter(to_status=self.status).order_by("-transitioned_at", "-pk").first()
        return latest.transitioned_at if latest else None

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        status_being_saved = update_fields is None or "status" in update_fields
        previous_status = None
        if not self._state.adding and status_being_saved:
            previous_status = getattr(self, "_loaded_status", None)
            if previous_status is None and self.pk is not None:
                previous_status = FinancialYear.objects.only("status").get(pk=self.pk).status
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
        return f"{self.financial_year}: {self.get_to_status_display()} @ {self.transitioned_at}"


class Item(TreeNodeMixin, models.Model):
    """A tax return checklist item, optionally nested under a parent item."""

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
        choices=TreeNodeMixin.STATUS_CHOICES,
        default=TreeNodeMixin.STATUS_PENDING,
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
        return f"{self.year} > {self.tree_path_str()}"

    def clean(self):
        if self.parent_id is not None:
            # Enforce year inheritance: child items always share their parent's year.
            try:
                parent = Item.objects.only("year_id").get(pk=self.parent_id)
                self.year_id = parent.year_id
            except Item.DoesNotExist:
                pass
        self.check_parent_cycle()

    def save(self, *args, **kwargs):
        old_year_id = None
        if self.pk is not None:
            old_year_id = (
                Item.objects.filter(pk=self.pk)
                .values_list(
                    "year_id",
                    flat=True,
                )
                .first()
            )

        # Enforce year inheritance at the ORM level too, so direct saves (shell,
        # fixtures, scripts) also respect the invariant without requiring full_clean().
        if self.parent_id is not None:
            try:
                self.year_id = Item.objects.only("year_id").get(pk=self.parent_id).year_id
            except Item.DoesNotExist:
                pass
        super().save(*args, **kwargs)

        if old_year_id is not None and self.year_id != old_year_id:
            # Keep descendants in the same year as this item whenever this item's
            # effective year changes (e.g. explicit root edit or re-parenting).
            ids_to_update = []
            current_level = [self.pk]
            while current_level:
                children = list(Item.objects.filter(parent_id__in=current_level).values_list("pk", flat=True))
                ids_to_update.extend(children)
                current_level = children
            if ids_to_update:
                Item.objects.filter(pk__in=ids_to_update).update(year_id=self.year_id)


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


class Attachment(AttachmentAutofillMixin, models.Model):
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
        has_received_document = self.received_document_id is not None or self.received_document is not None
        if has_item == has_received_document:
            raise ValidationError(
                "Attachment must belong to exactly one of Item or Received Document, not both or neither."
            )
