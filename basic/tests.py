from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from basic.models import (
    UserProfile, Task, Dispute, Conversation, Message,
    DisputeVote, JuryAssignment, RewardLedger
)
from datetime import timedelta

class JuryVotingEngineTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror3 = User.objects.create_user(username='juror3', password='password123')

        # Ensure profiles
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        self.juror1_profile, _ = UserProfile.objects.get_or_create(user=self.juror1, defaults={'rewards': 1500})
        self.juror2_profile, _ = UserProfile.objects.get_or_create(user=self.juror2, defaults={'rewards': 1500})
        self.juror3_profile, _ = UserProfile.objects.get_or_create(user=self.juror3, defaults={'rewards': 1500})

        # Create a disputed task
        deadline = timezone.now() + timedelta(days=1)
        self.task = Task.objects.create(
            title="Disputed Test Task",
            description="Task with dispute",
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=deadline,
            status='disputed'
        )

        # Create conversation for task
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        # Create dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Work completed but not accepted",
            status='open',
            quorum_threshold=3
        )

    def test_non_participant_can_view_disputed_chat_read_only(self):
        """Non-participant community members can view disputed chat in read-only mode."""
        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])

        # Attempt to send a message as a non-participant
        send_response = self.client.post(
            reverse('send_message', args=[self.conversation.id]),
            {'content': 'Unauthorized comment'}
        )
        self.assertEqual(send_response.status_code, 403)

    def test_non_participant_cannot_view_non_disputed_chat(self):
        """Non-participant cannot view chat for normal in_progress task."""
        normal_task = Task.objects.create(
            title="Normal Task",
            description="In progress task",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        normal_conv = Conversation.objects.create(task=normal_task)
        normal_conv.participants.add(self.poster, self.taker)

        self.client.login(username='juror1', password='password123')
        response = self.client.get(reverse('chat_view', args=[normal_conv.id]), follow=False)
        self.assertEqual(response.status_code, 302) # Redirects to home

    def test_task_participants_cannot_vote_on_own_dispute(self):
        """Task poster and taker cannot vote on their own dispute."""
        self.client.login(username='poster', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'vote_choice': 'poster'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(self.dispute.votes.count(), 0)

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'vote_choice': 'taker'}
        )
        self.assertEqual(self.dispute.votes.count(), 0)

    def test_submitting_community_vote_records_vote_and_increments_count(self):
        """Submitting a community vote records new vote and increments count."""
        self.client.login(username='juror1', password='password123')
        response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'vote_choice': 'taker', 'reason': 'Fair work completed.'}
        )
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(self.dispute.votes.count(), 1)

        vote = self.dispute.votes.first()
        self.assertEqual(vote.juror, self.juror1)
        self.assertEqual(vote.vote_choice, 'taker')
        self.assertEqual(vote.voted_for, self.taker)

        # Duplicate vote attempt should fail
        dup_response = self.client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'vote_choice': 'poster'}
        )
        self.assertEqual(self.dispute.votes.count(), 1)

    def test_reaching_quorum_settles_dispute_for_taker(self):
        """Reaching vote quorum resolves dispute in favor of worker and transfers rewards."""
        self.dispute.quorum_threshold = 2
        self.dispute.save()

        # Juror 1 votes for taker
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote_choice': 'taker'})

        # Juror 2 votes for taker
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote_choice': 'taker'})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winner, self.taker)
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 1500 + 500) # Awarded reward points

        # Verify ledger
        ledger_entry = RewardLedger.objects.filter(
            user=self.taker,
            task=self.task,
            transaction_type='dispute_payout'
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 500)

    def test_reaching_quorum_settles_dispute_for_poster(self):
        """Reaching vote quorum resolves dispute in favor of poster and refunds rewards."""
        self.dispute.quorum_threshold = 2
        self.dispute.save()

        # Juror 1 votes for poster
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote_choice': 'poster'})

        # Juror 2 votes for poster
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[self.dispute.id]), {'vote_choice': 'poster'})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winner, self.poster)
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1000 + 500) # Refunded reward points

        # Verify ledger
        ledger_entry = RewardLedger.objects.filter(
            user=self.poster,
            task=self.task,
            transaction_type='dispute_refund'
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 500)
