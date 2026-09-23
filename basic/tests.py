from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JuryAssignment, DisputeVote, Notification
from django.core.management import call_command


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


class PeerJurySelectionTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.poster = User.objects.create_user(username='poster', password='password123')
        UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.create(user=self.taker, rewards=500)

        # Create 4 eligible peer users
        self.peers = []
        for i in range(1, 5):
            u = User.objects.create_user(username=f'peer{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=200)
            self.peers.append(u)

        # Ineligible user: negative rewards
        self.poor_peer = User.objects.create_user(username='poor_peer', password='password123')
        UserProfile.objects.create(user=self.poor_peer, rewards=-10)

        # Ineligible user: inactive
        self.inactive_peer = User.objects.create_user(username='inactive_peer', password='password123', is_active=False)
        UserProfile.objects.create(user=self.inactive_peer, rewards=500)

        # Random non-juror user
        self.outsider = User.objects.create_user(username='outsider', password='password123')
        UserProfile.objects.create(user=self.outsider, rewards=100)

        self.task = Task.objects.create(
            title="Jury Test Task",
            description="Testing Jury Selection",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_jury_selection_creates_three_eligible_jurors(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work unsatisfactory'}
        )

        dispute = Dispute.objects.get(task=self.task)
        jury = JuryAssignment.objects.filter(dispute=dispute)

        # Must assign 3 distinct jurors
        self.assertEqual(jury.count(), 3)

        assigned_users = [j.user for j in jury]
        # Exclude task participants and ineligible users
        self.assertNotIn(self.poster, assigned_users)
        self.assertNotIn(self.taker, assigned_users)
        self.assertNotIn(self.poor_peer, assigned_users)
        self.assertNotIn(self.inactive_peer, assigned_users)

        # Verify notifications created for jurors
        for j in assigned_users:
            self.assertTrue(Notification.objects.filter(recipient=j).exists())

    def test_dispute_access_control(self):
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work unsatisfactory'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Task poster can view
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Task taker can view
        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Assigned juror can view
        juror = JuryAssignment.objects.filter(dispute=dispute).first().user
        self.client.login(username=juror.username, password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        # Non-juror outsider CANNOT view
        non_juror = User.objects.create_user(username='non_juror', password='password123')
        UserProfile.objects.create(user=non_juror, rewards=-1)
        self.client.login(username='non_juror', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_secret_voting_and_bonus_payout(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work unsatisfactory'}
        )
        dispute = Dispute.objects.get(task=self.task)
        juror = JuryAssignment.objects.filter(dispute=dispute).first().user

        initial_rewards = juror.userprofile.rewards
        # Juror votes for Taker
        self.client.login(username=juror.username, password='password123')
        response = self.client.post(
            reverse('submit_vote', args=[dispute.id]),
            {'voted_user': self.taker.id}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        # Verify vote logged
        vote = DisputeVote.objects.get(dispute=dispute, juror=juror)
        self.assertEqual(vote.vote_for, self.taker)

        # Verify participation bonus (+25)
        juror.userprofile.refresh_from_db()
        self.assertEqual(juror.userprofile.rewards, initial_rewards + 25)

        ledger = RewardLedger.objects.filter(user=juror, transaction_type='jury_reward').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 25)

        # Duplicate vote attempt rejected
        response_dup = self.client.post(
            reverse('submit_vote', args=[dispute.id]),
            {'voted_user': self.poster.id}
        )
        self.assertRedirects(response_dup, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(DisputeVote.objects.filter(dispute=dispute, juror=juror).count(), 1)

    def test_majority_consensus_settlement_taker_wins(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work unsatisfactory'}
        )
        dispute = Dispute.objects.get(task=self.task)
        jurors = [j.user for j in JuryAssignment.objects.filter(dispute=dispute)]

        # First vote for Taker (no consensus yet)
        self.client.login(username=jurors[0].username, password='password123')
        self.client.post(reverse('submit_vote', args=[dispute.id]), {'voted_user': self.taker.id})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Second vote for Taker -> 2 matching votes reached (Majority Consensus!)
        self.client.login(username=jurors[1].username, password='password123')
        self.client.post(reverse('submit_vote', args=[dispute.id]), {'voted_user': self.taker.id})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Taker received task reward (200) + deposit refund (50)
        self.taker.userprofile.refresh_from_db()
        # Initial 500 - 50 deposit + 200 reward + 50 deposit refund = 700
        self.assertEqual(self.taker.userprofile.rewards, 700)

    def test_majority_consensus_settlement_poster_wins(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work unsatisfactory'}
        )
        dispute = Dispute.objects.get(task=self.task)
        jurors = [j.user for j in JuryAssignment.objects.filter(dispute=dispute)]

        # Two jurors vote for Poster
        self.client.login(username=jurors[0].username, password='password123')
        self.client.post(reverse('submit_vote', args=[dispute.id]), {'voted_user': self.poster.id})

        self.client.login(username=jurors[1].username, password='password123')
        self.client.post(reverse('submit_vote', args=[dispute.id]), {'voted_user': self.poster.id})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        # Poster received task reward refund (200) + forfeited deposit bond from taker (50)
        self.poster.userprofile.refresh_from_db()
        # Initial 1000 + 200 reward refund + 50 forfeited deposit bond = 1250
        self.assertEqual(self.poster.userprofile.rewards, 1250)

    def test_expired_dispute_management_command_fallback(self):
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work unsatisfactory'}
        )
        dispute = Dispute.objects.get(task=self.task)

        # Set dispute creation time 49 hours in the past
        dispute.created_at = timezone.now() - timedelta(hours=49)
        dispute.save()

        # Run management command
        call_command('resolve_expired_disputes')

        dispute.refresh_from_db()
        # Should transition to staff review
        self.assertEqual(dispute.status, 'staff_review')


