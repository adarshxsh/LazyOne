from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation


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


class TaskAbandonmentRewardSlashingTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})

        self.other_user = User.objects.create_user(username='other_user', password='password123')
        self.other_profile, _ = UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 1500})

        self.client = Client()

    def test_abandon_task_deducts_penalty_and_creates_ledger(self):
        self.client.login(username='poster', password='password123')
        # Poster creates a task with reward = 100
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('add_task'), {
            'title': 'Build Feature',
            'description': 'Description here',
            'reward': '100',
            'deadline': deadline
        })
        task = Task.objects.get(title='Build Feature')
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1400) # 1500 - 100

        # Taker takes the task
        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.taker)

        # Taker abandons the task
        abandon_url = reverse('abandon_task', args=[task.id])
        response = self.client.get(abandon_url)
        self.assertRedirects(response, reverse('my_tasks'))

        # Check task state
        task.refresh_from_db()
        self.assertEqual(task.status, 'available')
        self.assertIsNone(task.taken_by)

        # Check taker reward balance (1500 - 20 = 1480)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1480)

        # Check poster reward balance was not altered by abandonment
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1400)

        # Check ledger transaction entry
        ledger_entry = RewardLedger.objects.filter(
            user=self.taker,
            transaction_type='task_abandonment'
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, -20)
        self.assertEqual(ledger_entry.task, task)
        self.assertIn("Penalty for abandoned task", ledger_entry.description)
        self.assertIn("Build Feature", ledger_entry.description)

    def test_abandon_task_rounding(self):
        self.client.login(username='poster', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        # Task with 15 points -> 20% of 15 is 3
        self.client.post(reverse('add_task'), {
            'title': 'Task 15',
            'description': 'Description',
            'reward': '15',
            'deadline': deadline
        })
        task15 = Task.objects.get(title='Task 15')

        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task15.id]))
        self.client.get(reverse('abandon_task', args=[task15.id]))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1497) # 1500 - 3

        ledger15 = RewardLedger.objects.get(user=self.taker, task=task15)
        self.assertEqual(ledger15.amount, -3)

        # Task with 7 points -> 20% of 7 is 1.4 -> rounds to 1
        self.client.login(username='poster', password='password123')
        self.client.post(reverse('add_task'), {
            'title': 'Task 7',
            'description': 'Description',
            'reward': '7',
            'deadline': deadline
        })
        task7 = Task.objects.get(title='Task 7')

        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task7.id]))
        self.client.get(reverse('abandon_task', args=[task7.id]))

        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1496) # 1497 - 1

        ledger7 = RewardLedger.objects.get(user=self.taker, task=task7)
        self.assertEqual(ledger7.amount, -1)

    def test_abandon_task_appears_in_rewards_history(self):
        self.client.login(username='poster', password='password123')
        deadline = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('add_task'), {
            'title': 'Design Task',
            'description': 'Description',
            'reward': '50',
            'deadline': deadline
        })
        task = Task.objects.get(title='Design Task')

        self.client.login(username='taker', password='password123')
        self.client.get(reverse('take_task', args=[task.id]))
        self.client.get(reverse('abandon_task', args=[task.id]))

        # View rewards page
        response = self.client.get(reverse('rewards'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Penalty for abandoned task: &#x27;Design Task&#x27;")
        self.assertContains(response, "-10")

    def test_unauthorized_or_invalid_abandonment(self):
        task = Task.objects.create(
            title='Test Task',
            description='Desc',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        Conversation.objects.create(task=task)

        # Unauthenticated user redirected to login
        abandon_url = reverse('abandon_task', args=[task.id])
        response = self.client.get(abandon_url)
        self.assertRedirects(response, f"/login/?next={abandon_url}")

        # Non-taker user gets 404
        self.client.login(username='other_user', password='password123')
        response = self.client.get(abandon_url)
        self.assertEqual(response.status_code, 404)
