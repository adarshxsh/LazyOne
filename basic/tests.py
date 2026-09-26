from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, Friendship, FriendRequest, Notification


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


class AutoJurySelectionTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster & taker
        self.poster = User.objects.create_user(username='poster_user', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker_user', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Friends of poster and taker
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, rewards=500)
        self.poster_profile.friends.add(self.poster_friend_profile)

        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile = UserProfile.objects.create(user=self.taker_friend, rewards=500)
        Friendship.objects.create(from_user=self.taker_profile, to_user=self.taker_friend_profile)

        # Neutral community members
        self.neutrals = []
        for i in range(1, 5):
            u = User.objects.create_user(username=f'neutral_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=500)
            self.neutrals.append(u)

        # Non-impaneled outsider and staff
        self.outsider = User.objects.create_user(username='outsider_user', password='password123')
        UserProfile.objects.create(user=self.outsider, rewards=500)

        self.staff_user = User.objects.create_user(username='staff_user', password='password123', is_staff=True)
        UserProfile.objects.create(user=self.staff_user, rewards=500)

        # Task
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Disputed Jury Task",
            description="Task for jury testing",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

    def test_jury_selection_filters_participants_and_friends(self):
        self.client.login(username='taker_user', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not satisfactory'}
        )

        dispute = Dispute.objects.get(task=self.task)
        selected_jurors = list(dispute.jurors.all())

        self.assertEqual(len(selected_jurors), 3)

        # Ensure participants and direct friends are NOT impaneled
        selected_ids = [u.id for u in selected_jurors]
        self.assertNotIn(self.poster.id, selected_ids)
        self.assertNotIn(self.taker.id, selected_ids)
        self.assertNotIn(self.poster_friend.id, selected_ids)
        self.assertNotIn(self.taker_friend.id, selected_ids)

        # Ensure selected jurors are neutral non-excluded community members
        excluded_ids = {self.poster.id, self.taker.id, self.poster_friend.id, self.taker_friend.id}
        for juror in selected_jurors:
            self.assertNotIn(juror.id, excluded_ids)

    def test_juror_notifications_created(self):
        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work issue'}
        )

        dispute = Dispute.objects.get(task=self.task)
        selected_jurors = dispute.jurors.all()
        dispute_link = reverse('dispute_detail', args=[dispute.id])

        for juror in selected_jurors:
            notif = Notification.objects.filter(recipient=juror).first()
            self.assertIsNotNone(notif)
            self.assertEqual(notif.link, dispute_link)
            self.assertIn("assigned as a juror", notif.message)

    def test_dispute_detail_access_control(self):
        # Raise dispute
        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Access check reason'}
        )
        dispute = Dispute.objects.get(task=self.task)
        dispute_url = reverse('dispute_detail', args=[dispute.id])

        # Impaneled juror should be allowed access
        impaneled_juror = dispute.jurors.first()
        self.client.login(username=impaneled_juror.username, password='password123')
        response = self.client.get(dispute_url)
        self.assertEqual(response.status_code, 200)

        # Non-impaneled third party (poster_friend) should be denied access and redirected
        self.client.login(username='poster_friend', password='password123')
        response = self.client.get(dispute_url, follow=False)
        self.assertRedirects(response, reverse('home'))

        # Staff user should be allowed access
        self.client.login(username='staff_user', password='password123')
        response = self.client.get(dispute_url)
        self.assertEqual(response.status_code, 200)

    def test_accepted_friend_request_filtered(self):
        # Create an accepted friend request between taker and a neutral user
        neutral_friend_req = self.neutrals[0]
        FriendRequest.objects.create(from_user=self.taker, to_user=neutral_friend_req, is_accepted=True)

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Testing friend request filter'}
        )

        dispute = Dispute.objects.get(task=self.task)
        selected_ids = [u.id for u in dispute.jurors.all()]
        self.assertNotIn(neutral_friend_req.id, selected_ids)

    def test_small_candidate_pool(self):
        # Deactivate all neutral community members except 1
        for u in self.neutrals[1:]:
            u.is_active = False
            u.save()
        self.outsider.is_active = False
        self.outsider.save()
        self.staff_user.is_active = False
        self.staff_user.save()

        self.client.login(username='taker_user', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Small candidate pool test'}
        )

        dispute = Dispute.objects.get(task=self.task)
        selected_jurors = list(dispute.jurors.all())
        self.assertEqual(len(selected_jurors), 1)
        self.assertEqual(selected_jurors[0].id, self.neutrals[0].id)


