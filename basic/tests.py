from django.test import TestCase, Client
from django.contrib.auth.models import User
from basic.models import UserProfile, Task, Dispute, RewardLedger, Conversation
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

class CustomSplitDisputeTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.doer = User.objects.create_user(username='doer', password='password123')
        self.admin_user = User.objects.create_user(username='admin', password='password123', is_staff=True)
        self.unauthorized_user = User.objects.create_user(username='other', password='password123')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        self.doer_profile, _ = UserProfile.objects.get_or_create(user=self.doer, defaults={'rewards': 1500})
        self.admin_profile, _ = UserProfile.objects.get_or_create(user=self.admin_user, defaults={'rewards': 1500})

        # Poster creates a 100 point task (100 points reserved)
        self.poster_profile.rewards -= 100
        self.poster_profile.save()

        self.task = Task.objects.create(
            title="Sample Task for Dispute Split",
            description="Complete partial work",
            reward=100,
            posted_by=self.poster,
            taken_by=self.doer,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed'
        )
        Conversation.objects.create(task=self.task)

        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-100,
            transaction_type='task_creation',
            description="Reserved for task"
        )

        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.doer,
            reason="Work partially completed, requesting split disbursement."
        )

    def test_poster_custom_split_settlement_success(self):
        client = Client()
        client.login(username='poster', password='password123')

        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = client.post(url, {
            'doer_payout': 60,
            'poster_refund': 40
        })

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        # Refresh objects
        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Check balances
        self.assertEqual(self.doer_profile.rewards, 1560)
        self.assertEqual(self.poster_profile.rewards, 1440)

        # Check statuses
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.doer_payout, 60)
        self.assertEqual(self.dispute.poster_refund, 40)
        self.assertEqual(self.task.status, 'completed')

        # Check twin RewardLedger records
        ledger_doer = RewardLedger.objects.filter(user=self.doer, task=self.task, transaction_type='dispute_payout').first()
        ledger_poster = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_refund').first()

        self.assertIsNotNone(ledger_doer)
        self.assertEqual(ledger_doer.amount, 60)
        self.assertIsNotNone(ledger_poster)
        self.assertEqual(ledger_poster.amount, 40)

    def test_staff_admin_split_settlement_success(self):
        client = Client()
        client.login(username='admin', password='password123')

        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = client.post(url, {
            'doer_payout': 75,
            'poster_refund': 25
        })

        self.assertRedirects(response, reverse('dispute_detail', args=[self.dispute.id]))

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.dispute.refresh_from_db()

        self.assertEqual(self.doer_profile.rewards, 1575)
        self.assertEqual(self.poster_profile.rewards, 1425)
        self.assertEqual(self.dispute.status, 'resolved')

    def test_invalid_split_sum_rejected(self):
        client = Client()
        client.login(username='poster', password='password123')

        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = client.post(url, {
            'doer_payout': 50,
            'poster_refund': 40  # Sum 90 != 100
        }, follow=True)

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.dispute.refresh_from_db()

        # Balances unchanged
        self.assertEqual(self.doer_profile.rewards, 1500)
        self.assertEqual(self.poster_profile.rewards, 1400)
        self.assertEqual(self.dispute.status, 'open')
        self.assertEqual(self.task.status, 'disputed')

    def test_negative_payout_rejected(self):
        client = Client()
        client.login(username='poster', password='password123')

        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = client.post(url, {
            'doer_payout': -10,
            'poster_refund': 110
        }, follow=True)

        self.doer_profile.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.dispute.refresh_from_db()

        self.assertEqual(self.doer_profile.rewards, 1500)
        self.assertEqual(self.poster_profile.rewards, 1400)
        self.assertEqual(self.dispute.status, 'open')

    def test_unauthorized_user_cannot_resolve(self):
        client = Client()
        client.login(username='other', password='password123')

        url = reverse('resolve_dispute', args=[self.dispute.id])
        response = client.post(url, {
            'doer_payout': 50,
            'poster_refund': 50
        }, follow=True)

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')

    def test_dispute_detail_view_renders_form_and_resolved_details(self):
        client = Client()
        client.login(username='poster', password='password123')

        # When open
        response = client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="doer_payout"')
        self.assertContains(response, 'name="poster_refund"')

        # Resolve dispute
        client.post(reverse('resolve_dispute', args=[self.dispute.id]), {
            'doer_payout': 60,
            'poster_refund': 40
        })

        # When resolved
        response = client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Doer Payout')
        self.assertContains(response, '60 points')
        self.assertContains(response, 'Poster Refund')
        self.assertContains(response, '40 points')
