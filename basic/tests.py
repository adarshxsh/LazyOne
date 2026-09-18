import math
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from .models import (
    UserProfile, Task, Dispute, RewardLedger,
    JuryAssignment, DisputeVote, DisputeEvidence,
    Friendship, FriendRequest, Conversation
)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.get_or_create(user=self.poster)[0]
        self.poster_profile.rewards = 1000
        self.poster_profile.save()

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.get_or_create(user=self.taker)[0]
        self.taker_profile.rewards = 100
        self.taker_profile.save()

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
        self.assertEqual(dispute.status, 'VOTING_ACTIVE')
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
        self.assertEqual(dispute.status, 'withdrawn')

        # Balance restored: 40 + 60 = 100
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        # Check refund ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 60)

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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DisputeSystemTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')

        self.poster_prof = UserProfile.objects.get_or_create(user=self.poster)[0]
        self.poster_prof.rewards = 1000
        self.poster_prof.is_phone_verified = True
        self.poster_prof.save()

        self.taker_prof = UserProfile.objects.get_or_create(user=self.taker)[0]
        self.taker_prof.rewards = 500
        self.taker_prof.is_phone_verified = True
        self.taker_prof.save()

        self.task = Task.objects.create(
            title="Fix Landing Page Bug",
            description="Fix responsive design bug on landing page",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        self.juror_users = []
        for i in range(1, 11):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            prof = UserProfile.objects.get_or_create(user=u)[0]
            prof.rewards = 100
            prof.is_phone_verified = True
            prof.save()
            self.juror_users.append(u)

    def test_bilateral_dispute_initiation_by_taker(self):
        self.client.login(username='taker', password='password123')
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client.post(url, {'reason': 'Poster refused to verify completed task'})
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.taker)
        self.assertEqual(dispute.status, 'VOTING_ACTIVE')

        assignments = JuryAssignment.objects.filter(dispute=dispute)
        self.assertEqual(assignments.count(), 5)

        assigned_user_ids = set(assignments.values_list('juror_id', flat=True))
        self.assertNotIn(self.poster.id, assigned_user_ids)
        self.assertNotIn(self.taker.id, assigned_user_ids)

    def test_bilateral_dispute_initiation_by_poster(self):
        self.client.login(username='poster', password='password123')
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client.post(url, {'reason': 'Taker abandoned task without finishing'})
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.poster)
        self.assertEqual(dispute.status, 'VOTING_ACTIVE')

    def test_sampling_excludes_friends(self):
        Friendship.objects.create(from_user=self.poster_prof, to_user=self.juror_users[0].userprofile)
        FriendRequest.objects.create(from_user=self.taker, to_user=self.juror_users[1], is_accepted=True)

        self.client.login(username='taker', password='password123')
        url = reverse('raise_dispute', args=[self.task.id])
        self.client.post(url, {'reason': 'Evidence of work attached'})

        dispute = Dispute.objects.get(task=self.task)
        assigned_user_ids = set(JuryAssignment.objects.filter(dispute=dispute).values_list('juror_id', flat=True))

        self.assertNotIn(self.juror_users[0].id, assigned_user_ids)
        self.assertNotIn(self.juror_users[1].id, assigned_user_ids)

    def test_staked_blind_voting_and_consensus_settlement_taker_win(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task done'})

        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = [assignment.juror for assignment in dispute.jury_assignments.all()]

        voted_jurors = assigned_jurors[:5]
        choices = ['poster', 'poster', 'taker', 'taker', 'taker']

        for juror, choice in zip(voted_jurors, choices):
            self.client.login(username=juror.username, password='password123')
            vote_url = reverse('submit_dispute_vote', args=[dispute.id])
            res = self.client.post(vote_url, {'choice': choice})
            self.assertEqual(res.status_code, 302)

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'RESOLVED')
        self.assertEqual(self.task.status, 'completed')

        self.taker_prof.refresh_from_db()
        self.assertEqual(self.taker_prof.rewards, 700)

        payout_ledger = RewardLedger.objects.filter(
            user=self.taker, task=self.task, transaction_type='dispute_payout'
        )
        self.assertTrue(payout_ledger.exists())
        self.assertEqual(payout_ledger.first().amount, 200)

        for winner in voted_jurors[2:]:
            winner.userprofile.refresh_from_db()
            self.assertGreaterEqual(winner.userprofile.rewards, 106)

    def test_consensus_settlement_poster_win(self):
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Incomplete task'})

        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = [a.juror for a in dispute.jury_assignments.all()]

        for juror in assigned_jurors[:3]:
            self.client.login(username=juror.username, password='password123')
            self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'poster'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'RESOLVED')
        self.assertEqual(self.task.status, 'cancelled')

        self.poster_prof.refresh_from_db()
        self.assertEqual(self.poster_prof.rewards, 1200)

    def test_consensus_settlement_split(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Partial work'})

        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = [a.juror for a in dispute.jury_assignments.all()]

        for juror in assigned_jurors[:3]:
            self.client.login(username=juror.username, password='password123')
            self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'split'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'RESOLVED')

        self.taker_prof.refresh_from_db()
        self.poster_prof.refresh_from_db()

        self.assertEqual(self.taker_prof.rewards, 600)
        self.assertEqual(self.poster_prof.rewards, 1100)

    def test_dispute_expiration_fallback(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Test expiration'})

        dispute = Dispute.objects.get(task=self.task)
        assigned_jurors = [a.juror for a in dispute.jury_assignments.all()]

        for juror in assigned_jurors[:2]:
            self.client.login(username=juror.username, password='password123')
            self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        dispute.voting_deadline = timezone.now() - timedelta(hours=1)
        dispute.save()

        self.client.login(username='taker', password='password123')
        self.client.get(reverse('dispute_detail', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'EXPIRED_FALLBACK')

        for juror in assigned_jurors[:2]:
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 100)

    def test_unauthorized_user_cannot_access_or_vote(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Test auth'})

        dispute = Dispute.objects.get(task=self.task)

        outsider = User.objects.create_user(username='outsider', password='password123')
        UserProfile.objects.create(user=outsider, rewards=100)

        self.client.login(username='outsider', password='password123')
        res_view = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(res_view.status_code, 302)

        res_vote = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})
        self.assertEqual(res_vote.status_code, 302)
        self.assertEqual(dispute.votes.count(), 0)

    def test_evidence_submission(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Work submitted'})

        dispute = Dispute.objects.get(task=self.task)
        ev_url = reverse('submit_dispute_evidence', args=[dispute.id])
        res = self.client.post(ev_url, {'text': 'Here is screenshot proof of work completed'})
        self.assertEqual(res.status_code, 302)

        self.assertEqual(dispute.evidence_entries.count(), 1)
        self.assertEqual(dispute.evidence_entries.first().text, 'Here is screenshot proof of work completed')

    def test_chat_view_juror_access(self):
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Chat review test'})

        dispute = Dispute.objects.get(task=self.task)
        assigned_juror = dispute.jury_assignments.first().juror

        self.client.login(username=assigned_juror.username, password='password123')
        chat_url = reverse('chat_view', args=[self.conversation.id])
        res = self.client.get(chat_url)
        self.assertEqual(res.status_code, 200)
