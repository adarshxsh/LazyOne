import os
from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from .models import DisputeEvidence

ALLOWED_EVIDENCE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.pdf', '.txt', '.doc', '.docx', '.zip']
MAX_EVIDENCE_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

def validate_evidence_file(file):
    if file.size > MAX_EVIDENCE_FILE_SIZE:
        raise ValidationError("File size exceeds the maximum limit of 10MB.")
    ext = os.path.splitext(file.name)[1].lower()
    if ext not in ALLOWED_EVIDENCE_EXTENSIONS:
        raise ValidationError(f"File extension '{ext}' is not allowed. Allowed extensions: {', '.join(ALLOWED_EVIDENCE_EXTENSIONS)}.")

class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email') # Only show these fields in addition to password fields

class DisputeEvidenceForm(forms.ModelForm):
    class Meta:
        model = DisputeEvidence
        fields = ['file', 'description']
        widgets = {
            'description': forms.TextInput(attrs={
                'class': 'w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-cyan-500',
                'placeholder': 'Optional description of this evidence...'
            }),
            'file': forms.FileInput(attrs={
                'class': 'w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-cyan-500'
            })
        }

    def clean_file(self):
        file = self.cleaned_data.get('file')
        if file:
            validate_evidence_file(file)
        return file
