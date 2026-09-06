from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, Conversation, DisputeVote, UserProfile, RewardLedger

class DisputeChatAndVotingTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.voter1 = User.objects.create_user(username='voter1', password='password123')
        self.voter2 = User.objects.create_user(username='voter2', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)

        # Create profiles
        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.voter1, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.voter2, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 1000})

        # Create task in progress
        self.task = Task.objects.create(
            title='Test Disputed Task',
            description='Description of task',
            reward=100,
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
            reason='Work was done properly but poster refuses to acknowledge.'
        )

    def test_community_member_can_view_disputed_chat_read_only(self):
        client = Client()
        client.login(username='voter1', password='password123')
        response = client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])
        self.assertTrue(response.context['can_vote'])

    def test_non_participant_denied_send_message_in_disputed_chat(self):
        client = Client()
        client.login(username='voter1', password='password123')
        response = client.post(reverse('send_message', args=[self.conversation.id]), {'content': 'Hello'})
        self.assertEqual(response.status_code, 403)

    def test_community_member_can_cast_vote(self):
        client = Client()
        client.login(username='voter1', password='password123')
        response = client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for_id': self.taker.id},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(DisputeVote.objects.filter(dispute=self.dispute, voter=self.voter1, voted_for=self.taker).exists())

    def test_task_parties_cannot_vote_on_own_dispute(self):
        client = Client()
        # Poster attempts to vote
        client.login(username='poster', password='password123')
        response = client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for_id': self.poster.id},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.poster).exists())

        # Taker attempts to vote
        client.login(username='taker', password='password123')
        response = client.post(
            reverse('vote_dispute', args=[self.dispute.id]),
            {'voted_for_id': self.taker.id},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DisputeVote.objects.filter(dispute=self.dispute, voter=self.taker).exists())

    def test_voter_cannot_cast_multiple_votes(self):
        client = Client()
        client.login(username='voter1', password='password123')

        # First vote
        client.post(reverse('vote_dispute', args=[self.dispute.id]), {'voted_for_id': self.taker.id})
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute, voter=self.voter1).count(), 1)

        # Duplicate vote attempt
        client.post(reverse('vote_dispute', args=[self.dispute.id]), {'voted_for_id': self.poster.id})
        self.assertEqual(DisputeVote.objects.filter(dispute=self.dispute, voter=self.voter1).count(), 1)

    def test_staff_view_vote_tallies(self):
        # Create votes
        DisputeVote.objects.create(dispute=self.dispute, voter=self.voter1, voted_for=self.taker)
        DisputeVote.objects.create(dispute=self.dispute, voter=self.voter2, voted_for=self.poster)

        client = Client()

        # Non-staff user should NOT see vote tallies
        client.login(username='poster', password='password123')
        response = client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('show_vote_tallies', response.context)

        # Staff user DOES see vote tallies
        client.login(username='staff', password='password123')
        response = client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get('show_vote_tallies'))
        self.assertEqual(response.context['total_votes'], 2)
        self.assertEqual(response.context['taker_votes'], 1)
        self.assertEqual(response.context['poster_votes'], 1)

    def test_manual_staff_dispute_settlement(self):
        client = Client()

        # Non-staff settlement attempt blocked
        client.login(username='voter1', password='password123')
        response = client.post(reverse('settle_dispute', args=[self.dispute.id]), {'winner': 'taker'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Dispute.objects.get(id=self.dispute.id).status, 'open')

        # Staff settlement in favor of taker
        client.login(username='staff', password='password123')
        taker_initial_rewards = self.taker.userprofile.rewards
        response = client.post(reverse('settle_dispute', args=[self.dispute.id]), {'winner': 'taker'}, follow=True)
        self.assertEqual(response.status_code, 200)

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker.userprofile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker.userprofile.rewards, taker_initial_rewards + 100)
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, task=self.task, amount=100).exists())

