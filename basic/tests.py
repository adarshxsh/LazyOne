from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation
from .forms import DisputeForm


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
            {
                'category': 'other',
                'reason': 'Work not clear',
                'evidence_details': 'Detailed description of evidence exceeding twenty characters.'
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
                'evidence_details': 'Detailed description of evidence exceeding twenty characters.'
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
                'evidence_details': 'Detailed description of evidence exceeding twenty characters.'
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
                'evidence_details': 'Detailed description of evidence exceeding twenty characters.'
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


class DisputeModelAndFormTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

    def test_dispute_model_creation_and_choices(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            category='incomplete_requirements',
            reason='The task requirements were incomplete.',
            evidence_url='https://example.com/evidence.jpg',
            evidence_details='Here is the detailed evidence explaining why requirements were incomplete.'
        )
        self.assertEqual(dispute.category, 'incomplete_requirements')
        self.assertEqual(dispute.get_category_display(), 'Incomplete Requirements')
        self.assertEqual(dispute.evidence_url, 'https://example.com/evidence.jpg')
        self.assertEqual(dispute.evidence_details, 'Here is the detailed evidence explaining why requirements were incomplete.')

    def test_legacy_dispute_compatibility(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Legacy reason without category or evidence'
        )
        self.assertIsNone(dispute.category)
        self.assertIsNone(dispute.evidence_url)
        self.assertIsNone(dispute.evidence_details)

    def test_dispute_form_valid_data(self):
        form = DisputeForm(data={
            'category': 'deliverable_issue',
            'reason': 'Deliverable was defective',
            'evidence_url': 'https://example.com/proof',
            'evidence_details': 'Detailed description of the deliverable issue exceeding twenty characters.'
        })
        self.assertTrue(form.is_valid())

    def test_dispute_form_missing_category(self):
        form = DisputeForm(data={
            'category': '',
            'reason': 'Deliverable was defective',
            'evidence_details': 'Detailed description of the deliverable issue exceeding twenty characters.'
        })
        self.assertFalse(form.is_valid())
        self.assertIn('category', form.errors)

    def test_dispute_form_invalid_category(self):
        form = DisputeForm(data={
            'category': 'invalid_cat',
            'reason': 'Deliverable was defective',
            'evidence_details': 'Detailed description of the deliverable issue exceeding twenty characters.'
        })
        self.assertFalse(form.is_valid())
        self.assertIn('category', form.errors)

    def test_dispute_form_missing_evidence_details(self):
        form = DisputeForm(data={
            'category': 'deliverable_issue',
            'reason': 'Deliverable was defective',
            'evidence_details': ''
        })
        self.assertFalse(form.is_valid())
        self.assertIn('evidence_details', form.errors)

    def test_dispute_form_short_evidence_details(self):
        form = DisputeForm(data={
            'category': 'deliverable_issue',
            'reason': 'Deliverable was defective',
            'evidence_details': 'Too short'
        })
        self.assertFalse(form.is_valid())
        self.assertIn('evidence_details', form.errors)


class RaiseDisputeViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)
        self.task = Task.objects.create(
            title='View Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {
            'category': 'unresponsive_counterparty',
            'reason': 'Poster is unresponsive',
            'evidence_url': 'https://example.com/chat-log',
            'evidence_details': 'I sent multiple messages over three days and received no response from the poster.'
        }, follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        dispute = self.task.dispute
        self.assertEqual(dispute.category, 'unresponsive_counterparty')
        self.assertEqual(dispute.evidence_details, 'I sent multiple messages over three days and received no response from the poster.')
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertContains(response, 'Unresponsive Counterparty')

    def test_raise_dispute_missing_category_fails(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {
            'category': '',
            'reason': 'Some reason for dispute',
            'evidence_details': 'This is a long enough evidence detail text that passes minimum length.'
        }, follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))
        self.assertRedirects(response, reverse('my_tasks'))

    def test_raise_dispute_short_evidence_details_fails(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {
            'category': 'payment_points_dispute',
            'reason': 'Points not credited',
            'evidence_details': 'Short details'
        }, follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))
        self.assertRedirects(response, reverse('my_tasks'))

    def test_dispute_detail_view_renders_category_and_evidence(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            category='deliverable_issue',
            reason='Flawed deliverable',
            evidence_url='https://example.com/evidence-link',
            evidence_details='The delivered item did not meet the specified standard.'
        )
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Deliverable Issue')
        self.assertContains(response, 'https://example.com/evidence-link')
        self.assertContains(response, 'The delivered item did not meet the specified standard.')
