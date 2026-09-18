from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JuryAssignment, DisputeVote


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


class PeerJuryDisputeResolutionTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create poster and taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.create(user=self.taker, rewards=1000)

        # Create 6 community users eligible for jury duty
        self.juror_users = []
        for i in range(1, 7):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=1000)
            self.juror_users.append(u)

        # Poster creates a task
        self.client.login(username='poster', password='password123')
        self.task = Task.objects.create(
            title='Test Disputed Task',
            description='Detailed task description',
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='available'
        )
        self.client.logout()

        # Taker takes the task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[self.task.id]))
        self.task.refresh_from_db()
        self.conversation = Conversation.objects.get(task=self.task)
        self.client.logout()

    def test_randomized_jury_selection_excludes_poster_and_worker(self):
        # Taker raises a dispute
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfair task requirement'})
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        dispute = getattr(self.task, 'dispute', None)
        self.assertIsNotNone(dispute)

        assignments = JuryAssignment.objects.filter(dispute=dispute)
        self.assertEqual(assignments.count(), 5)

        assigned_jurors = [a.juror for a in assignments]
        self.assertNotIn(self.poster, assigned_jurors)
        self.assertNotIn(self.taker, assigned_jurors)

    def test_read_only_chat_access_for_jurors_and_blocking_non_jurors(self):
        # Raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        self.client.logout()

        dispute = self.task.dispute
        assigned_juror = JuryAssignment.objects.filter(dispute=dispute).first().juror

        # Juror can access chat_view and context has is_read_only = True
        self.client.login(username=assigned_juror.username, password='password123')
        response = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])

        # Juror attempting to send a chat message gets HTTP 403
        response = self.client.post(reverse('send_message', args=[self.conversation.id]), {'content': 'Juror trying to talk'})
        self.assertEqual(response.status_code, 403)
        self.client.logout()

        # Create a non-juror community user after dispute creation
        non_juror = User.objects.create_user(username='non_juror_chat', password='password123')
        UserProfile.objects.create(user=non_juror, rewards=1000)

        # Non-juror gets redirected away from chat_view
        self.client.login(username='non_juror_chat', password='password123')
        response = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertRedirects(response, reverse('home'))
        self.client.logout()

    def test_dispute_detail_authorization(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        self.client.logout()

        dispute = self.task.dispute
        assigned_juror = JuryAssignment.objects.filter(dispute=dispute).first().juror

        # Juror, poster, and taker can view dispute details
        for user in [assigned_juror, self.poster, self.taker]:
            self.client.login(username=user.username, password='password123')
            response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
            self.assertEqual(response.status_code, 200)
            self.client.logout()

        # Create a non-juror community user after dispute creation
        non_juror = User.objects.create_user(username='non_juror_detail', password='password123')
        UserProfile.objects.create(user=non_juror, rewards=1000)

        # Non-juror is redirected
        self.client.login(username='non_juror_detail', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_vote_submission_persistence_and_validation(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        self.client.logout()

        dispute = self.task.dispute
        juror = JuryAssignment.objects.filter(dispute=dispute).first().juror

        self.client.login(username=juror.username, password='password123')

        # Attempt vote with empty rationale
        response = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'vote': 'poster_wins', 'rationale': '   '})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, juror=juror).exists())

        # Submit valid vote
        response = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'vote': 'poster_wins', 'rationale': 'Poster fulfilled initial contract terms.'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

        vote_record = DisputeVote.objects.filter(dispute=dispute, juror=juror).first()
        self.assertIsNotNone(vote_record)
        self.assertEqual(vote_record.vote, 'poster_wins')
        self.assertEqual(vote_record.rationale, 'Poster fulfilled initial contract terms.')

        # Attempt to vote a second time
        response = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'vote': 'taker_wins', 'rationale': 'Changed my mind.'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(DisputeVote.objects.filter(dispute=dispute, juror=juror).count(), 1)

    def test_automated_consensus_poster_wins(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        self.client.logout()

        dispute = self.task.dispute
        jurors = [a.juror for a in JuryAssignment.objects.filter(dispute=dispute)[:3]]

        initial_poster_rewards = self.poster.userprofile.rewards

        for juror in jurors:
            self.client.login(username=juror.username, password='password123')
            self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'vote': 'poster_wins', 'rationale': 'Poster wins rationale'})
            self.client.logout()

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster.userprofile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        # Poster gets refunded task reward + forfeited dispute deposit bond
        deposit_amount = dispute.deposit_amount
        self.assertEqual(self.poster.userprofile.rewards, initial_poster_rewards + self.task.reward + deposit_amount)

        ledger_entry = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='task_cancellation').last()
        self.assertIsNotNone(ledger_entry)

    def test_automated_consensus_taker_wins(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        self.client.logout()

        dispute = self.task.dispute
        jurors = [a.juror for a in JuryAssignment.objects.filter(dispute=dispute)[:3]]

        initial_taker_rewards = self.taker.userprofile.rewards

        for juror in jurors:
            self.client.login(username=juror.username, password='password123')
            self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'vote': 'taker_wins', 'rationale': 'Worker completed the work as requested.'})
            self.client.logout()

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker.userprofile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker.userprofile.rewards, initial_taker_rewards + self.task.reward)

        ledger_entry = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='task_completion').last()
        self.assertIsNotNone(ledger_entry)

    def test_voting_window_expiration_48_hours(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Dispute reason'})
        self.client.logout()

        dispute = self.task.dispute
        # Backdate dispute creation to 49 hours ago
        dispute.created_at = timezone.now() - timedelta(hours=49)
        dispute.save()

        juror = JuryAssignment.objects.filter(dispute=dispute).first().juror
        self.client.login(username=juror.username, password='password123')

        response = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'vote': 'poster_wins', 'rationale': 'Late vote'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=dispute, juror=juror).exists())
