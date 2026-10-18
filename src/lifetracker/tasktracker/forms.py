from django import forms

from lifetracker.core.forms import FlexibleDateField
from lifetracker.tasktracker.models import TaskAttachment


class TaskAttachmentForm(forms.ModelForm):
    date = FlexibleDateField(required=False)

    class Meta:
        model = TaskAttachment
        fields = "__all__"
