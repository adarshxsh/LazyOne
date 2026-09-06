from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger

class DisputeSplitSettlementTests(TestCase):
    def setUp(self):
        # Create regular users (poster and taker)
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')

        # Create profiles
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Create staff user
        self.staff_user = User.objects.create_user(username='staff_admin', password='password123', is_staff=True)
        self.staff_profile = UserProfile.objects.create(user=self.staff_user, rewards=1000)

        # Create task with reward = 100 points
        self.task = Task.objects.create(
            title='Test Task for Dispute',
            description='Test task description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )

        # Poster's balance reduced by 100 on creation, simulate 900
        self.poster_profile.rewards = 900
        self.poster_profile.save()

        # Create dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Work partially completed, unable to reach agreement.',
            status='open'
        )

        self.client = Client()

    def test_non_staff_blocked_from_admin_dispute_panel(self):
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('admin_dispute_panel'))
        self.assertEqual(response.status_code, 403)

    def test_non_staff_blocked_from_settling_dispute(self):
        self.client.login(username='poster', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'taker_payout': 50, 'poster_refund': 50})
        self.assertEqual(response.status_code, 403)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_staff_can_access_admin_dispute_panel(self):
        self.client.login(username='staff_admin', password='password123')
        response = self.client.get(reverse('admin_dispute_panel'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Task for Dispute')
        self.assertContains(response, '100 Points')

    def test_settlement_rejected_when_split_sum_mismatch(self):
        self.client.login(username='staff_admin', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])

        # Task reward is 100; submit 60 + 50 = 110
        response = self.client.post(url, {'taker_payout': 60, 'poster_refund': 50}, follow=True)
        self.assertContains(response, 'must equal the total task reward')

        # Verify no changes to balances or status
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(self.poster_profile.rewards, 900)
        self.assertEqual(self.taker_profile.rewards, 500)

    def test_settlement_rejected_when_negative_amounts(self):
        self.client.login(username='staff_admin', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])

        response = self.client.post(url, {'taker_payout': -10, 'poster_refund': 110}, follow=True)
        self.assertContains(response, 'must be non-negative integers')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_successful_split_settlement_atomic_execution(self):
        self.client.login(username='staff_admin', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])

        # Settle: 70 points to taker, 30 points refunded to poster (70 + 30 = 100)
        response = self.client.post(url, {'taker_payout': 70, 'poster_refund': 30}, follow=True)
        self.assertContains(response, 'Dispute resolved successfully')

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        # Check statuses
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Check balances
        self.assertEqual(self.taker_profile.rewards, 500 + 70) # 570
        self.assertEqual(self.poster_profile.rewards, 900 + 30) # 930

        # Check ledger entries
        taker_ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='dispute_payout').first()
        poster_ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_refund').first()

        self.assertIsNotNone(taker_ledger)
        self.assertEqual(taker_ledger.amount, 70)
        self.assertIn("Dispute settlement payout", taker_ledger.description)

        self.assertIsNotNone(poster_ledger)
        self.assertEqual(poster_ledger.amount, 30)
        self.assertIn("Dispute settlement refund", poster_ledger.description)

    def test_already_resolved_dispute_cannot_be_settled_again(self):
        self.dispute.status = 'resolved'
        self.dispute.save()

        self.client.login(username='staff_admin', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'taker_payout': 50, 'poster_refund': 50}, follow=True)
        self.assertContains(response, 'already been resolved')

