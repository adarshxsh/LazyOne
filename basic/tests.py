from django.test import TestCase, TransactionTestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from asgiref.sync import sync_to_async
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


from channels.testing import WebsocketCommunicator
from LazyOne.asgi import application
from basic.broadcasting import broadcast_dispute_update

class DisputeWebSocketTests(TransactionTestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster_ws', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_ws', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.task = Task.objects.create(
            title="WebSocket Task",
            description="WebSocket Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

    async def test_websocket_global_dispute_broadcasting(self):
        communicator = WebsocketCommunicator(application, "/ws/disputes/")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Create dispute and broadcast
        dispute = await Dispute.objects.acreate(
            task=self.task,
            raised_by=self.taker,
            reason="Unmet expectations",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

        broadcast_dispute_update(dispute, 'dispute_raised', "New dispute raised")

        response = await communicator.receive_json_from()
        self.assertEqual(response.get('type'), 'dispute_update')
        self.assertEqual(response.get('event'), 'dispute_raised')
        self.assertEqual(response['dispute']['id'], dispute.id)
        self.assertEqual(response['task']['id'], self.task.id)

        await communicator.disconnect()

    async def test_websocket_dispute_specific_channel(self):
        dispute = await Dispute.objects.acreate(
            task=self.task,
            raised_by=self.taker,
            reason="Detail channel test",
            deposit_amount=50,
            escrow_status='held',
            status='open'
        )

        communicator = WebsocketCommunicator(application, f"/ws/dispute/{dispute.id}/")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        broadcast_dispute_update(dispute, 'dispute_withdrawn', "Dispute withdrawn")

        response = await communicator.receive_json_from()
        self.assertEqual(response.get('type'), 'dispute_update')
        self.assertEqual(response.get('event'), 'dispute_withdrawn')
        self.assertEqual(response['dispute']['id'], dispute.id)

        await communicator.disconnect()

    async def test_websocket_ping_pong(self):
        communicator = WebsocketCommunicator(application, "/ws/disputes/")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        await communicator.send_json_to({'type': 'ping'})
        response = await communicator.receive_json_from()
        self.assertEqual(response, {'type': 'pong'})

        await communicator.disconnect()

    async def test_raise_dispute_view_broadcasts_websocket(self):
        communicator = WebsocketCommunicator(application, "/ws/disputes/")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        client = Client()
        await sync_to_async(client.login)(username='taker_ws', password='password123')
        response = await sync_to_async(client.post)(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'View broadcast test'}
        )
        self.assertEqual(response.status_code, 302)

        ws_msg = await communicator.receive_json_from()
        self.assertEqual(ws_msg.get('type'), 'dispute_update')
        self.assertEqual(ws_msg.get('event'), 'dispute_raised')
        self.assertEqual(ws_msg['task']['id'], self.task.id)

        await communicator.disconnect()



