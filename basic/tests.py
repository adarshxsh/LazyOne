from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from .models import UserProfile, Task, Dispute, RewardLedger, Notification

class DisputeSettlementTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.staff = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.normal_user = User.objects.create_user(username='normal', password='password123')

        # UserProfiles are created automatically or manually
        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.doer, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.staff, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.normal_user, defaults={'rewards': 1500})

        # Poster creates a task with 100 points
        self.task_reward = 100
        self.poster.userprofile.rewards -= self.task_reward
        self.poster.userprofile.save()
        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task description",
            reward=self.task_reward,
            posted_by=self.poster,
            taken_by=self.doer,
            status='disputed'
        )
        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-self.task_reward,
            transaction_type='task_creation',
            description=f"Reserved for task: '{self.task.title}'"
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason="Work was completed 60% before issue arose."
        )

        self.client = Client()

    def test_staff_partial_settlement_60_40(self):
        self.client.login(username='staff', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'doer_payout': 60, 'poster_refund': 40})

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.poster.userprofile.refresh_from_db()
        self.doer.userprofile.refresh_from_db()
        self.task.refresh_from_db()
        self.dispute.refresh_from_db()

        # Check balances: poster 1400 + 40 = 1440, doer 1500 + 60 = 1560
        self.assertEqual(self.poster.userprofile.rewards, 1440)
        self.assertEqual(self.doer.userprofile.rewards, 1560)

        # Check total points conserved across poster + doer
        total_points = self.poster.userprofile.rewards + self.doer.userprofile.rewards
        self.assertEqual(total_points, 3000)

        # Check statuses
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.doer_payout, 60)
        self.assertEqual(self.dispute.poster_refund, 40)
        self.assertEqual(self.task.status, 'completed')

        # Check ledger entries
        doer_ledger = RewardLedger.objects.get(user=self.doer, transaction_type='dispute_doer_payout')
        self.assertEqual(doer_ledger.amount, 60)
        self.assertEqual(doer_ledger.task, self.task)

        poster_ledger = RewardLedger.objects.get(user=self.poster, transaction_type='dispute_poster_refund')
        self.assertEqual(poster_ledger.amount, 40)
        self.assertEqual(poster_ledger.task, self.task)

        # Check notifications
        self.assertTrue(Notification.objects.filter(recipient=self.doer).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_staff_full_award_100_0(self):
        self.client.login(username='staff', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'doer_payout': 100, 'poster_refund': 0})

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.poster.userprofile.refresh_from_db()
        self.doer.userprofile.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.poster.userprofile.rewards, 1400)
        self.assertEqual(self.doer.userprofile.rewards, 1600)
        self.assertEqual(self.task.status, 'completed')

        self.assertTrue(RewardLedger.objects.filter(user=self.doer, transaction_type='dispute_doer_payout', amount=100).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_poster_refund', amount=0).exists())

    def test_staff_full_refund_0_100(self):
        self.client.login(username='staff', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'doer_payout': 0, 'poster_refund': 100})

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.poster.userprofile.refresh_from_db()
        self.doer.userprofile.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.poster.userprofile.rewards, 1500)
        self.assertEqual(self.doer.userprofile.rewards, 1500)
        self.assertEqual(self.task.status, 'cancelled')

        self.assertTrue(RewardLedger.objects.filter(user=self.doer, transaction_type='dispute_doer_payout', amount=0).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_poster_refund', amount=100).exists())

    def test_validation_sum_mismatch(self):
        self.client.login(username='staff', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'doer_payout': 50, 'poster_refund': 40}) # Sum = 90 != 100

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.poster.userprofile.refresh_from_db()
        self.doer.userprofile.refresh_from_db()
        self.dispute.refresh_from_db()

        # Balances should remain unchanged
        self.assertEqual(self.poster.userprofile.rewards, 1400)
        self.assertEqual(self.doer.userprofile.rewards, 1500)
        self.assertEqual(self.dispute.status, 'open')

    def test_validation_negative_allocation(self):
        self.client.login(username='staff', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'doer_payout': -10, 'poster_refund': 110})

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.poster.userprofile.refresh_from_db()
        self.doer.userprofile.refresh_from_db()
        self.dispute.refresh_from_db()

        self.assertEqual(self.poster.userprofile.rewards, 1400)
        self.assertEqual(self.doer.userprofile.rewards, 1500)
        self.assertEqual(self.dispute.status, 'open')

    def test_non_staff_forbidden(self):
        self.client.login(username='normal', password='password123')
        url = reverse('settle_dispute', args=[self.dispute.id])
        response = self.client.post(url, {'doer_payout': 60, 'poster_refund': 40})

        self.assertRedirects(response, reverse('home'), fetch_redirect_response=False)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_point_conservation_across_ratios(self):
        ratios = [(0, 100), (20, 80), (50, 50), (75, 25), (100, 0)]
        for doer_pts, poster_pts in ratios:
            poster = User.objects.create_user(username=f'poster_{doer_pts}', password='password123')
            doer = User.objects.create_user(username=f'doer_{doer_pts}', password='password123')
            UserProfile.objects.get_or_create(user=poster, defaults={'rewards': 1000})
            UserProfile.objects.get_or_create(user=doer, defaults={'rewards': 1000})

            # Task reward = 100
            poster.userprofile.rewards -= 100
            poster.userprofile.save()

            task = Task.objects.create(
                title=f"Task {doer_pts}", description="desc", reward=100,
                posted_by=poster, taken_by=doer, status='disputed'
            )
            dispute = Dispute.objects.create(task=task, raised_by=doer, reason="reason")

            self.client.login(username='staff', password='password123')
            url = reverse('settle_dispute', args=[dispute.id])
            self.client.post(url, {'doer_payout': doer_pts, 'poster_refund': poster_pts})

            poster.userprofile.refresh_from_db()
            doer.userprofile.refresh_from_db()

            # Initial sum = 2000, final sum must = 2000
            self.assertEqual(poster.userprofile.rewards + doer.userprofile.rewards, 2000)

