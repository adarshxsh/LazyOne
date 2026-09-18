import os
from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import DisputeEvidence

class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email') # Only show these fields in addition to password fields

class DisputeEvidenceForm(forms.ModelForm):
    class Meta:
        model = DisputeEvidence
        fields = ['file', 'url', 'description']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Optional description or notes about this evidence...'}),
            'url': forms.URLInput(attrs={'placeholder': 'https://example.com/delivery-proof'}),
            'file': forms.FileInput(attrs={'accept': 'image/*,.pdf'}),
        }

    def clean_file(self):
        file = self.cleaned_data.get('file')
        if file:
            # File size restriction: max 10MB
            max_size = 10 * 1024 * 1024
            if file.size > max_size:
                raise forms.ValidationError("File size must be under 10MB.")
            
            # File format restriction: images and PDFs only
            ext = os.path.splitext(file.name)[1].lower()
            allowed_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.pdf']
            if ext not in allowed_extensions:
                raise forms.ValidationError("Invalid file format. File uploads are restricted to image and PDF formats.")
            
            # Additional content_type check if available
            content_type = getattr(file, 'content_type', '')
            if content_type and not (content_type.startswith('image/') or content_type == 'application/pdf'):
                raise forms.ValidationError("Invalid file format. File uploads are restricted to image and PDF formats.")
                
        return file

    def clean(self):
        cleaned_data = super().clean()
        file = cleaned_data.get('file')
        url = cleaned_data.get('url')
        description = cleaned_data.get('description')

        if not file and not url and not description:
            raise forms.ValidationError("Please provide at least a file, URL, or description as evidence.")
        return cleaned_data

