from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from .models import UserProfile, Task, Dispute, Conversation, JuryAssignment, DisputeVote, RewardLedger

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
        self.assertEqual(self.poster.userprofile.rewards, initial_poster_rewards + self.task.reward)

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
