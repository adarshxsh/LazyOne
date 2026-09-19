from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import DisputeEvidence, validate_evidence_file

class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email') # Only show these fields in addition to password fields

class DisputeEvidenceForm(forms.ModelForm):
    class Meta:
        model = DisputeEvidence
        fields = ['comment', 'file']
        widgets = {
            'comment': forms.Textarea(attrs={
                'rows': 3,
                'placeholder': 'Provide additional details or counter-evidence...',
                'class': 'w-full bg-gray-900 border border-gray-700 text-white rounded-lg p-3 focus:outline-none focus:border-cyan-500'
            }),
            'file': forms.FileInput(attrs={
                'class': 'w-full text-gray-300 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-cyan-600 file:text-white hover:file:bg-cyan-700'
            }),
        }

    def clean(self):
        cleaned_data = super().clean()
        comment = cleaned_data.get('comment')
        file = cleaned_data.get('file')

        if not comment and not file:
            raise forms.ValidationError("Please provide either a comment or a file attachment as evidence.")

        if file:
            validate_evidence_file(file)

        return cleaned_data
