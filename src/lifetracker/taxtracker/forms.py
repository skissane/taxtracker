from django import forms
from django.utils import formats

from .models import Attachment

# These English month-name formats are not in the en-AU locale defaults but are
# natural ways to type a date in English (e.g. in a PDF statement heading).
_EXTRA_DATE_FORMATS = [
    "%d %B %Y",  # "20 July 2023"
    "%B %d, %Y",  # "June 30, 2023"
    "%d %b %Y",  # "20 Jul 2023"
    "%b %d, %Y",  # "Jun 30, 2023"
    "%B %d %Y",  # "June 30 2023"
    "%d %b, %Y",  # "20 Jul, 2023"
]


class FlexibleDateField(forms.DateField):
    """DateField that also accepts common English month-name date formats."""

    def __init__(self, *args, **kwargs):
        if "input_formats" not in kwargs:
            locale_formats = list(formats.get_format("DATE_INPUT_FORMATS"))
            extra = [f for f in _EXTRA_DATE_FORMATS if f not in locale_formats]
            kwargs["input_formats"] = extra + locale_formats
        super().__init__(*args, **kwargs)


class AttachmentForm(forms.ModelForm):
    date = FlexibleDateField(required=False)

    class Meta:
        model = Attachment
        fields = "__all__"


class ImportArchiveForm(forms.Form):
    """Upload form for the "Import Archive" admin view."""

    archive = forms.FileField(
        label="Archive file",
        help_text=(
            "Upload an archive file to extract attachments from. "
            "Supported formats: .har, .zip"
        ),
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
