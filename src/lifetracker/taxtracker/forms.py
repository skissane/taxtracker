from django import forms

from lifetracker.core.forms import FlexibleDateField
from lifetracker.taxtracker.models import Attachment


class AttachmentForm(forms.ModelForm):
    date = FlexibleDateField(required=False)

    class Meta:
        model = Attachment
        fields = "__all__"


class ImportArchiveForm(forms.Form):
    """Upload form for the "Import Archive" admin view."""

    archive = forms.FileField(
        label="Archive file",
        help_text=("Upload an archive file to extract attachments from. Supported formats: .har, .zip"),
    )


class ReceivedDocumentImportArchiveForm(forms.Form):
    """Upload form for the ReceivedDocument "Import Archive" admin view."""

    archive = forms.FileField(
        label="Archive file",
        help_text=(
            "Upload a ZIP file containing PDF documents. Each PDF's filename must "
            "start with the 4-digit financial year it belongs to (e.g. "
            "'2024-notice-of-assessment.pdf'). PDFs without a 4-digit year prefix, "
            "or whose year has no matching Financial Year record, are skipped."
        ),
    )
