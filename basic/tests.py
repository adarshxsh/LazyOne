from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, RewardLedger, Conversation, FriendRequest, Friendship
import basic.stripe_utils as stripe_utils

class StripeFiatEscrowTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.creator_user = User.objects.create_user(username='creator', password='password', email='creator@example.com')
        self.worker_user = User.objects.create_user(username='worker', password='password', email='worker@example.com')
        
        self.creator_profile = UserProfile.objects.get_or_create(user=self.creator_user)[0]
        self.worker_profile = UserProfile.objects.get_or_create(user=self.worker_user)[0]
        
        # Log in creator
        self.client.login(username='creator', password='password')

    def test_stripe_connect_onboarding_initiates_and_callbacks(self):
        # 1. Onboarding initiates
        response = self.client.get(reverse('stripe_connect'))
        self.assertEqual(response.status_code, 302) # Redirects to onboarding URL
        
        self.creator_profile.refresh_from_db()
        self.assertTrue(self.creator_profile.stripe_account_id.startswith('acct_mock_'))

        # 2. Onboarding callback success
        response = self.client.get(reverse('stripe_callback') + '?status=success')
        self.assertEqual(response.status_code, 302) # Redirects to profile
        
        # 3. Onboarding callback refresh
        response = self.client.get(reverse('stripe_callback') + '?status=refresh')
        self.assertEqual(response.status_code, 302) # Redirects to stripe_connect

    def test_usd_task_creation_escrows_funds_and_registers_ledger(self):
        # Create USD task
        deadline = (timezone.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        post_data = {
            'title': 'USD Professional Task',
            'description': 'Help with coding homework',
            'reward': '50', # $50
            'reward_type': 'usd',
            'deadline': deadline,
        }
        
        response = self.client.post(reverse('add_task'), post_data)
        self.assertEqual(response.status_code, 302) # Redirects to home
        
        # Verify task is created in DB
        task = Task.objects.get(title='USD Professional Task')
        self.assertEqual(task.reward_type, 'usd')
        self.assertEqual(task.reward, 50)
        self.assertTrue(task.stripe_payment_intent_id.startswith('pi_mock_'))
        self.assertEqual(task.status, 'available')
        
        # Verify ledger entry
        ledger_entry = RewardLedger.objects.get(task=task, user=self.creator_user)
        self.assertEqual(ledger_entry.amount, -5000) # $50 in cents
        self.assertEqual(ledger_entry.currency, 'usd')
        self.assertEqual(ledger_entry.transaction_type, 'task_creation')

    def test_taking_usd_task_validates_worker_stripe_connection(self):
        # Create USD task first
        task = Task.objects.create(
            title='USD Task', description='Desc', reward=20,
            posted_by=self.creator_user, status='available', reward_type='usd',
            stripe_payment_intent_id='pi_mock_123'
        )
        
        # Log in worker
        self.client.login(username='worker', password='password')
        
        # Try to take task without Stripe Connect
        response = self.client.get(reverse('take_task', args=[task.id]))
        self.assertEqual(response.status_code, 302) # Redirects to profile page to onboard
        
        # Now onboard worker
        self.worker_profile.stripe_account_id = 'acct_mock_worker'
        self.worker_profile.save()
        
        # Try to take task again
        response = self.client.get(reverse('take_task', args=[task.id]))
        self.assertEqual(response.status_code, 302) # Success, redirects to my_tasks
        
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertEqual(task.taken_by, self.worker_user)

    def test_completing_usd_task_triggers_payout_and_ledger(self):
        # Create USD task in_progress
        self.worker_profile.stripe_account_id = 'acct_mock_worker'
        self.worker_profile.save()
        
        task = Task.objects.create(
            title='USD Task Complete', description='Desc', reward=30,
            posted_by=self.creator_user, taken_by=self.worker_user, status='in_progress',
            reward_type='usd', stripe_payment_intent_id='pi_mock_123'
        )
        
        # Log in creator to complete task
        self.client.login(username='creator', password='password')
        response = self.client.get(reverse('complete_task', args=[task.id]))
        self.assertEqual(response.status_code, 302) # Redirects to my_tasks
        
        task.refresh_from_db()
        self.assertEqual(task.status, 'completed')
        self.assertTrue(task.stripe_transfer_id.startswith('tr_mock_'))
        
        # Verify worker received payment in ledger
        ledger_entry = RewardLedger.objects.get(task=task, user=self.worker_user)
        self.assertEqual(ledger_entry.amount, 3000) # $30 in cents
        self.assertEqual(ledger_entry.currency, 'usd')
        self.assertEqual(ledger_entry.transaction_type, 'task_completion')

    def test_cancelling_available_usd_task_refunds_funds(self):
        # Create available USD task
        task = Task.objects.create(
            title='USD Task Cancel', description='Desc', reward=15,
            posted_by=self.creator_user, status='available',
            reward_type='usd', stripe_payment_intent_id='pi_mock_123'
        )
        
        # Cancel task
        response = self.client.get(reverse('cancel_task', args=[task.id]))
        self.assertEqual(response.status_code, 302) # Redirects to my_tasks
        
        task.refresh_from_db()
        self.assertEqual(task.status, 'cancelled')
        self.assertTrue(task.stripe_refund_id.startswith('re_mock_'))
        
        # Verify ledger has positive refund
        ledger_entry = RewardLedger.objects.get(task=task, user=self.creator_user, transaction_type='task_cancellation')
        self.assertEqual(ledger_entry.amount, 1500) # $15 refund in cents
        self.assertEqual(ledger_entry.currency, 'usd')

    def test_accept_cancellation_mutually_refunds_fiat_task(self):
        # Create in_progress USD task with cancellation requested
        task = Task.objects.create(
            title='USD Task Mutual Cancel', description='Desc', reward=25,
            posted_by=self.creator_user, taken_by=self.worker_user, status='in_progress',
            cancellation_requested=True, reward_type='usd', stripe_payment_intent_id='pi_mock_123'
        )
        
        # Log in worker to accept cancellation
        self.client.login(username='worker', password='password')
        response = self.client.get(reverse('accept_cancellation', args=[task.id]))
        self.assertEqual(response.status_code, 302) # Redirects to my_tasks
        
        task.refresh_from_db()
        self.assertEqual(task.status, 'cancelled')
        self.assertTrue(task.stripe_refund_id.startswith('re_mock_'))
        
        # Verify ledger has positive refund for creator
        ledger_entry = RewardLedger.objects.get(task=task, user=self.creator_user, transaction_type='task_cancellation')
        self.assertEqual(ledger_entry.amount, 2500) # $25 refund in cents
        self.assertEqual(ledger_entry.currency, 'usd')

    def test_rewards_dashboard_separates_points_and_usd_statistics(self):
        # Set up a points transaction
        RewardLedger.objects.create(
            user=self.creator_user, amount=1500, currency='points',
            transaction_type='initial_points', description='Initial Points'
        )
        # Set up USD spent transaction
        task = Task.objects.create(
            title='USD Task for Stats', description='Desc', reward=40,
            posted_by=self.creator_user, status='available',
            reward_type='usd', stripe_payment_intent_id='pi_mock_123'
        )
        RewardLedger.objects.create(
            user=self.creator_user, task=task, amount=-4000, currency='usd',
            transaction_type='task_creation', description='USD Escrow hold'
        )
        
        # Request rewards page
        response = self.client.get(reverse('rewards'))
        self.assertEqual(response.status_code, 200)
        
        # Assert USD stats calculated correctly
        self.assertEqual(response.context['usd_earned'], 0.0)
        self.assertEqual(response.context['usd_spent'], 40.0)
        self.assertEqual(response.context['usd_escrow_hold'], 40.0)
        self.assertEqual(response.context['current_balance'], 1500) # Points balance unaffected
        
        # Filter USD only
        response_usd = self.client.get(reverse('rewards') + '?currency=usd')
        self.assertEqual(len(response_usd.context['all_transactions']), 1)
        self.assertEqual(response_usd.context['all_transactions'][0].currency, 'usd')
