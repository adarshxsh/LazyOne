from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from .models import UserProfile, Task, Dispute, JuryPanel, DisputeVote, RewardLedger, Conversation, Message, Notification


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


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ImpaneledJuryPoolTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, rewards=1500)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, rewards=1500)

        # Create 6 disinterested community members
        self.juror_users = []
        for i in range(1, 7):
            user = User.objects.create_user(username=f'juror_{i}', password='password123')
            user.date_joined = timezone.now() - timedelta(days=10)
            user.save()
            profile, _ = UserProfile.objects.get_or_create(user=user, rewards=500)
            self.juror_users.append(user)

        self.task = Task.objects.create(
            title='Sample Task for Adjudication',
            description='This is a test task.',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_raise_dispute_impanels_5_disinterested_jurors_and_sends_notifications(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task not completed as expected'})

        dispute = Dispute.objects.get(task=self.task)
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(dispute.status, 'open')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        panel = JuryPanel.objects.get(dispute=dispute)
        jurors = list(panel.jurors.all())
        self.assertEqual(len(jurors), 5)
        self.assertNotIn(self.poster, jurors)
        self.assertNotIn(self.taker, jurors)

        for juror in jurors:
            notifications = Notification.objects.filter(recipient=juror)
            self.assertTrue(notifications.exists())
            notification = notifications.first()
            self.assertIn("impaneled as a juror", notification.message)
            self.assertEqual(notification.link, reverse('dispute_detail', args=[dispute.id]))

    def test_principals_and_unauthorized_users_voting_restrictions(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed outcome')
        self.task.status = 'disputed'
        self.task.save()

        from basic.views.dispute import impanel_jury
        panel = impanel_jury(dispute)

        # Poster trying to vote
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('cast_jury_vote', args=[panel.id]), {'vote_choice': 'poster'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(panel=panel, juror=self.poster).exists())

        # Taker trying to vote
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('cast_jury_vote', args=[panel.id]), {'vote_choice': 'taker'})
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertFalse(DisputeVote.objects.filter(panel=panel, juror=self.taker).exists())

        # Random user not in panel trying to view detail
        non_juror = User.objects.create_user(username='outsider', password='password123')
        UserProfile.objects.get_or_create(user=non_juror)
        self.client.login(username='outsider', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_majority_quorum_reasons_and_escrow_settlement_for_poster(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed outcome')
        self.task.status = 'disputed'
        self.task.save()

        from basic.views.dispute import impanel_jury
        panel = impanel_jury(dispute)
        jurors = list(panel.jurors.all())

        poster_initial_rewards = self.poster.userprofile.rewards

        # Vote 1: Poster choice
        self.client.login(username=jurors[0].username, password='password123')
        self.client.post(reverse('cast_jury_vote', args=[panel.id]), {'vote_choice': 'poster'})
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Vote 2: Taker choice
        self.client.login(username=jurors[1].username, password='password123')
        self.client.post(reverse('cast_jury_vote', args=[panel.id]), {'vote_choice': 'taker'})
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Vote 3: Poster choice (2nd poster vote)
        self.client.login(username=jurors[2].username, password='password123')
        self.client.post(reverse('cast_jury_vote', args=[panel.id]), {'vote_choice': 'poster'})
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Vote 4: Poster choice (3rd poster vote -> SIMPLE MAJORITY QUORUM ACHIEVED: 3 of 5)
        self.client.login(username=jurors[3].username, password='password123')
        self.client.post(reverse('cast_jury_vote', args=[panel.id]), {'vote_choice': 'poster'})

        dispute.refresh_from_db()
        panel.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(panel.status, 'resolved')

        # Escrow settlement verification
        self.poster.userprofile.refresh_from_db()
        self.assertEqual(self.poster.userprofile.rewards, poster_initial_rewards + self.task.reward)

        self.assertTrue(RewardLedger.objects.filter(
            user=self.poster,
            task=self.task,
            amount=self.task.reward,
            transaction_type='dispute_settlement_poster'
        ).exists())

        # Verify participating jurors (jurors 0, 1, 2, 3) received 20 bonus reward points each
        for juror in jurors[:4]:
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 520)
            self.assertTrue(RewardLedger.objects.filter(
                user=juror,
                amount=20,
                transaction_type='juror_reward'
            ).exists())

        # Non-participating juror (juror 4) should NOT have received bonus points
        jurors[4].userprofile.refresh_from_db()
        self.assertEqual(jurors[4].userprofile.rewards, 500)

    def test_majority_quorum_settlement_for_taker(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed outcome')
        self.task.status = 'disputed'
        self.task.save()

        from basic.views.dispute import impanel_jury
        panel = impanel_jury(dispute)
        jurors = list(panel.jurors.all())

        taker_initial_rewards = self.taker.userprofile.rewards

        # 3 matching votes for 'taker'
        for juror in jurors[:3]:
            self.client.login(username=juror.username, password='password123')
            self.client.post(reverse('cast_jury_vote', args=[panel.id]), {'vote_choice': 'taker'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.taker.userprofile.refresh_from_db()
        self.assertEqual(self.taker.userprofile.rewards, taker_initial_rewards + self.task.reward)

        self.assertTrue(RewardLedger.objects.filter(
            user=self.taker,
            task=self.task,
            amount=self.task.reward,
            transaction_type='dispute_settlement_taker'
        ).exists())

    def test_majority_quorum_settlement_for_split(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed outcome')
        self.task.status = 'disputed'
        self.task.save()

        from basic.views.dispute import impanel_jury
        panel = impanel_jury(dispute)
        jurors = list(panel.jurors.all())

        poster_initial_rewards = self.poster.userprofile.rewards
        taker_initial_rewards = self.taker.userprofile.rewards

        # 3 matching votes for 'split'
        for juror in jurors[:3]:
            self.client.login(username=juror.username, password='password123')
            self.client.post(reverse('cast_jury_vote', args=[panel.id]), {'vote_choice': 'split'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        self.poster.userprofile.refresh_from_db()
        self.taker.userprofile.refresh_from_db()

        self.assertEqual(self.taker.userprofile.rewards, taker_initial_rewards + 100)
        self.assertEqual(self.poster.userprofile.rewards, poster_initial_rewards + 100)

        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_split').exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_split').exists())

    def test_voting_window_expiration_triggers_fallback_resolution(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed outcome')
        self.task.status = 'disputed'
        self.task.save()

        from basic.views.dispute import impanel_jury
        panel = impanel_jury(dispute)
        panel.expires_at = timezone.now() - timedelta(hours=1)
        panel.save()

        # Accessing detail view after expiration triggers fallback settlement
        self.client.login(username=self.poster.username, password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

    def test_read_only_chat_access_for_impaneled_juror(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disputed outcome')
        self.task.status = 'disputed'
        self.task.save()

        from basic.views.dispute import impanel_jury
        panel = impanel_jury(dispute)
        juror = panel.jurors.first()

        # Juror can view chat
        self.client.login(username=juror.username, password='password123')
        response = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)

        # Juror cannot post messages
        response = self.client.post(reverse('send_message', args=[self.conversation.id]), {'content': 'Hello'})
        self.assertEqual(response.status_code, 403)
