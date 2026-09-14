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
        fields = ['description', 'file']
        widgets = {
            'description': forms.Textarea(attrs={
                'rows': 3,
                'class': 'w-full px-4 py-2 bg-gray-800 text-gray-100 rounded-lg border border-gray-700 focus:outline-none focus:border-cyan-500 text-sm',
                'placeholder': 'Describe your evidence or reason...'
            }),
            'file': forms.FileInput(attrs={
                'class': 'w-full text-sm text-gray-400 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-cyan-600 file:text-white hover:file:bg-cyan-700 cursor-pointer',
                'accept': '.jpg,.jpeg,.png,.pdf'
            })
        }

