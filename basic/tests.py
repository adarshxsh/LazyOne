from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeAppeal, RewardLedger, Conversation, Notification


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


class DisputeArbitrationAndAppealTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.senior_staff = User.objects.create_user(username='senior_staff', password='password123', is_staff=True, is_superuser=True)

        # Ensure user profiles exist
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster)
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker)
        UserProfile.objects.get_or_create(user=self.other_user)
        UserProfile.objects.get_or_create(user=self.staff_user)
        UserProfile.objects.get_or_create(user=self.senior_staff)

        # Set initial reward balances
        self.poster_profile.rewards = 1000
        self.poster_profile.save()
        self.taker_profile.rewards = 1000
        self.taker_profile.save()

        # Create task
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        # Create dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Poster refused to accept completion.'
        )
        self.task.status = 'disputed'
        self.task.save()

    def test_staff_arbitration_favour_poster(self):
        self.client.login(username='staff', password='password123')
        url = reverse('arbitrate_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'outcome': 'favour_poster',
            'rationale': 'Poster was justified as task criteria were missed.'
        })

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_outcome, 'favour_poster')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1100) # 1000 + 100 refund

        ledger_entry = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 100)

        # Verify notifications sent
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.taker).exists())

    def test_staff_arbitration_favour_taker(self):
        self.client.login(username='staff', password='password123')
        url = reverse('arbitrate_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'outcome': 'favour_taker',
            'rationale': 'Taker completed work as requested.'
        })

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_outcome, 'favour_taker')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 1100) # 1000 + 100 payout

        ledger_entry = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 100)

    def test_non_staff_cannot_arbitrate(self):
        self.client.login(username='poster', password='password123')
        url = reverse('arbitrate_dispute', args=[self.dispute.id])
        response = self.client.post(url, {
            'outcome': 'favour_poster',
            'rationale': 'I should win.'
        })

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_submit_appeal_flow(self):
        # Resolve dispute first
        self.client.login(username='staff', password='password123')
        self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {
            'outcome': 'favour_poster',
            'rationale': 'Initial ruling favouring poster.'
        })

        # Login as taker and submit appeal
        self.client.login(username='taker', password='password123')
        url = reverse('submit_appeal', args=[self.dispute.id])
        response = self.client.post(url, {
            'reason': 'I have screenshots showing completed work.'
        })

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appealed')

        appeal = self.dispute.appeal
        self.assertEqual(appeal.appealed_by, self.taker)
        self.assertEqual(appeal.status, 'pending')
        self.assertEqual(appeal.reason, 'I have screenshots showing completed work.')

    def test_unauthorized_user_cannot_appeal(self):
        # Resolve dispute
        self.client.login(username='staff', password='password123')
        self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {
            'outcome': 'favour_poster',
            'rationale': 'Initial ruling.'
        })

        # Other user tries to appeal
        self.client.login(username='other', password='password123')
        url = reverse('submit_appeal', args=[self.dispute.id])
        response = self.client.post(url, {
            'reason': 'Random user appealing.'
        })

        self.assertFalse(hasattr(self.dispute, 'appeal'))

    def test_review_appeal_uphold(self):
        # Setup resolved and appealed dispute
        self.dispute.status = 'resolved'
        self.dispute.resolution_outcome = 'favour_poster'
        self.dispute.resolved_at = timezone.now()
        self.dispute.save()

        appeal = DisputeAppeal.objects.create(
            dispute=self.dispute,
            appealed_by=self.taker,
            reason='Filing appeal.'
        )
        self.dispute.status = 'appealed'
        self.dispute.save()

        self.client.login(username='senior_staff', password='password123')
        url = reverse('review_appeal', args=[self.dispute.id])
        response = self.client.post(url, {
            'decision': 'uphold',
            'review_notes': 'Original ruling was accurate.'
        })

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        appeal.refresh_from_db()
        self.dispute.refresh_from_db()

        self.assertEqual(appeal.status, 'upheld')
        self.assertEqual(appeal.reviewed_by, self.senior_staff)
        self.assertEqual(self.dispute.status, 'closed')

    def test_review_appeal_overturn_initial_favour_poster(self):
        # First arbitrate favouring poster
        self.client.login(username='staff', password='password123')
        self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {
            'outcome': 'favour_poster',
            'rationale': 'Initial ruling.'
        })

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1100)

        # Taker appeals
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'reason': 'New evidence attached.'
        })

        # Senior staff overturns appeal
        self.client.login(username='senior_staff', password='password123')
        response = self.client.post(reverse('review_appeal', args=[self.dispute.id]), {
            'decision': 'overturn',
            'review_notes': 'New evidence justifies awarding taker.'
        })

        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Balance reversed: Poster loses 100 refund (back to 1000), Taker receives 100 payout (1100)
        self.assertEqual(self.poster_profile.rewards, 1000)
        self.assertEqual(self.taker_profile.rewards, 1100)
        self.assertEqual(self.dispute.resolution_outcome, 'favour_taker')
        self.assertEqual(self.task.status, 'completed')

        self.assertTrue(RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='appeal_overturn', amount=-100).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='appeal_overturn', amount=100).exists())

    def test_review_appeal_overturn_initial_favour_taker(self):
        # First arbitrate favouring taker
        self.client.login(username='staff', password='password123')
        self.client.post(reverse('arbitrate_dispute', args=[self.dispute.id]), {
            'outcome': 'favour_taker',
            'rationale': 'Initial ruling.'
        })

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1100)

        # Poster appeals
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('submit_appeal', args=[self.dispute.id]), {
            'reason': 'Work was done incorrectly.'
        })

        # Senior staff overturns appeal
        self.client.login(username='senior_staff', password='password123')
        response = self.client.post(reverse('review_appeal', args=[self.dispute.id]), {
            'decision': 'overturn',
            'review_notes': 'Poster provided valid proof.'
        })

        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Balance reversed: Taker loses 100 payout (back to 1000), Poster receives 100 refund (1100)
        self.assertEqual(self.taker_profile.rewards, 1000)
        self.assertEqual(self.poster_profile.rewards, 1100)
        self.assertEqual(self.dispute.resolution_outcome, 'favour_poster')
        self.assertEqual(self.task.status, 'cancelled')

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='appeal_overturn', amount=-100).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='appeal_overturn', amount=100).exists())
