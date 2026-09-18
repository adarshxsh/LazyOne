import json
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation
from .forms import validate_dispute_input, DisputeForm


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})[0]
        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 100})[0]
        self.taker_profile.rewards = 100
        self.taker_profile.save()

        # Create task: reward = 300, 20% = 60 (> 50 minimum)
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Create small reward task: reward = 100, 20% = 20 (min 50 applies)
        self.small_task = Task.objects.create(
            title="Small Task",
            description="Small Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.small_task)

    def test_deposit_bond_calculation(self):
        # 20% of 300 = 60 (> 50)
        self.assertEqual(self.task.deposit_bond_amount, 60)
        # 20% of 100 = 20 (< 50, so minimum 50 applies)
        self.assertEqual(self.small_task.deposit_bond_amount, 50)

    def test_raise_dispute_insufficient_rewards(self):
        # Set taker rewards to 30 (less than 60 required)
        self.taker_profile.rewards = 30
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'category': 'other',
                'reason': 'Work not clear',
                'description': 'Details provided'
            }
        )

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

        # Balance should remain unchanged
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30)

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'category': 'other',
                'reason': 'Unreasonable request',
                'description': 'Details provided'
            }
        )

        # Deposit bond is 60. Taker balance was 100 -> now 40
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)

        # Check ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_withdraw_dispute_success(self):
        # First raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'category': 'other',
                'reason': 'Dispute reason',
                'description': 'Details provided'
            }
        )

        dispute = Dispute.objects.get(task=self.task)

        # Withdraw dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Balance restored: 40 + 60 = 100
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        # Check refund ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 60)

    def test_complete_disputed_task_refunds_deposit(self):
        # Taker raises dispute (deposit 60 deducted from 100 -> 40 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'category': 'other',
                'reason': 'Dispute reason',
                'description': 'Details provided'
            }
        )

        # Poster marks task as completed
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Taker balance: 40 + 300 (task reward) + 60 (deposit refund) = 400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

        # Check ledger entries for taker
        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 60)

    def test_forfeit_deposit_method(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='False dispute',
            deposit_amount=60,
            escrow_status='held'
        )
        self.taker_profile.rewards = 40
        self.taker_profile.save()

        # Forfeit deposit bond to poster
        dispute.forfeit_deposit(beneficiary=self.poster)

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'forfeited')

        # Taker rewards remain 40 (already deducted when raised)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        # Poster gets 1000 + 60 = 1060
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

        # Check forfeit ledger
        forfeit_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_forfeit').first()
        self.assertIsNotNone(forfeit_ledger)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DisputeCategoryAndEvidenceTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.unrelated_user = User.objects.create_user(username='otheruser', password='password123')

        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)
        UserProfile.objects.get_or_create(user=self.unrelated_user)

        self.task = Task.objects.create(
            title='Test Painting Task',
            description='Paint a fence',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

        self.client = Client()

    def test_dispute_model_category_and_evidence(self):
        """Verify Dispute model fields and properties."""
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Work completed but poster refuses to acknowledge',
            category='non_completion',
            evidence_payload={
                'proof_url': 'https://example.com/proof.png',
                'work_submission_timestamp': '2026-09-16 12:00',
                'description': 'Submitted via portal on time'
            }
        )

        self.assertEqual(dispute.category, 'non_completion')
        self.assertEqual(dispute.category_display, 'Non-Completion')
        self.assertIn('bg-red-500/20', dispute.category_badge_class)

        formatted = dispute.formatted_evidence
        self.assertEqual(len(formatted), 3)
        proof_item = next(item for item in formatted if item['key'] == 'proof_url')
        self.assertTrue(proof_item['is_url'])
        self.assertEqual(proof_item['value'], 'https://example.com/proof.png')
        self.assertEqual(proof_item['label'], 'Proof Url')

    def test_legacy_dispute_fallback_properties(self):
        """Verify legacy disputes without category or evidence payload handle fallbacks safely."""
        legacy_dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Old dispute without category'
        )

        self.assertEqual(legacy_dispute.category_display, 'Other')
        self.assertEqual(legacy_dispute.formatted_evidence, [])

    def test_validate_dispute_input_success(self):
        """Test validate_dispute_input helper with valid input data."""
        raw_data = {
            'category': 'quality_issue',
            'reason': 'Poor quality work delivered',
            'proof_url': 'https://example.com/image.jpg',
            'issue_description': 'Colors do not match guidelines'
        }

        cleaned, errors = validate_dispute_input(raw_data)
        self.assertEqual(errors, [])
        self.assertEqual(cleaned['category'], 'quality_issue')
        self.assertEqual(cleaned['reason'], 'Poor quality work delivered')
        self.assertEqual(cleaned['evidence_payload']['proof_url'], 'https://example.com/image.jpg')
        self.assertEqual(cleaned['evidence_payload']['issue_description'], 'Colors do not match guidelines')

    def test_validate_dispute_input_missing_category(self):
        """Test validate_dispute_input raises error when category is missing."""
        raw_data = {
            'reason': 'No category provided',
            'proof_url': 'https://example.com/image.jpg'
        }

        cleaned, errors = validate_dispute_input(raw_data)
        self.assertIsNone(cleaned)
        self.assertIn('Please select a valid dispute category.', errors)

    def test_validate_dispute_input_missing_required_evidence(self):
        """Test validate_dispute_input raises error when required category evidence field is missing."""
        raw_data = {
            'category': 'payment_dispute',
            'reason': 'Payment was not released',
            # missing required communication_summary for payment_dispute
            'proof_url': 'https://example.com/receipt.png'
        }

        cleaned, errors = validate_dispute_input(raw_data)
        self.assertIsNone(cleaned)
        self.assertTrue(any("required for category 'Payment Dispute'" in err for err in errors))

    def test_validate_dispute_input_invalid_url(self):
        """Test validate_dispute_input raises error when proof_url is malformed."""
        raw_data = {
            'category': 'other',
            'reason': 'Other dispute reason',
            'description': 'Details provided',
            'proof_url': 'not-a-valid-url'
        }

        cleaned, errors = validate_dispute_input(raw_data)
        self.assertIsNone(cleaned)
        self.assertTrue(any("Invalid URL format" in err for err in errors))

    def test_validate_dispute_input_xss_sanitization(self):
        """Test validate_dispute_input sanitizes HTML in text fields."""
        raw_data = {
            'category': 'other',
            'reason': 'Reason with <script>alert("XSS")</script>',
            'description': 'Details with <b>HTML</b>'
        }

        cleaned, errors = validate_dispute_input(raw_data)
        self.assertEqual(errors, [])
        self.assertNotIn('<script>', cleaned['reason'])
        self.assertIn('&lt;script&gt;', cleaned['reason'])
        self.assertNotIn('<b>', cleaned['evidence_payload']['description'])

    def test_raise_dispute_view_success(self):
        """Test raise_dispute view successfully creates categorized dispute."""
        self.client.login(username='taker', password='password123')
        url = reverse('raise_dispute', args=[self.task.id])

        post_data = {
            'category': 'communication_failure',
            'reason': 'Poster has stopped responding',
            'communication_summary': 'Sent 5 messages over 3 days without response',
            'last_contact_date': '2026-09-15 10:00 AM',
            'proof_url': 'https://example.com/chat.png'
        }

        response = self.client.post(url, post_data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.category, 'communication_failure')
        self.assertEqual(dispute.evidence_payload['communication_summary'], 'Sent 5 messages over 3 days without response')
        self.assertEqual(dispute.evidence_payload['last_contact_date'], '2026-09-15 10:00 AM')

        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

    def test_raise_dispute_view_validation_failure(self):
        """Test raise_dispute view redirects with error when validation fails."""
        self.client.login(username='taker', password='password123')
        url = reverse('raise_dispute', args=[self.task.id])

        # Missing category
        post_data = {
            'reason': 'Reason without category'
        }

        response = self.client.post(url, post_data)
        self.assertEqual(Dispute.objects.filter(task=self.task).count(), 0)
        self.assertRedirects(response, reverse('my_tasks'))

    def test_dispute_detail_view_renders_category_and_evidence(self):
        """Test dispute_detail template renders category badge and evidence table."""
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Task payment unresolved',
            category='payment_dispute',
            evidence_payload={
                'proof_url': 'https://example.com/receipt.png',
                'communication_summary': 'Agreed on 100 points balance'
            }
        )

        self.client.login(username='taker', password='password123')
        url = reverse('dispute_detail', args=[dispute.id])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Payment Dispute')
        self.assertContains(response, 'Submitted Evidence')
        self.assertContains(response, 'https://example.com/receipt.png')
        self.assertContains(response, 'Agreed on 100 points balance')

    def test_dispute_detail_view_legacy_fallback_rendering(self):
        """Test dispute_detail template gracefully handles legacy dispute lacking category or evidence."""
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Legacy dispute reason'
        )

        self.client.login(username='taker', password='password123')
        url = reverse('dispute_detail', args=[dispute.id])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Other')
        self.assertContains(response, 'No evidence payload provided for this dispute.')
