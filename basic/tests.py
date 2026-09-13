from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import (
    UserProfile, Task, Dispute, DisputeJury, DisputeVote,
    RewardLedger, Conversation, Notification, Friendship
)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DisputeJuryAndVotingTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.worker = User.objects.create_user(username='worker', password='password123')
        
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.worker_profile, _ = UserProfile.objects.get_or_create(user=self.worker, defaults={'rewards': 500})

        # Create poster friend & worker friend to test exclusion
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile, _ = UserProfile.objects.get_or_create(user=self.poster_friend)
        self.poster_profile.friends.add(self.poster_friend_profile)

        self.worker_friend = User.objects.create_user(username='worker_friend', password='password123')
        self.worker_friend_profile, _ = UserProfile.objects.get_or_create(user=self.worker_friend)
        Friendship.objects.create(from_user=self.worker_profile, to_user=self.worker_friend_profile)

        # Create 5 neutral community members for jury selection
        self.jurors = []
        for i in range(1, 6):
            juror = User.objects.create_user(username=f'juror_{i}', password='password123')
            UserProfile.objects.get_or_create(user=juror)
            self.jurors.append(juror)

        # Create an extra unassigned community member
        self.unassigned_user = User.objects.create_user(username='unassigned', password='password123')
        UserProfile.objects.get_or_create(user=self.unassigned_user)

        # Create task and conversation
        self.task = Task.objects.create(
            title='Test Campus Task',
            description='Help deliver package across campus',
            reward=100,
            posted_by=self.poster,
            taken_by=self.worker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.worker)

    def test_dispute_creation_selects_jury_excluding_participants_and_friends(self):
        client = Client()
        client.login(username='worker', password='password123')

        response = client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task not paid properly'})
        self.task.refresh_from_db()

        self.assertEqual(self.task.status, 'disputed')
        dispute = getattr(self.task, 'dispute', None)
        self.assertIsNotNone(dispute)
        self.assertTrue(hasattr(dispute, 'jury'))

        assigned_juror_ids = list(dispute.jury.jurors.values_list('id', flat=True))
        self.assertEqual(len(assigned_juror_ids), 5)
        self.assertNotIn(self.poster.id, assigned_juror_ids)
        self.assertNotIn(self.worker.id, assigned_juror_ids)
        self.assertNotIn(self.poster_friend.id, assigned_juror_ids)
        self.assertNotIn(self.worker_friend.id, assigned_juror_ids)

        for juror_id in assigned_juror_ids:
            self.assertTrue(Notification.objects.filter(recipient_id=juror_id).exists())

    def test_juror_chat_access_and_read_only(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.worker, reason='Work completed')
        self.task.status = 'disputed'
        self.task.save()
        jury = DisputeJury.objects.create(dispute=dispute)
        jury.jurors.set(self.jurors)

        client = Client()

        # Assigned juror access
        client.login(username='juror_1', password='password123')
        res = client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.context.get('is_read_only'))

        # Unassigned non-participant access should be blocked
        client.login(username='unassigned', password='password123')
        res_blocked = client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertRedirects(res_blocked, reverse('home'))

    def test_voting_majority_resolves_dispute_worker_win(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.worker, reason='Unfair cancellation')
        self.task.status = 'disputed'
        self.task.save()
        jury = DisputeJury.objects.create(dispute=dispute)
        jury.jurors.set(self.jurors)

        client = Client()

        # 1st vote
        client.login(username='juror_1', password='password123')
        client.post(reverse('vote_on_dispute', args=[dispute.id]), {'vote': 'taken_by'})

        # 2nd vote
        client.login(username='juror_2', password='password123')
        client.post(reverse('vote_on_dispute', args=[dispute.id]), {'vote': 'taken_by'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # 3rd vote (majority: 3 out of 5)
        client.login(username='juror_3', password='password123')
        client.post(reverse('vote_on_dispute', args=[dispute.id]), {'vote': 'taken_by'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.worker_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.worker_profile.rewards, 600)  # 500 + 100

        self.assertTrue(RewardLedger.objects.filter(
            user=self.worker,
            task=self.task,
            transaction_type='dispute_payout'
        ).exists())

    def test_voting_majority_resolves_dispute_poster_win(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.worker, reason='Work not done')
        self.task.status = 'disputed'
        self.task.save()
        jury = DisputeJury.objects.create(dispute=dispute)
        jury.jurors.set(self.jurors)

        client = Client()

        # 3 jurors vote for poster
        for juror_user in self.jurors[:3]:
            client.login(username=juror_user.username, password='password123')
            client.post(reverse('vote_on_dispute', args=[dispute.id]), {'vote': 'posted_by'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1100)  # 1000 + 100

        self.assertTrue(RewardLedger.objects.filter(
            user=self.poster,
            task=self.task,
            transaction_type='dispute_refund'
        ).exists())

    def test_vote_immutability_and_non_juror_voting(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.worker, reason='Test dispute')
        self.task.status = 'disputed'
        self.task.save()
        jury = DisputeJury.objects.create(dispute=dispute)
        jury.jurors.set(self.jurors)

        client = Client()

        # Non-juror attempt
        client.login(username='unassigned', password='password123')
        res_non_juror = client.post(reverse('vote_on_dispute', args=[dispute.id]), {'vote': 'taken_by'})
        self.assertRedirects(res_non_juror, reverse('home'))
        self.assertEqual(DisputeVote.objects.count(), 0)

        # Juror votes once
        client.login(username='juror_1', password='password123')
        client.post(reverse('vote_on_dispute', args=[dispute.id]), {'vote': 'taken_by'})
        self.assertEqual(DisputeVote.objects.count(), 1)

        # Juror attempts to vote again
        res_double = client.post(reverse('vote_on_dispute', args=[dispute.id]), {'vote': 'posted_by'})
        self.assertRedirects(res_double, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(DisputeVote.objects.count(), 1)
