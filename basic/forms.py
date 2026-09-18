from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import Dispute

class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email') # Only show these fields in addition to password fields


class DisputeForm(forms.ModelForm):
    class Meta:
        model = Dispute
        fields = ['category', 'reason', 'evidence_url', 'evidence_details']

    def clean_category(self):
        category = self.cleaned_data.get('category')
        if not category:
            raise forms.ValidationError("Category selection is required.")
        valid_categories = [choice[0] for choice in Dispute.CATEGORY_CHOICES]
        if category not in valid_categories:
            raise forms.ValidationError("Select a valid dispute category.")
        return category

    def clean_reason(self):
        reason = self.cleaned_data.get('reason')
        if not reason or not reason.strip():
            raise forms.ValidationError("A reason is required to raise a dispute.")
        return reason.strip()

    def clean_evidence_details(self):
        details = self.cleaned_data.get('evidence_details')
        if not details or not details.strip():
            raise forms.ValidationError("Evidence details are required.")
        if len(details.strip()) < 20:
            raise forms.ValidationError("Evidence details must be at least 20 characters long.")
        return details.strip()
