from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, Conversation


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

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
        self.assertEqual(dispute.status, 'open')
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
        self.assertEqual(dispute.status, 'resolved')

        # Balance restored: 40 + 60 = 100
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        # Check refund ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 60)

    def test_complete_disputed_task_refunds_deposit(self):
        # Taker raises dispute (deposit 60 deducted from 100 -> 40 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        # Poster marks task as completed
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Taker balance: 40 + 300 (task reward) + 60 (deposit refund) = 400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

        # Check ledger entries for taker
        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 60)

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
