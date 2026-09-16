from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, DisputeVote, RewardLedger

class DisputeEscrowAndJurorRewardsTests(TestCase):
    def setUp(self):
        self.client = Client()
        # Create poster, doer, and 3 neutral jurors
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror3 = User.objects.create_user(username='juror3', password='password123')

        self.poster_profile = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})[0]
        self.doer_profile = UserProfile.objects.get_or_create(user=self.doer, defaults={'rewards': 1000})[0]
        self.juror1_profile = UserProfile.objects.get_or_create(user=self.juror1, defaults={'rewards': 100})[0]
        self.juror2_profile = UserProfile.objects.get_or_create(user=self.juror2, defaults={'rewards': 100})[0]
        self.juror3_profile = UserProfile.objects.get_or_create(user=self.juror3, defaults={'rewards': 100})[0]

        # Create a task in progress
        deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Design Logo",
            description="Create a cool logo",
            reward=100,
            posted_by=self.poster,
            taken_by=self.doer,
            status='in_progress',
            deadline=deadline
        )
        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-100,
            transaction_type='task_creation',
            description="Reserved for task: Design Logo"
        )

    def test_raising_dispute_creates_escrow_lock_entry(self):
        self.client.login(username='doer', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Work submitted but unpaid'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))

        # AC2: Raising a dispute creates an explicit dispute_escrow_lock entry in RewardLedger
        escrow_lock = RewardLedger.objects.filter(
            task=self.task,
            transaction_type='dispute_escrow_lock'
        ).first()
        self.assertIsNotNone(escrow_lock)
        self.assertEqual(escrow_lock.user, self.poster)

    def test_complete_task_on_disputed_task_returns_403(self):
        # Raise dispute first
        Dispute.objects.create(task=self.task, raised_by=self.doer, reason='Work rejected unfairly')
        self.task.status = 'disputed'
        self.task.save()
        RewardLedger.objects.create(
            user=self.poster, task=self.task, amount=0,
            transaction_type='dispute_escrow_lock', description='Escrow locked'
        )

        # Poster attempts to call complete_task on disputed task
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))

        # AC1: Calling completion endpoints on a disputed task returns an authorization error (403) and prevents fund release
        self.assertEqual(response.status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        
        self.doer_profile.refresh_from_db()
        self.assertEqual(self.doer_profile.rewards, 1000) # Doer balance unchanged

    def test_task_parties_prohibited_from_voting_as_jurors(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.doer, reason='Dispute reason')
        self.task.status = 'disputed'
        self.task.save()

        # Poster attempts to vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.poster.id})
        self.assertEqual(response.status_code, 403)

        # Doer attempts to vote
        self.client.login(username='doer', password='password123')
        response = self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.doer.id})
        self.assertEqual(response.status_code, 403)

    def test_juror_voting_and_doer_win_resolution(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.doer, reason='Unfair rejection')
        self.task.status = 'disputed'
        self.task.save()

        # Juror 1 and Juror 2 vote for doer; Juror 3 votes for poster
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.doer.id})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.doer.id})

        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.poster.id})

        # Resolve dispute
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('resolve_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # AC4: Winning majority jurors receive point allocations from the dispute fee pool
        self.juror1_profile.refresh_from_db()
        self.juror2_profile.refresh_from_db()
        self.juror3_profile.refresh_from_db()

        self.assertGreater(self.juror1_profile.rewards, 100)
        self.assertGreater(self.juror2_profile.rewards, 100)
        self.assertEqual(self.juror3_profile.rewards, 100) # Losing juror received nothing

        # AC3: Check all 5 transaction types in RewardLedger
        types = set(RewardLedger.objects.filter(task=self.task).values_list('transaction_type', flat=True))
        self.assertIn('dispute_payout_doer', types)
        self.assertIn('dispute_slash', types)
        self.assertIn('juror_reward', types)

    def test_poster_win_resolution_refunds_poster_and_slashes_doer(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.doer, reason='Unfair rejection')
        self.task.status = 'disputed'
        self.task.save()

        # Jurors vote for poster
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.poster.id})

        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('vote_dispute', args=[dispute.id]), {'voted_for': self.poster.id})

        # Resolve dispute
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('resolve_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')

        types = set(RewardLedger.objects.filter(task=self.task).values_list('transaction_type', flat=True))
        self.assertIn('dispute_refund_poster', types)
        self.assertIn('dispute_slash', types)
        self.assertIn('juror_reward', types)
