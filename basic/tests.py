from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation


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

    def test_complete_disputed_task_blocked(self):
        # Taker raises dispute (deposit 60 deducted from 100 -> 40 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        # Poster attempts to mark task as completed
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Task status must remain 'disputed'
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.status, 'open')

    def test_jury_voting_guards_and_consensus_resolution(self):
        # 1. Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # 2. Poster and Taker cannot vote
        self.client.login(username='poster', password='password123')
        res = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for_id': self.poster.id})
        self.assertRedirects(res, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(dispute.jury_votes.count(), 0)

        # 3. Create 3 neutral juror users
        jurors = []
        for i in range(1, 4):
            juror = User.objects.create_user(username=f'juror{i}', password='password123')
            UserProfile.objects.create(user=juror, rewards=100)
            jurors.append(juror)

        # 4. Juror 1 votes for Taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for_id': self.taker.id})
        self.assertEqual(dispute.jury_votes.count(), 1)

        # Juror 1 tries to vote again -> blocked
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for_id': self.taker.id})
        self.assertEqual(dispute.jury_votes.count(), 1)

        # 5. Juror 2 votes for Taker
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for_id': self.taker.id})
        self.assertEqual(dispute.jury_votes.count(), 2)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # 6. Juror 3 votes for Poster -> total 3 votes -> Consensus reached!
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for_id': self.poster.id})

        # Dispute should now be resolved!
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        # Taker won 2 vs 1.
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Bond was 60. Winning jurors (juror1, juror2) get 60 // 2 = 30 reward each!
        juror1_profile = UserProfile.objects.get(user=jurors[0])
        juror2_profile = UserProfile.objects.get(user=jurors[1])
        juror3_profile = UserProfile.objects.get(user=jurors[2])

        self.assertEqual(juror1_profile.rewards, 130) # 100 + 30
        self.assertEqual(juror2_profile.rewards, 130) # 100 + 30
        self.assertEqual(juror3_profile.rewards, 100) # Lost vote, no reward

        # Check RewardLedger entries for juror_reward and dispute_penalty
        self.assertTrue(RewardLedger.objects.filter(user=jurors[0], transaction_type='juror_reward', amount=30).exists())
        self.assertTrue(RewardLedger.objects.filter(user=jurors[1], transaction_type='juror_reward', amount=30).exists())
        self.assertTrue(RewardLedger.objects.filter(transaction_type='dispute_penalty').exists())

    def test_jury_voting_poster_wins(self):
        # Taker raises dispute (deposit 60 deducted from 100 -> 40 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Create 3 neutral juror users
        jurors = []
        for i in range(1, 4):
            juror = User.objects.create_user(username=f'p_juror{i}', password='password123')
            UserProfile.objects.create(user=juror, rewards=100)
            jurors.append(juror)

        # All 3 jurors vote for Poster
        for j in jurors:
            self.client.login(username=j.username, password='password123')
            self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for_id': self.poster.id})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'forfeited')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        # Poster gets task reward (300) refunded: 1000 + 300 = 1300
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1300)

        # 3 winning jurors share 60 deposit bond -> 20 each (100 -> 120)
        for j in jurors:
            p = UserProfile.objects.get(user=j)
            self.assertEqual(p.rewards, 120)
            self.assertTrue(RewardLedger.objects.filter(user=j, transaction_type='juror_reward', amount=20).exists())

        # Check dispute_penalty ledger for taker
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_penalty').exists())

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

