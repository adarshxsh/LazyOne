from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, Conversation, Message, RewardLedger, DisputeVote

class CommunityDisputeTests(TestCase):
    def setUp(self):
        # Create poster and taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)

        # Create community voters
        self.voters = []
        for i in range(1, 8):
            user = User.objects.create_user(username=f'voter{i}', password='password123')
            UserProfile.objects.create(user=user, rewards=1500)
            self.voters.append(user)

        # Create task in disputed status
        self.task = Task.objects.create(
            title="Disputed Task Title",
            description="Task description details",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )

        # Create dispute and conversation
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason="Incomplete work claim"
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_disputed_chat_read_only_access(self):
        client = Client()
        client.login(username='voter1', password='password123')

        response = client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])

    def test_non_disputed_chat_access_denied(self):
        # Create non-disputed task and chat
        other_task = Task.objects.create(
            title="Normal Task",
            description="Normal description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        other_conv = Conversation.objects.create(task=other_task)
        other_conv.participants.add(self.poster, self.taker)

        client = Client()
        client.login(username='voter1', password='password123')

        response = client.get(reverse('chat_view', args=[other_conv.id]))
        self.assertRedirects(response, reverse('home'))

    def test_send_message_restricted_for_non_participants(self):
        client = Client()
        client.login(username='voter1', password='password123')

        response = client.post(reverse('send_message', args=[self.conversation.id]), {'content': 'Hello!'})
        self.assertEqual(response.status_code, 403)

    def test_dispute_detail_access_for_community_members(self):
        client = Client()
        client.login(username='voter1', password='password123')

        response = client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['can_vote'])

    def test_participants_cannot_vote_on_own_dispute(self):
        client = Client()
        
        # Test poster voting
        client.login(username='poster', password='password123')
        response = client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'choice': 'poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.poster).exists())

        # Test taker voting
        client.login(username='taker', password='password123')
        response = client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'choice': 'taker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.taker).exists())

    def test_duplicate_vote_rejected(self):
        client = Client()
        client.login(username='voter1', password='password123')

        # First vote
        response = client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'choice': 'taker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute, voter=self.voters[0]).count(), 1)

        # Second vote attempt
        response2 = client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'choice': 'poster'})
        self.assertRedirects(response2, reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute, voter=self.voters[0]).count(), 1)

    def test_settlement_when_5_votes_accumulated_taker_wins(self):
        # 3 votes for taker, 2 votes for poster
        choices = ['taker', 'taker', 'poster', 'taker', 'poster']
        for i in range(5):
            client = Client()
            client.login(username=self.voters[i].username, password='password123')
            response = client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'choice': choices[i]})

        self.task.refresh_from_db()
        self.dispute.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.taker_profile.rewards, 1700) # 1500 + 200

        # Verify RewardLedger
        ledger = RewardLedger.objects.filter(task=self.task, user=self.taker, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)

    def test_settlement_when_5_votes_accumulated_poster_wins(self):
        # 3 votes for poster, 2 votes for taker
        choices = ['poster', 'poster', 'taker', 'poster', 'taker']
        for i in range(5):
            client = Client()
            client.login(username=self.voters[i].username, password='password123')
            response = client.post(reverse('submit_dispute_vote', args=[self.dispute.id]), {'choice': choices[i]})

        self.task.refresh_from_db()
        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.poster_profile.rewards, 1200) # 1000 + 200 refund

        # Verify RewardLedger
        ledger = RewardLedger.objects.filter(task=self.task, user=self.poster, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 200)
