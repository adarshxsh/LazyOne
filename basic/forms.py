import json
from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from django.utils.html import escape
from .models import Dispute

class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email') # Only show these fields in addition to password fields


def validate_dispute_input(raw_data):
    """
    Validates dispute submission parameters (category, reason, evidence_payload).
    Returns (cleaned_data_dict, list_of_error_messages).
    """
    errors = []

    # 1. Category validation
    category = raw_data.get('category')
    valid_categories = [c[0] for c in Dispute.CATEGORY_CHOICES]
    if not category or category not in valid_categories:
        errors.append("Please select a valid dispute category.")
        return None, errors

    # 2. Reason validation
    reason = raw_data.get('reason', '')
    if isinstance(reason, str):
        reason = reason.strip()
    else:
        reason = str(reason or '').strip()

    if not reason:
        errors.append("A reason is required to raise a dispute.")

    sanitized_reason = escape(reason)

    # 3. Evidence payload extraction
    raw_payload = raw_data.get('evidence_payload')
    payload = {}

    if isinstance(raw_payload, str):
        try:
            raw_payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            raw_payload = {}
    elif not isinstance(raw_payload, dict):
        raw_payload = {}

    # Merge non-core top-level keys into raw_payload
    excluded_keys = {'category', 'reason', 'csrfmiddlewaretoken', 'evidence_payload'}
    for k, v in raw_data.items():
        if k not in excluded_keys and v is not None and str(v).strip():
            if k not in raw_payload:
                raw_payload[k] = v

    category_rules = {
        'non_completion': {
            'required': ['proof_url', 'work_submission_timestamp', 'description'],
            'labels': {
                'proof_url': 'Proof Image / Document URL',
                'work_submission_timestamp': 'Work Submission Timestamp',
                'description': 'Description'
            }
        },
        'quality_issue': {
            'required': ['proof_url', 'issue_description'],
            'labels': {
                'proof_url': 'Proof Image URL',
                'issue_description': 'Issue Description'
            }
        },
        'payment_dispute': {
            'required': ['proof_url', 'communication_summary'],
            'labels': {
                'proof_url': 'Proof / Receipt URL',
                'communication_summary': 'Communication Summary'
            }
        },
        'communication_failure': {
            'required': ['communication_summary', 'last_contact_date'],
            'labels': {
                'communication_summary': 'Communication Summary',
                'last_contact_date': 'Last Contact Date'
            }
        },
        'other': {
            'required': ['description'],
            'labels': {
                'description': 'Description'
            }
        }
    }

    rule = category_rules.get(category, {'required': [], 'labels': {}})
    required_fields = rule.get('required', [])
    labels = rule.get('labels', {})

    url_validator = URLValidator()

    # Verify required fields
    for field in required_fields:
        val = raw_payload.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            label = labels.get(field, field.replace('_', ' ').title())
            category_label = dict(Dispute.CATEGORY_CHOICES).get(category, category)
            errors.append(f"'{label}' is required for category '{category_label}'.")

    # Validate URL fields and sanitize text values
    for field, val in raw_payload.items():
        if val is None:
            continue
        val_str = str(val).strip() if not isinstance(val, (dict, list)) else val
        if isinstance(val_str, str) and val_str:
            if field == 'proof_url' or field.endswith('_url'):
                try:
                    url_validator(val_str)
                except ValidationError:
                    label = labels.get(field, field.replace('_', ' ').title())
                    errors.append(f"Invalid URL format for '{label}'.")
                    continue
            payload[field] = escape(val_str)

    if errors:
        return None, errors

    return {
        'category': category,
        'reason': sanitized_reason,
        'evidence_payload': payload
    }, []


class DisputeForm(forms.ModelForm):
    category = forms.ChoiceField(choices=Dispute.CATEGORY_CHOICES, required=True)
    reason = forms.CharField(widget=forms.Textarea, required=True)

    class Meta:
        model = Dispute
        fields = ['category', 'reason']

    def clean(self):
        cleaned_data = super().clean()
        result, errors = validate_dispute_input(self.data)
        if errors:
            for err in errors:
                raise forms.ValidationError(err)
        if result:
            cleaned_data['category'] = result['category']
            cleaned_data['reason'] = result['reason']
            cleaned_data['evidence_payload'] = result['evidence_payload']
        return cleaned_data
