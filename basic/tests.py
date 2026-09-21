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


class PeerJuryVotingTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster & taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Friends of poster/taker to test exclusion
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, rewards=100)
        self.poster_profile.friends.add(self.poster_friend_profile)

        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile = UserProfile.objects.create(user=self.taker_friend, rewards=100)
        self.taker_profile.friends.add(self.taker_friend_profile)

        # Candidate neutral users (6 neutral users)
        self.neutral_users = []
        for i in range(1, 7):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=100)
            self.neutral_users.append(u)

        # Task
        self.task = Task.objects.create(
            title="Peer Jury Task",
            description="Task for testing jury voting",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_random_jury_assignment_and_exclusions(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not up to par'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertTrue(hasattr(dispute, 'jury'))
        jury = dispute.jury

        assignments = jury.assignments.all()
        self.assertEqual(assignments.count(), 5)

        assigned_jurors = [a.juror for a in assignments]

        # Poster, taker, poster_friend, taker_friend must NOT be assigned
        self.assertNotIn(self.poster, assigned_jurors)
        self.assertNotIn(self.taker, assigned_jurors)
        self.assertNotIn(self.poster_friend, assigned_jurors)
        self.assertNotIn(self.taker_friend, assigned_jurors)

        # Assigned jurors must all be neutral users
        for juror in assigned_jurors:
            self.assertIn(juror, self.neutral_users)

    def test_juror_authorization_and_access_control(self):
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})

        dispute = Dispute.objects.get(task=self.task)
        jury = dispute.jury
        assigned_juror = jury.assignments.first().juror

        # Unrelated non-juror (poster_friend)
        self.client.login(username='poster_friend', password='password123')
        res = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(res, reverse('home'))

        # Assigned juror can access dispute detail page
        self.client.login(username=assigned_juror.username, password='password123')
        res = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Dispute Details")
        self.assertContains(res, "Submit Juror Vote")

    def test_jury_majority_voting_and_settlement(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})

        dispute = Dispute.objects.get(task=self.task)
        jury = dispute.jury
        jurors = [a.juror for a in jury.assignments.all()]

        # 3 jurors vote for taker (majority)
        for juror in jurors[:3]:
            self.client.login(username=juror.username, password='password123')
            res = self.client.post(
                reverse('submit_jury_vote', args=[dispute.id]),
                {'voted_for': self.taker.id}
            )
            self.assertRedirects(res, reverse('dispute_detail', args=[dispute.id]))

        # Re-fetch objects
        jury.refresh_from_db()
        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(jury.status, 'concluded')
        self.assertEqual(jury.winner, self.taker)
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(self.task.status, 'completed')

        # Check juror rewards for the 3 majority voters (20 points each)
        for juror in jurors[:3]:
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 120)  # 100 + 20

    def test_non_responsive_juror_expiration(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})

        dispute = Dispute.objects.get(task=self.task)
        jury = dispute.jury

        # Fast forward time beyond deadline (49 hours)
        jury.deadline = timezone.now() - timedelta(hours=1)
        jury.save()

        # Access dispute detail page as poster to trigger timeout check
        self.client.login(username='poster', password='password123')
        self.client.get(reverse('dispute_detail', args=[dispute.id]))

        # Expired assignments should be marked as expired
        expired_count = jury.assignments.filter(status='expired').count()
        self.assertGreater(expired_count, 0)


