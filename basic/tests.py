import tempfile
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from .models import UserProfile, Task, Dispute, DisputeEvidence, DisputeVote, RewardLedger, Conversation


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    MEDIA_ROOT=tempfile.mkdtemp()
)
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
        self.assertEqual(dispute.status, 'evidence_submission')
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
        self.assertEqual(dispute.status, 'withdrawn')

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
        self.assertEqual(dispute.status, 'resolved_taken_by')

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


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    MEDIA_ROOT=tempfile.mkdtemp()
)
class DisputeSystemTests(TestCase):
    def setUp(self):
        # Create Poster and Taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})

        # Create 3 neutral Jurors
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        
        UserProfile.objects.get_or_create(user=self.juror1)
        UserProfile.objects.get_or_create(user=self.juror2)
        UserProfile.objects.get_or_create(user=self.juror3)

        # Create Task
        self.task = Task.objects.create(
            title="Design Logo",
            description="Create a cool vector logo",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_raise_dispute_initializes_evidence_submission_phase(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task not delivered as agreed'})
        self.task.refresh_from_db()
        
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.status, 'evidence_submission')
        self.assertEqual(self.task.dispute.raised_by, self.taker)

    def test_upload_valid_and_invalid_evidence_files(self):
        # Raise dispute
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason="Proof needed", status="evidence_submission"
        )
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')

        # Upload valid file (PNG & TXT)
        valid_png = SimpleUploadedFile("screenshot.png", b"file_content", content_type="image/png")
        valid_txt = SimpleUploadedFile("log.txt", b"log_content", content_type="text/plain")

        response = self.client.post(
            reverse('upload_evidence', args=[dispute.id]),
            {'file': [valid_png, valid_txt], 'description': 'Initial proof'},
            format='multipart'
        )
        self.assertEqual(dispute.evidence_entries.count(), 2)

        # Test invalid file format (.exe)
        invalid_exe = SimpleUploadedFile("malware.exe", b"binary_data", content_type="application/octet-stream")
        response = self.client.post(
            reverse('upload_evidence', args=[dispute.id]),
            {'file': invalid_exe, 'description': 'Bad file'},
            format='multipart'
        )
        self.assertEqual(dispute.evidence_entries.count(), 2)  # Count should remain 2

    def test_non_participant_cannot_upload_evidence(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason="Proof needed", status="evidence_submission"
        )
        self.client.login(username='juror1', password='password123')

        valid_png = SimpleUploadedFile("screenshot.png", b"file_content", content_type="image/png")
        response = self.client.post(
            reverse('upload_evidence', args=[dispute.id]),
            {'file': valid_png, 'description': 'Unauthorized upload'},
            format='multipart'
        )
        self.assertEqual(dispute.evidence_entries.count(), 0)

    def test_transition_to_jury_voting_locks_evidence(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason="Proof needed", status="evidence_submission"
        )
        self.client.login(username='poster', password='password123')

        response = self.client.post(reverse('transition_to_jury_voting', args=[dispute.id]))
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'jury_voting')

        # Attempt to upload evidence during jury_voting phase should fail
        valid_png = SimpleUploadedFile("after_lock.png", b"file_content", content_type="image/png")
        self.client.post(
            reverse('upload_evidence', args=[dispute.id]),
            {'file': valid_png, 'description': 'Late upload'},
            format='multipart'
        )
        self.assertEqual(dispute.evidence_entries.count(), 0)

    def test_juror_eligibility_and_unique_vote_constraint(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason="Proof needed", status="jury_voting"
        )
        self.task.status = 'disputed'
        self.task.save()

        # Task Poster cannot vote as juror
        self.client.login(username='poster', password='password123')
        response = self.client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'posted_by', 'rationale': 'Self vote'})
        self.assertEqual(dispute.votes.count(), 0)

        # Neutral Juror 1 votes
        self.client.login(username='juror1', password='password123')
        response = self.client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'posted_by', 'rationale': 'Poster is right'})
        self.assertEqual(dispute.votes.count(), 1)

        # Duplicate vote by Juror 1 should be blocked
        response = self.client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'taken_by', 'rationale': 'Changed my mind'})
        self.assertEqual(dispute.votes.count(), 1)

    def test_jury_consensus_poster_wins_refunds_points(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason="Proof needed", status="jury_voting"
        )
        self.task.status = 'disputed'
        self.task.save()

        poster_initial_rewards = self.poster_profile.rewards

        # Juror 1 votes for Poster
        self.client.login(username='juror1', password='password123')
        self.client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'posted_by', 'rationale': 'Juror 1 rationale'})

        # Juror 2 votes for Taker
        self.client.login(username='juror2', password='password123')
        self.client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'taken_by', 'rationale': 'Juror 2 rationale'})

        # Juror 3 votes for Poster (Reaching 3 votes threshold, Poster wins 2-1)
        self.client.login(username='juror3', password='password123')
        self.client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'posted_by', 'rationale': 'Juror 3 rationale'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved_posted_by')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, poster_initial_rewards + self.task.reward)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task.reward)

    def test_jury_consensus_taker_wins_awards_points(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.poster, reason="Poster complaint", status="jury_voting"
        )
        self.task.status = 'disputed'
        self.task.save()

        taker_initial_rewards = self.taker_profile.rewards

        # Jurors 1, 2, 3 all vote for Taker (3-0 sweep)
        for juror in [self.juror1, self.juror2, self.juror3]:
            self.client.login(username=juror.username, password='password123')
            self.client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'taken_by', 'rationale': 'Taker did good job'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved_taken_by')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, taker_initial_rewards + self.task.reward)

        # Check RewardLedger
        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='dispute_payout').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task.reward)

    def test_withdraw_dispute_restores_task_in_progress(self):
        dispute = Dispute.objects.create(
            task=self.task, raised_by=self.taker, reason="Misunderstanding", status="evidence_submission"
        )
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'withdrawn')
        self.assertEqual(self.task.status, 'in_progress')
