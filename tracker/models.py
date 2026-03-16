import datetime

from django.db import models
from django.utils import timezone


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
        if self.parent:
            return f"{self.parent} > {self.title}"
        return self.title

    @property
    def is_done(self):
        return self.status == self.STATUS_DONE

    def get_folder_path(self):
        """Return a list of titles representing the path from root to this item."""
        path = [self.title]
        current = self
        while current.parent_id is not None:
            current = current.parent
            path.insert(0, current.title)
        return path


class Attachment(models.Model):
    """A file attachment associated with an Item."""

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    title = models.CharField(max_length=255)
    notes = models.TextField(blank=True)
    file_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="e.g. PDF, Word, CSV, Image",
    )
    file = models.FileField(upload_to="attachments/%Y/")

    class Meta:
        ordering = ["title"]
        verbose_name = "Attachment"
        verbose_name_plural = "Attachments"

    def __str__(self):
        return f"{self.title} ({self.item})"
