from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, DisputeVote
from .views.dispute import resolve_dispute


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
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

        # Dispute is resolved via dispute resolution engine
        dispute = Dispute.objects.get(task=self.task)
        resolve_dispute(dispute, winner='taker')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute.refresh_from_db()
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
class EscrowLockAndJurorIncentivesTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')

        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1500)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=1500)
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=1500)

        self.client = Client()

    def test_poster_cannot_complete_disputed_task(self):
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )
        Dispute.objects.create(task=task, raised_by=self.taker, reason="Work unfinished")

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        self.assertEqual(self.taker_profile.rewards, 1500)
        messages = list(response.context['messages'])
        self.assertTrue(any("Disputed tasks cannot be manually completed." in str(m) for m in messages))

    def test_raise_dispute_records_dispute_hold_ledger(self):
        task = Task.objects.create(
            title="In Progress Task",
            description="Details",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='in_progress'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[task.id]), {'reason': 'Quality issue'}, follow=True)

        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        dispute_hold_ledger = RewardLedger.objects.filter(
            task=task,
            transaction_type='dispute_hold',
            user=self.poster
        ).first()
        self.assertIsNotNone(dispute_hold_ledger)
        self.assertEqual(dispute_hold_ledger.amount, 300)

    def test_dispute_resolution_with_jurors_taker_wins(self):
        task = Task.objects.create(
            title="Disputed Task Taker Wins",
            description="Details",
            reward=1000,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )
        dispute = Dispute.objects.create(task=task, raised_by=self.taker, reason="Disagreed on requirements")

        DisputeVote.objects.create(dispute=dispute, voter=self.juror1, choice='taker', voted_for=self.taker)
        DisputeVote.objects.create(dispute=dispute, voter=self.juror2, choice='taker', voted_for=self.taker)

        resolve_dispute(dispute)

        task.refresh_from_db()
        dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.juror1_profile.refresh_from_db()
        self.juror2_profile.refresh_from_db()

        self.assertEqual(task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')

        # Total reward = 1000. 10% juror fee = 100. Split between 2 jurors = 50 each.
        self.assertEqual(self.juror1_profile.rewards, 1550)
        self.assertEqual(self.juror2_profile.rewards, 1550)

        j1_ledger = RewardLedger.objects.get(user=self.juror1, transaction_type='juror_reward', task=task)
        j2_ledger = RewardLedger.objects.get(user=self.juror2, transaction_type='juror_reward', task=task)
        self.assertEqual(j1_ledger.amount, 50)
        self.assertEqual(j2_ledger.amount, 50)

        # Net payout for taker = 1000 - 100 = 900.
        self.assertEqual(self.taker_profile.rewards, 2400) # 1500 + 900
        payout_ledger = RewardLedger.objects.get(user=self.taker, transaction_type='dispute_payout', task=task)
        self.assertEqual(payout_ledger.amount, 900)

    def test_dispute_resolution_with_jurors_poster_wins(self):
        task = Task.objects.create(
            title="Disputed Task Poster Wins",
            description="Details",
            reward=1000,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )
        dispute = Dispute.objects.create(task=task, raised_by=self.taker, reason="Disagreed on requirements")

        DisputeVote.objects.create(dispute=dispute, voter=self.juror1, choice='poster', voted_for=self.poster)
        DisputeVote.objects.create(dispute=dispute, voter=self.juror2, choice='poster', voted_for=self.poster)

        resolve_dispute(dispute)

        task.refresh_from_db()
        dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.juror1_profile.refresh_from_db()
        self.juror2_profile.refresh_from_db()

        self.assertEqual(task.status, 'cancelled')
        self.assertEqual(dispute.status, 'resolved')

        # Jurors get 50 each
        self.assertEqual(self.juror1_profile.rewards, 1550)
        self.assertEqual(self.juror2_profile.rewards, 1550)

        # Poster gets net refund = 900
        self.assertEqual(self.poster_profile.rewards, 2400) # 1500 + 900
        refund_ledger = RewardLedger.objects.get(user=self.poster, transaction_type='dispute_refund', task=task)
        self.assertEqual(refund_ledger.amount, 900)

    def test_pending_rewards_includes_in_progress_and_disputed(self):
        t1 = Task.objects.create(
            title="In Progress Task", description="Details", reward=400,
            posted_by=self.poster, taken_by=self.taker, status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        t2 = Task.objects.create(
            title="Disputed Task", description="Details", reward=600,
            posted_by=self.poster, taken_by=self.taker, status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('rewards'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['pending_points'], 1000)

    def test_transaction_type_schema_choices(self):
        valid_types = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('dispute_hold', valid_types)
        self.assertIn('dispute_payout', valid_types)
        self.assertIn('dispute_refund', valid_types)
        self.assertIn('juror_reward', valid_types)

    def test_submit_dispute_vote_and_auto_resolution(self):
        task = Task.objects.create(
            title="Auto Resolution Task", description="Details", reward=500,
            posted_by=self.poster, taken_by=self.taker, status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )
        dispute = Dispute.objects.create(task=task, raised_by=self.taker, reason="Disagreement")

        jurors = []
        for i in range(5):
            u = User.objects.create_user(username=f'juror_vote_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=1000)
            jurors.append(u)

        # First 4 jurors vote for taker
        for u in jurors[:4]:
            self.client.login(username=u.username, password='password123')
            res = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'}, follow=True)
            self.assertEqual(res.status_code, 200)

        dispute.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # 5th juror votes for taker -> triggers auto resolution
        self.client.login(username=jurors[4].username, password='password123')
        res = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'}, follow=True)

        dispute.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(task.status, 'completed')

        # 500 reward * 10% = 50 total juror fee. Split between 5 jurors = 10 each.
        for u in jurors:
            u.userprofile.refresh_from_db()
            self.assertEqual(u.userprofile.rewards, 1010) # 1000 + 10
            jl = RewardLedger.objects.get(user=u, transaction_type='juror_reward', task=task)
            self.assertEqual(jl.amount, 10)

        # Taker receives net payout 500 - 50 = 450
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1950) # 1500 + 450
        pl = RewardLedger.objects.get(user=self.taker, transaction_type='dispute_payout', task=task)
        self.assertEqual(pl.amount, 450)

