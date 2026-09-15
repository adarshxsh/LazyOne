from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from basic.models import UserProfile, Task, Dispute, Friendship, RewardLedger, JuryAssignment, Notification
from basic.views.dispute import draw_dispute_jurors, release_juror_stakes, get_excluded_juror_user_ids

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class JurorSelectionAndStakeLockTests(TestCase):
    def setUp(self):
        # Create task poster and taker
        self.poster = User.objects.create_user(username='poster', password='password')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1500)

        self.taker = User.objects.create_user(username='taker', password='password')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1500)

        # Create task
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=500,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timezone.timedelta(days=1)
        )

    def test_litigants_and_friends_exclusion(self):
        # Create direct friend of poster
        friend_poster = User.objects.create_user(username='friend_poster', password='password')
        friend_poster_profile = UserProfile.objects.create(user=friend_poster, rewards=1500)
        self.poster_profile.friends.add(friend_poster_profile)

        # Create explicit Friendship for taker (from_user)
        friend_taker_1 = User.objects.create_user(username='friend_taker_1', password='password')
        friend_taker_1_profile = UserProfile.objects.create(user=friend_taker_1, rewards=1500)
        Friendship.objects.create(from_user=self.taker_profile, to_user=friend_taker_1_profile)

        # Create explicit Friendship for taker (to_user)
        friend_taker_2 = User.objects.create_user(username='friend_taker_2', password='password')
        friend_taker_2_profile = UserProfile.objects.create(user=friend_taker_2, rewards=1500)
        Friendship.objects.create(from_user=friend_taker_2_profile, to_user=self.taker_profile)

        # Create neutral candidate
        neutral_user = User.objects.create_user(username='neutral', password='password')
        UserProfile.objects.create(user=neutral_user, rewards=1500)

        excluded_ids = get_excluded_juror_user_ids(self.task)

        # Check that litigants and all friends are in excluded_ids
        self.assertIn(self.poster.id, excluded_ids)
        self.assertIn(self.taker.id, excluded_ids)
        self.assertIn(friend_poster.id, excluded_ids)
        self.assertIn(friend_taker_1.id, excluded_ids)
        self.assertIn(friend_taker_2.id, excluded_ids)

        # Check that neutral_user is not excluded
        self.assertNotIn(neutral_user.id, excluded_ids)

    def test_insufficient_reward_balance_exclusion(self):
        # Create candidates with low balance
        low_balance_user = User.objects.create_user(username='low_balance', password='password')
        UserProfile.objects.create(user=low_balance_user, rewards=50)

        high_balance_user = User.objects.create_user(username='high_balance', password='password')
        UserProfile.objects.create(user=high_balance_user, rewards=100)

        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        excluded_ids = get_excluded_juror_user_ids(self.task)

        candidates = User.objects.filter(
            is_active=True,
            userprofile__rewards__gte=100
        ).exclude(id__in=excluded_ids)

        candidate_ids = list(candidates.values_list('id', flat=True))
        self.assertNotIn(low_balance_user.id, candidate_ids)
        self.assertIn(high_balance_user.id, candidate_ids)

    def test_successful_juror_draw_and_stake_lock(self):
        # Create 3 eligible neutral candidates
        jurors = []
        for i in range(3):
            j = User.objects.create_user(username=f'juror_{i}', password='password')
            UserProfile.objects.create(user=j, rewards=1000)
            jurors.append(j)

        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        success, selected = draw_dispute_jurors(dispute)

        self.assertTrue(success)
        self.assertEqual(len(selected), 3)

        # Verify profile rewards deducted
        for j in jurors:
            j.userprofile.refresh_from_db()
            self.assertEqual(j.userprofile.rewards, 900)

            # Verify RewardLedger record created
            ledger = RewardLedger.objects.filter(user=j, transaction_type='juror_stake_lock').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, -100)

            # Verify JuryAssignment created
            assignment = JuryAssignment.objects.filter(dispute=dispute, juror=j).first()
            self.assertIsNotNone(assignment)
            self.assertEqual(assignment.staked_amount, 100)
            self.assertEqual(assignment.stake_status, 'locked')
            self.assertTrue(assignment.is_staked)

    def test_insufficient_jurors_fallback_and_admin_notification(self):
        admin = User.objects.create_user(username='admin', password='password', is_staff=True)
        # Create only 2 eligible neutral candidates
        for i in range(2):
            j = User.objects.create_user(username=f'juror_{i}', password='password')
            UserProfile.objects.create(user=j, rewards=1000)

        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        success, selected = draw_dispute_jurors(dispute)

        self.assertFalse(success)
        dispute.refresh_from_db()
        self.assertIn(dispute.status, ['pending_juror', 'pending-juror'])

        # Verify admin notification
        notification = Notification.objects.filter(recipient=admin).first()
        self.assertIsNotNone(notification)
        self.assertIn('pending juror draw', notification.message)

    def test_stake_release_on_dispute_resolution(self):
        # Setup 3 jurors
        jurors = []
        for i in range(3):
            j = User.objects.create_user(username=f'juror_{i}', password='password')
            UserProfile.objects.create(user=j, rewards=1000)
            jurors.append(j)

        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute reason')
        draw_dispute_jurors(dispute)

        # Now release stakes
        release_juror_stakes(dispute)

        for j in jurors:
            j.userprofile.refresh_from_db()
            self.assertEqual(j.userprofile.rewards, 1000)

            ledger = RewardLedger.objects.filter(user=j, transaction_type='juror_stake_release').first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, 100)

            assignment = JuryAssignment.objects.get(dispute=dispute, juror=j)
            self.assertEqual(assignment.stake_status, 'released')
            self.assertFalse(assignment.is_staked)

    def test_raise_dispute_and_withdraw_dispute_flow(self):
        # Create 3 neutral candidates
        for i in range(3):
            j = User.objects.create_user(username=f'juror_{i}', password='password')
            UserProfile.objects.create(user=j, rewards=1000)

        self.client.force_login(self.taker)
        response = self.client.post(f'/task/dispute/{self.task.id}/', {'reason': 'Quality issue'})
        self.assertEqual(response.status_code, 302)

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.jury_assignments.count(), 3)

        # Check assigned jurors had stake locked
        for assignment in dispute.jury_assignments.all():
            self.assertEqual(assignment.stake_status, 'locked')
            self.assertTrue(assignment.is_staked)
            self.assertEqual(assignment.juror.userprofile.rewards, 900)

        # Withdraw dispute
        response = self.client.post(f'/dispute/withdraw/{dispute.id}/')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

        # Verify stakes refunded
        for assignment in JuryAssignment.objects.filter(dispute_id=dispute.id):
            self.assertEqual(assignment.stake_status, 'released')
            self.assertFalse(assignment.is_staked)
            self.assertEqual(assignment.juror.userprofile.rewards, 1000)
