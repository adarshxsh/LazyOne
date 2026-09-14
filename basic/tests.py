from datetime import timedelta
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from basic.models import Task, Dispute, JuryPanel, DisputeVote, RewardLedger, UserProfile, Conversation, Message, Notification


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
