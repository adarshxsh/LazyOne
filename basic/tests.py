import tempfile
from django.test import TestCase, override_settings, Client
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Conversation


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

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
            {'reason': 'Work not clear'}
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
            {'reason': 'Unreasonable request'}
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
            {'reason': 'Dispute reason'}
        )

        dispute = Dispute.objects.get(task=self.task)

        # Withdraw dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'withdrawn')

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
            {'reason': 'Dispute reason'}
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


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    MEDIA_ROOT=tempfile.mkdtemp()
)
class DisputeLifecycleAndEvidenceTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        self.task = Task.objects.create(
            title='Test Task',
            description='Do something',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Task work was not acknowledged'
        )

        self.client = Client()

    def test_status_choices_expansion(self):
        expected_choices = ['open', 'evidence_submission', 'voting', 'appealed', 'resolved', 'withdrawn']
        actual_choices = [choice[0] for choice in Dispute.STATUS_CHOICES]
        for choice in expected_choices:
            self.assertIn(choice, actual_choices)

    def test_dispute_evidence_model_creation_and_attribution(self):
        evidence1 = DisputeEvidence.objects.create(
            dispute=self.dispute,
            user=self.taker,
            text='Proof of completion screenshot'
        )
        uploaded_file = SimpleUploadedFile('screenshot.png', b'file_content', content_type='image/png')
        evidence2 = DisputeEvidence.objects.create(
            dispute=self.dispute,
            user=self.poster,
            text='Counter proof statement',
            file=uploaded_file
        )

        self.assertEqual(self.dispute.evidence_entries.count(), 2)
        self.assertEqual(evidence1.submitted_by, self.taker)
        self.assertEqual(evidence1.description, 'Proof of completion screenshot')
        self.assertEqual(evidence2.submitted_by, self.poster)
        self.assertIn('screenshot', evidence2.file_path)

    def test_submit_evidence_transition(self):
        self.assertEqual(self.dispute.status, 'open')
        evidence = self.dispute.submit_evidence(user=self.taker, text='First evidence')
        self.assertEqual(self.dispute.status, 'evidence_submission')
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.text, 'First evidence')

        # Additional evidence submission in evidence_submission state
        evidence2 = self.dispute.submit_evidence(user=self.poster, text='Second evidence')
        self.assertEqual(self.dispute.status, 'evidence_submission')
        self.assertEqual(self.dispute.evidence_entries.count(), 2)

    def test_start_voting_transition(self):
        self.dispute.submit_evidence(user=self.taker, text='Evidence before voting')
        self.assertEqual(self.dispute.status, 'evidence_submission')

        self.dispute.start_voting()
        self.assertEqual(self.dispute.status, 'voting')

    def test_resolve_and_appeal_transitions(self):
        self.dispute.start_voting()
        self.assertEqual(self.dispute.status, 'voting')

        self.dispute.resolve()
        self.assertEqual(self.dispute.status, 'resolved')

        self.dispute.appeal()
        self.assertEqual(self.dispute.status, 'appealed')

        self.dispute.resolve()
        self.assertEqual(self.dispute.status, 'resolved')

    def test_invalid_state_transitions_raise_validation_error(self):
        # Open directly to appealed is invalid
        with self.assertRaises(ValidationError):
            self.dispute.appeal()

        # Resolve the dispute
        self.dispute.resolve()
        self.assertEqual(self.dispute.status, 'resolved')

        # Cannot submit evidence when resolved
        with self.assertRaises(ValidationError):
            self.dispute.submit_evidence(user=self.taker, text='Late evidence')

        # Cannot start voting when resolved
        with self.assertRaises(ValidationError):
            self.dispute.start_voting()

        # Cannot resolve an already resolved dispute
        with self.assertRaises(ValidationError):
            self.dispute.resolve()

        # Cannot withdraw a resolved dispute
        with self.assertRaises(ValidationError):
            self.dispute.withdraw()

    def test_withdraw_dispute_retains_instance(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[self.dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'withdrawn')
        self.assertTrue(Dispute.objects.filter(id=self.dispute.id).exists())
        self.assertEqual(self.task.status, 'in_progress')

        # Attempting to withdraw again should raise ValidationError
        with self.assertRaises(ValidationError):
            self.dispute.withdraw()

    def test_dispute_detail_view_renders_status_badge_and_evidence(self):
        DisputeEvidence.objects.create(
            dispute=self.dispute,
            user=self.taker,
            text='Initial proof submitted'
        )

        self.client.login(username='taker', password='password123')
        url = reverse('dispute_detail', args=[self.dispute.id])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Open')
        self.assertContains(response, 'Initial proof submitted')
        self.assertContains(response, 'Submit Evidence')

    def test_dispute_detail_post_submits_evidence(self):
        self.client.login(username='poster', password='password123')
        url = reverse('dispute_detail', args=[self.dispute.id])
        
        test_file = SimpleUploadedFile('log.txt', b'log_data', content_type='text/plain')
        response = self.client.post(url, {
            'text': 'Poster response statement',
            'file': test_file
        })

        self.assertRedirects(response, url)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'evidence_submission')
        self.assertEqual(self.dispute.evidence_entries.count(), 1)
        evidence = self.dispute.evidence_entries.first()
        self.assertEqual(evidence.user, self.poster)
        self.assertEqual(evidence.text, 'Poster response statement')
