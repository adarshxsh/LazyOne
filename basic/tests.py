from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from basic.models import UserProfile, Task, Dispute, JuryMember, RewardLedger, Friendship

class PeerJuryDisputeTests(TestCase):
    def setUp(self):
        # Create poster and taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})

        # Create friends
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        pf_profile, _ = UserProfile.objects.get_or_create(user=self.poster_friend, defaults={'rewards': 500})
        tf_profile, _ = UserProfile.objects.get_or_create(user=self.taker_friend, defaults={'rewards': 500})

        self.poster.userprofile.friends.add(pf_profile)
        self.taker.userprofile.friends.add(tf_profile)

        # Create user with task co-participation history with poster
        self.co_participant = User.objects.create_user(username='co_participant', password='password123')
        UserProfile.objects.get_or_create(user=self.co_participant, defaults={'rewards': 500})
        Task.objects.create(
            title='Past Task',
            description='Old task',
            reward=100,
            posted_by=self.poster,
            taken_by=self.co_participant,
            status='completed'
        )

        # Create 5 neutral users
        self.neutral_users = []
        for i in range(5):
            u = User.objects.create_user(username=f'neutral_{i}', password='password123')
            UserProfile.objects.get_or_create(user=u, defaults={'rewards': 500})
            self.neutral_users.append(u)

        # Create unauthorized user
        self.unauthorized_user = User.objects.create_user(username='unauthorized', password='password123')
        UserProfile.objects.get_or_create(user=self.unauthorized_user, defaults={'rewards': 500})

        # Create active task
        self.task = Task.objects.create(
            title='Disputed Task',
            description='A task that will be disputed',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

    def test_dispute_creation_selects_neutral_jurors(self):
        client = Client()
        client.login(username='taker', password='password123')

        response = client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Unfinished work'})
        self.assertEqual(response.status_code, 302)

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')

        # Check jurors assigned
        jurors = JuryMember.objects.filter(dispute=dispute)
        self.assertEqual(jurors.count(), 5)

        juror_user_ids = set(jurors.values_list('user_id', flat=True))

        # Verify poster and taker excluded
        self.assertNotIn(self.poster.id, juror_user_ids)
        self.assertNotIn(self.taker.id, juror_user_ids)

        # Verify friends excluded
        self.assertNotIn(self.poster_friend.id, juror_user_ids)
        self.assertNotIn(self.taker_friend.id, juror_user_ids)

        # Verify co-participant excluded
        self.assertNotIn(self.co_participant.id, juror_user_ids)

        # Verify all assigned jurors are from eligible neutral users set
        eligible_neutral_ids = {u.id for u in self.neutral_users} | {self.unauthorized_user.id}
        self.assertTrue(juror_user_ids.issubset(eligible_neutral_ids))

    def test_access_control_for_dispute_voting_page(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute test')
        # Assign neutral users as jurors
        for u in self.neutral_users:
            JuryMember.objects.create(dispute=dispute, user=u)

        client = Client()

        # Assigned juror can access
        client.login(username='neutral_0', password='password123')
        resp = client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)

        # Disputing party (poster/taker) can access detail
        client.login(username='poster', password='password123')
        resp = client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)

        # Unauthorized non-juror non-participant is denied access
        client.login(username='unauthorized', password='password123')
        resp = client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 302)

        # Unauthorized non-juror trying to vote is denied access
        resp_vote = client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'poster'})
        self.assertEqual(resp_vote.status_code, 302)

    def test_vote_submission_and_immutability(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute test')
        for u in self.neutral_users:
            JuryMember.objects.create(dispute=dispute, user=u)

        client = Client()
        client.login(username='neutral_0', password='password123')

        # First vote
        resp = client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'poster'})
        self.assertEqual(resp.status_code, 302)

        juror = JuryMember.objects.get(dispute=dispute, user=self.neutral_users[0])
        self.assertEqual(juror.vote, 'poster')

        # Cannot change vote
        resp_change = client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'taker'})
        self.assertEqual(resp_change.status_code, 302)
        juror.refresh_from_db()
        self.assertEqual(juror.vote, 'poster')

    def test_majority_consensus_resolves_poster_wins(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute test')
        for u in self.neutral_users:
            JuryMember.objects.create(dispute=dispute, user=u)

        client = Client()

        # 3 jurors vote 'poster'
        for i in range(3):
            client.login(username=f'neutral_{i}', password='password123')
            client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'poster'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.winning_party, 'poster')
        self.assertEqual(dispute.winner, self.poster)
        self.assertEqual(self.task.status, 'cancelled')

        # Verify refund to poster
        self.poster.userprofile.refresh_from_db()
        self.assertEqual(self.poster.userprofile.rewards, 1200) # 1000 initial + 200 task reward

        # Verify winning jurors rewarded
        for i in range(3):
            self.neutral_users[i].userprofile.refresh_from_db()
            self.assertEqual(self.neutral_users[i].userprofile.rewards, 550) # 500 + 50 bonus

        # Verify reward ledger entries
        ledger_poster = RewardLedger.objects.filter(user=self.poster, transaction_type='task_cancellation')
        self.assertTrue(ledger_poster.exists())

        ledger_juror = RewardLedger.objects.filter(user=self.neutral_users[0], transaction_type='jury_reward')
        self.assertTrue(ledger_juror.exists())

    def test_majority_consensus_resolves_taker_wins(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute test')
        for u in self.neutral_users:
            JuryMember.objects.create(dispute=dispute, user=u)

        client = Client()

        # 3 jurors vote 'taker'
        for i in range(3):
            client.login(username=f'neutral_{i}', password='password123')
            client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'taker'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.winning_party, 'taker')
        self.assertEqual(dispute.winner, self.taker)
        self.assertEqual(self.task.status, 'completed')

        # Verify reward to taker
        self.taker.userprofile.refresh_from_db()
        self.assertEqual(self.taker.userprofile.rewards, 700) # 500 initial + 200 task reward

        # Verify winning jurors rewarded
        for i in range(3):
            self.neutral_users[i].userprofile.refresh_from_db()
            self.assertEqual(self.neutral_users[i].userprofile.rewards, 550) # 500 + 50 bonus

    def test_48_hour_timeout_staff_escalation(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute test')
        for u in self.neutral_users:
            JuryMember.objects.create(dispute=dispute, user=u)

        # Set dispute creation time 49 hours ago
        dispute.created_at = timezone.now() - timedelta(hours=49)
        dispute.save()

        # Check escalation
        escalated = dispute.check_and_escalate()
        self.assertTrue(escalated)
        self.assertEqual(dispute.status, 'escalated')

        # Accessing detail view triggers escalation as well
        client = Client()
        client.login(username='neutral_0', password='password123')
        resp = client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['dispute'].status, 'escalated')
