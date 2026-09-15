from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, DisputeVote
from basic.views.dispute import resolve_dispute

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class EscrowLockAndJurorIncentivesTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')

        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1500)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=1500)
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=1500)

        self.client = Client()

    def test_poster_cannot_complete_disputed_task(self):
        task = Task.objects.create(
            title="Test Task",
            description="Details",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )
        Dispute.objects.create(task=task, raised_by=self.taker, reason="Work unfinished")

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[task.id]), follow=True)

        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        self.assertEqual(self.taker_profile.rewards, 1500)
        messages = list(response.context['messages'])
        self.assertTrue(any("Disputed tasks cannot be manually completed." in str(m) for m in messages))

    def test_raise_dispute_records_dispute_hold_ledger(self):
        task = Task.objects.create(
            title="In Progress Task",
            description="Details",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='in_progress'
        )

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[task.id]), {'reason': 'Quality issue'}, follow=True)

        task.refresh_from_db()
        self.assertEqual(task.status, 'disputed')
        dispute_hold_ledger = RewardLedger.objects.filter(
            task=task,
            transaction_type='dispute_hold',
            user=self.poster
        ).first()
        self.assertIsNotNone(dispute_hold_ledger)
        self.assertEqual(dispute_hold_ledger.amount, 300)

    def test_dispute_resolution_with_jurors_taker_wins(self):
        task = Task.objects.create(
            title="Disputed Task Taker Wins",
            description="Details",
            reward=1000,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )
        dispute = Dispute.objects.create(task=task, raised_by=self.taker, reason="Disagreed on requirements")

        DisputeVote.objects.create(dispute=dispute, voter=self.juror1, choice='taker', voted_for=self.taker)
        DisputeVote.objects.create(dispute=dispute, voter=self.juror2, choice='taker', voted_for=self.taker)

        resolve_dispute(dispute)

        task.refresh_from_db()
        dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.juror1_profile.refresh_from_db()
        self.juror2_profile.refresh_from_db()

        self.assertEqual(task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')

        # Total reward = 1000. 10% juror fee = 100. Split between 2 jurors = 50 each.
        self.assertEqual(self.juror1_profile.rewards, 1550)
        self.assertEqual(self.juror2_profile.rewards, 1550)

        j1_ledger = RewardLedger.objects.get(user=self.juror1, transaction_type='juror_reward', task=task)
        j2_ledger = RewardLedger.objects.get(user=self.juror2, transaction_type='juror_reward', task=task)
        self.assertEqual(j1_ledger.amount, 50)
        self.assertEqual(j2_ledger.amount, 50)

        # Net payout for taker = 1000 - 100 = 900.
        self.assertEqual(self.taker_profile.rewards, 2400) # 1500 + 900
        payout_ledger = RewardLedger.objects.get(user=self.taker, transaction_type='dispute_payout', task=task)
        self.assertEqual(payout_ledger.amount, 900)

    def test_dispute_resolution_with_jurors_poster_wins(self):
        task = Task.objects.create(
            title="Disputed Task Poster Wins",
            description="Details",
            reward=1000,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )
        dispute = Dispute.objects.create(task=task, raised_by=self.taker, reason="Disagreed on requirements")

        DisputeVote.objects.create(dispute=dispute, voter=self.juror1, choice='poster', voted_for=self.poster)
        DisputeVote.objects.create(dispute=dispute, voter=self.juror2, choice='poster', voted_for=self.poster)

        resolve_dispute(dispute)

        task.refresh_from_db()
        dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.juror1_profile.refresh_from_db()
        self.juror2_profile.refresh_from_db()

        self.assertEqual(task.status, 'cancelled')
        self.assertEqual(dispute.status, 'resolved')

        # Jurors get 50 each
        self.assertEqual(self.juror1_profile.rewards, 1550)
        self.assertEqual(self.juror2_profile.rewards, 1550)

        # Poster gets net refund = 900
        self.assertEqual(self.poster_profile.rewards, 2400) # 1500 + 900
        refund_ledger = RewardLedger.objects.get(user=self.poster, transaction_type='dispute_refund', task=task)
        self.assertEqual(refund_ledger.amount, 900)

    def test_pending_rewards_includes_in_progress_and_disputed(self):
        t1 = Task.objects.create(
            title="In Progress Task", description="Details", reward=400,
            posted_by=self.poster, taken_by=self.taker, status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        t2 = Task.objects.create(
            title="Disputed Task", description="Details", reward=600,
            posted_by=self.poster, taken_by=self.taker, status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('rewards'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['pending_points'], 1000)

    def test_transaction_type_schema_choices(self):
        valid_types = dict(RewardLedger.TRANSACTION_TYPES)
        self.assertIn('dispute_hold', valid_types)
        self.assertIn('dispute_payout', valid_types)
        self.assertIn('dispute_refund', valid_types)
        self.assertIn('juror_reward', valid_types)

    def test_submit_dispute_vote_and_auto_resolution(self):
        task = Task.objects.create(
            title="Auto Resolution Task", description="Details", reward=500,
            posted_by=self.poster, taken_by=self.taker, status='disputed',
            deadline=timezone.now() + timedelta(days=1)
        )
        dispute = Dispute.objects.create(task=task, raised_by=self.taker, reason="Disagreement")

        jurors = []
        for i in range(5):
            u = User.objects.create_user(username=f'juror_vote_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=1000)
            jurors.append(u)

        # First 4 jurors vote for taker
        for u in jurors[:4]:
            self.client.login(username=u.username, password='password123')
            res = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'}, follow=True)
            self.assertEqual(res.status_code, 200)

        dispute.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # 5th juror votes for taker -> triggers auto resolution
        self.client.login(username=jurors[4].username, password='password123')
        res = self.client.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'}, follow=True)

        dispute.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(task.status, 'completed')

        # 500 reward * 10% = 50 total juror fee. Split between 5 jurors = 10 each.
        for u in jurors:
            u.userprofile.refresh_from_db()
            self.assertEqual(u.userprofile.rewards, 1010) # 1000 + 10
            jl = RewardLedger.objects.get(user=u, transaction_type='juror_reward', task=task)
            self.assertEqual(jl.amount, 10)

        # Taker receives net payout 500 - 50 = 450
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1950) # 1500 + 450
        pl = RewardLedger.objects.get(user=self.taker, transaction_type='dispute_payout', task=task)
        self.assertEqual(pl.amount, 450)

