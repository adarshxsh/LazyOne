from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from .models import UserProfile, Task, Dispute, DisputeAppeal, JuryVote, RewardLedger, Notification, Friendship, Conversation


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


class JuryAppealSystemTestCase(TestCase):
    def setUp(self):
        # Create Poster and Taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        self.poster_profile.rewards = 1500
        self.poster_profile.save()

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})
        self.taker_profile.rewards = 500
        self.taker_profile.save()

        # Create Friends of Poster and Taker
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile, _ = UserProfile.objects.get_or_create(user=self.poster_friend, defaults={'rewards': 1000})
        self.poster_profile.friends.add(self.poster_friend_profile)

        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile, _ = UserProfile.objects.get_or_create(user=self.taker_friend, defaults={'rewards': 1000})
        self.taker_profile.friends.add(self.taker_friend_profile)

        # Create neutral potential jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile, _ = UserProfile.objects.get_or_create(user=self.juror1, defaults={'rewards': 1000})

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile, _ = UserProfile.objects.get_or_create(user=self.juror2, defaults={'rewards': 1000})

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile, _ = UserProfile.objects.get_or_create(user=self.juror3, defaults={'rewards': 1000})

        # Create Task & Dispute
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Taker did the task, poster claims incomplete.',
            status='open'
        )

        self.client = Client()

    def test_file_appeal_success_and_neutrality(self):
        """Test filing an appeal deducts bond, records ledger entry, and selects neutral jurors."""
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('file_appeal', args=[self.dispute.id]))

        self.assertEqual(response.status_code, 302)
        self.dispute.refresh_from_reload = True
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appealed')

        # Check bond deduction & ledger entry
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400) # 500 - 100

        bond_ledger = RewardLedger.objects.get(user=self.taker, transaction_type='appeal_bond')
        self.assertEqual(bond_ledger.amount, -100)

        # Check appeal creation & assigned jurors
        appeal = DisputeAppeal.objects.get(dispute=self.dispute)
        self.assertEqual(appeal.appellant, self.taker)
        self.assertEqual(appeal.bond_amount, 100)
        self.assertEqual(appeal.status, 'voting')

        assigned_jurors = set(appeal.jurors.all())
        self.assertNotIn(self.poster, assigned_jurors)
        self.assertNotIn(self.taker, assigned_jurors)
        self.assertNotIn(self.poster_friend, assigned_jurors)
        self.assertNotIn(self.taker_friend, assigned_jurors)
        self.assertTrue(assigned_jurors.issubset({self.juror1, self.juror2, self.juror3}))

        # Notifications checked
        poster_notif = Notification.objects.filter(recipient=self.poster, link=reverse('dispute_detail', args=[self.dispute.id])).first()
        self.assertIsNotNone(poster_notif)
        self.assertIn("has filed an appeal", poster_notif.message)

    def test_file_appeal_insufficient_rewards(self):
        """Test filing an appeal fails if user has insufficient rewards."""
        self.taker_profile.rewards = 50
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('file_appeal', args=[self.dispute.id]))

        self.assertEqual(response.status_code, 302)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')
        self.assertFalse(hasattr(self.dispute, 'appeal'))

    def test_jury_voting_and_quorum_resolution_appellant_wins(self):
        """Test jury voting reaches majority quorum where appellant wins, distributing rewards and slashing penalty."""
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('file_appeal', args=[self.dispute.id]))

        appeal = DisputeAppeal.objects.get(dispute=self.dispute)
        assigned_jurors = list(appeal.jurors.all())
        self.assertEqual(len(assigned_jurors), 3)

        # Juror 1 votes for Taker (appellant)
        self.client.login(username=assigned_jurors[0].username, password='password123')
        self.client.post(reverse('cast_jury_vote', args=[appeal.id]), {'voted_for_id': self.taker.id})

        appeal.refresh_from_db()
        self.assertEqual(appeal.status, 'voting') # 1/3, quorum (2) not reached yet

        # Juror 2 votes for Taker (appellant) -> Majority (2/3) reached!
        self.client.login(username=assigned_jurors[1].username, password='password123')
        self.client.post(reverse('cast_jury_vote', args=[appeal.id]), {'voted_for_id': self.taker.id})

        appeal.refresh_from_db()
        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(appeal.status, 'resolved')
        self.assertEqual(appeal.winner, self.taker)
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Check Taker (Appellant Winner) reward payout & bond refund
        self.taker_profile.refresh_from_db()
        # Initial 500 - 100 bond + 100 refund + 200 task reward = 700
        self.assertEqual(self.taker_profile.rewards, 700)

        # Check Poster (Respondent Loser) slashing
        self.poster_profile.refresh_from_db()
        # Initial 1500 - 100 slash = 1400
        self.assertEqual(self.poster_profile.rewards, 1400)

        # Check Ledger entries for appeal_bond, task_completion, slashing_penalty, juror_reward
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='appeal_bond', amount=100).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='task_completion', amount=200).exists())
        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='slashing_penalty', amount=-100).exists())

        # Check Majority Juror Rewards (100 slash pool divided between 2 majority jurors = 50 each)
        j1_profile = assigned_jurors[0].userprofile
        j1_profile.refresh_from_db()
        self.assertEqual(j1_profile.rewards, 1050)
        self.assertTrue(RewardLedger.objects.filter(user=assigned_jurors[0], transaction_type='juror_reward', amount=50).exists())

    def test_jury_voting_appellant_loses(self):
        """Test appeal where appellant loses, resulting in bond slashing and refund to poster."""
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('file_appeal', args=[self.dispute.id]))

        appeal = DisputeAppeal.objects.get(dispute=self.dispute)
        assigned_jurors = list(appeal.jurors.all())

        # Jurors 1 and 2 vote for Poster
        self.client.login(username=assigned_jurors[0].username, password='password123')
        self.client.post(reverse('cast_jury_vote', args=[appeal.id]), {'voted_for_id': self.poster.id})

        self.client.login(username=assigned_jurors[1].username, password='password123')
        self.client.post(reverse('cast_jury_vote', args=[appeal.id]), {'voted_for_id': self.poster.id})

        appeal.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(appeal.status, 'resolved')
        self.assertEqual(appeal.winner, self.poster)
        self.assertEqual(self.task.status, 'cancelled')

        # Check Taker (Appellant Loser) rewards balance: initial 500 - 100 bond (not refunded) = 400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

        # Check slashing penalty ledger entry recorded for losing appellant
        self.assertTrue(RewardLedger.objects.filter(user=self.taker, transaction_type='slashing_penalty', amount=-100).exists())

        # Check Poster (Winner) gets task reward refunded
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1700) # 1500 + 200

    def test_appeal_timeout_expiration(self):
        """Test that an appeal exceeding 72 hours without quorum refunds bond and escalates to admin."""
        self.client.login(username='taker', password='password123')
        self.client.post(reverse('file_appeal', args=[self.dispute.id]))

        appeal = DisputeAppeal.objects.get(dispute=self.dispute)
        # Fast forward deadline to past
        appeal.voting_deadline = timezone.now() - timedelta(hours=1)
        appeal.save()

        # Access dispute detail page to trigger check_appeal_timeout
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))

        appeal.refresh_from_db()
        self.assertEqual(appeal.status, 'escalated')

        # Taker profile should have appeal bond refunded
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 500)
