from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.management import call_command
from .models import UserProfile, Task, Dispute, RewardLedger, Conversation, JurorAssignment, Friendship, FriendRequest


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        # Eligible Jurors
        for i in range(1, 4):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=500)

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


class JurorSelectionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task Description",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=2)
        )
        Conversation.objects.create(task=self.task)

    def test_jury_panel_selection_and_stake_lock(self):
        jurors = []
        for i in range(5):
            u = User.objects.create_user(username=f'candidate_{i}', password='password123')
            p = UserProfile.objects.create(user=u, rewards=300)
            jurors.append(u)

        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not delivered'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')
        assignments = JurorAssignment.objects.filter(dispute=dispute)
        self.assertEqual(assignments.count(), 3)

        for assignment in assignments:
            self.assertEqual(assignment.staked_amount, 100)
            self.assertEqual(assignment.status, 'assigned')
            assignment.user.userprofile.refresh_from_db()
            self.assertEqual(assignment.user.userprofile.rewards, 200)

            ledger = RewardLedger.objects.filter(
                user=assignment.user,
                task=self.task,
                transaction_type='juror_stake_lock'
            ).first()
            self.assertIsNotNone(ledger)
            self.assertEqual(ledger.amount, -100)

    def test_social_graph_exclusion_friends(self):
        # Poster's friend via M2M
        poster_friend_m2m = User.objects.create_user(username='poster_friend_m2m', password='password123')
        pf_m2m_prof = UserProfile.objects.create(user=poster_friend_m2m, rewards=500)
        self.poster_profile.friends.add(pf_m2m_prof)

        # Poster's friend via FriendRequest
        poster_friend_req = User.objects.create_user(username='poster_friend_req', password='password123')
        UserProfile.objects.create(user=poster_friend_req, rewards=500)
        FriendRequest.objects.create(from_user=self.poster, to_user=poster_friend_req, is_accepted=True)

        # Taker's friend via Friendship model
        taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        tf_prof = UserProfile.objects.create(user=taker_friend, rewards=500)
        Friendship.objects.create(from_user=self.taker_profile, to_user=tf_prof)

        # 3 Neutral candidates
        neutrals = []
        for i in range(3):
            u = User.objects.create_user(username=f'neutral_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=500)
            neutrals.append(u)

        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Quality issues'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_users = list(User.objects.filter(juror_assignments__dispute=dispute))
        self.assertEqual(len(assigned_users), 3)

        for neutral in neutrals:
            self.assertIn(neutral, assigned_users)

        self.assertNotIn(poster_friend_m2m, assigned_users)
        self.assertNotIn(poster_friend_req, assigned_users)
        self.assertNotIn(taker_friend, assigned_users)
        self.assertNotIn(self.poster, assigned_users)
        self.assertNotIn(self.taker, assigned_users)

    def test_stake_threshold_exclusion(self):
        rich_users = []
        for i in range(3):
            u = User.objects.create_user(username=f'rich_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=150)
            rich_users.append(u)

        poor_users = []
        for i in range(2):
            u = User.objects.create_user(username=f'poor_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=80)
            poor_users.append(u)

        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Quality issues'}
        )

        dispute = Dispute.objects.get(task=self.task)
        assigned_users = list(User.objects.filter(juror_assignments__dispute=dispute))
        self.assertEqual(len(assigned_users), 3)
        for ru in rich_users:
            self.assertIn(ru, assigned_users)
        for pu in poor_users:
            self.assertNotIn(pu, assigned_users)

    def test_insufficient_eligible_pool_marks_under_review(self):
        for i in range(2):
            u = User.objects.create_user(username=f'candidate_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=200)

        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Need admin help'}
        )

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'under_review')
        self.assertEqual(dispute.juror_assignments.count(), 0)

    def test_stake_release_on_withdraw(self):
        jurors = []
        for i in range(3):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=200)
            jurors.append(u)

        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Withdraw test'}
        )
        dispute = Dispute.objects.get(task=self.task)

        for juror in jurors:
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 100)

        self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        for juror in jurors:
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 200)

            release_ledger = RewardLedger.objects.filter(
                user=juror,
                task=self.task,
                transaction_type='juror_stake_release'
            ).first()
            self.assertIsNotNone(release_ledger)
            self.assertEqual(release_ledger.amount, 100)

    def test_stake_release_on_task_completion(self):
        jurors = []
        for i in range(3):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=200)
            jurors.append(u)

        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Completion test'}
        )
        dispute = Dispute.objects.get(task=self.task)

        self.client.login(username='poster', password='password123')
        self.client.get(reverse('complete_task', args=[self.task.id]))

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        for juror in jurors:
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 200)

            release_ledger = RewardLedger.objects.filter(
                user=juror,
                task=self.task,
                transaction_type='juror_stake_release'
            ).first()
            self.assertIsNotNone(release_ledger)
            self.assertEqual(release_ledger.amount, 100)

    def test_stake_release_on_expired_dispute(self):
        jurors = []
        for i in range(3):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            UserProfile.objects.create(user=u, rewards=200)
            jurors.append(u)

        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Expiration test'}
        )
        dispute = Dispute.objects.get(task=self.task)
        dispute.created_at = timezone.now() - timedelta(days=10)
        dispute.save()

        call_command('resolve_expired_disputes', days=7)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')

        for juror in jurors:
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 200)

            release_ledger = RewardLedger.objects.filter(
                user=juror,
                task=self.task,
                transaction_type='juror_stake_release'
            ).first()
            self.assertIsNotNone(release_ledger)
            self.assertEqual(release_ledger.amount, 100)


