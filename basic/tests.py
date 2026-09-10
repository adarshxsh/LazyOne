from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, DisputeVote, Conversation, RewardLedger, Notification

class CommunityDisputeWayTests(TestCase):
    def setUp(self):
        # Create users & user profiles
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.voter1 = User.objects.create_user(username='voter1', password='password123')
        self.voter2 = User.objects.create_user(username='voter2', password='password123')
        self.voter3 = User.objects.create_user(username='voter3', password='password123')

        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.voter1, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.voter2, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.voter3, defaults={'rewards': 1000})

        # Create task
        self.task = Task.objects.create(
            title='Test Disputed Task',
            description='Test Description',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )

        # Create conversation
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        # Create dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Work was done but poster refuses to mark complete'
        )

    def test_non_participant_can_view_disputed_chat_in_readonly(self):
        client = Client()
        client.login(username='voter1', password='password123')
        response = client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_readonly'])

    def test_non_participant_cannot_send_chat_message(self):
        client = Client()
        client.login(username='voter1', password='password123')
        response = client.post(reverse('send_message', args=[self.conversation.id]), {'content': 'Hello!'})
        self.assertEqual(response.status_code, 403)

    def test_participant_and_non_participant_dispute_detail_access(self):
        client = Client()
        # Non participant viewing open dispute
        client.login(username='voter1', password='password123')
        response = client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['is_participant'])

    def test_participants_cannot_vote_on_own_dispute(self):
        client = Client()
        client.login(username='poster', password='password123')
        response = client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'poster'}, follow=True)
        self.assertEqual(DisputeVote.objects.count(), 0)
        self.assertContains(response, "cannot vote on your own dispute")

    def test_non_participant_voting_and_duplicate_prevention(self):
        client = Client()
        client.login(username='voter1', password='password123')

        # Vote 1
        response = client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'taker'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DisputeVote.objects.count(), 1)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.taker_votes, 1)
        self.assertEqual(self.dispute.poster_votes, 0)

        # Duplicate Vote attempt
        response2 = client.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'taker'}, follow=True)
        self.assertEqual(DisputeVote.objects.count(), 1)
        self.assertContains(response2, "already voted")

    def test_quorum_settlement_favoring_taker(self):
        # 3 votes cast: 2 for taker, 1 for poster
        c1 = Client()
        c1.login(username='voter1', password='password123')
        c1.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'taker'})

        c2 = Client()
        c2.login(username='voter2', password='password123')
        c2.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'poster'})

        c3 = Client()
        c3.login(username='voter3', password='password123')
        c3.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'taker'})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_verdict, 'taker')
        self.assertEqual(self.task.status, 'completed')

        # Taker awarded 200 points (initial 1000 + 200 = 1200)
        taker_profile = UserProfile.objects.get(user=self.taker)
        self.assertEqual(taker_profile.rewards, 1200)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

        # Check Notifications sent to both poster and taker
        self.assertEqual(Notification.objects.filter(recipient=self.poster).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.taker).count(), 1)

    def test_quorum_settlement_favoring_poster(self):
        # 3 votes cast: 2 for poster, 1 for taker
        c1 = Client()
        c1.login(username='voter1', password='password123')
        c1.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'poster'})

        c2 = Client()
        c2.login(username='voter2', password='password123')
        c2.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'poster'})

        c3 = Client()
        c3.login(username='voter3', password='password123')
        c3.post(reverse('cast_dispute_vote', args=[self.dispute.id]), {'verdict': 'taker'})

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_verdict, 'poster')
        self.assertEqual(self.task.status, 'cancelled')

        # Poster refunded 200 points (initial 1000 + 200 = 1200)
        poster_profile = UserProfile.objects.get(user=self.poster)
        self.assertEqual(poster_profile.rewards, 1200)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

        # Check Notifications
        self.assertEqual(Notification.objects.filter(recipient=self.poster).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.taker).count(), 1)
