import tempfile
from datetime import timedelta
from io import BytesIO
from PIL import Image

from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone
from django.urls import reverse

from basic.models import UserProfile, Task, Dispute, DisputeEvidence, RewardLedger, Notification

def create_image_file(name='test.png', size=(100, 100), color='blue'):
    file_obj = BytesIO()
    image = Image.new('RGB', size, color=color)
    image.save(file_obj, 'PNG')
    file_obj.seek(0)
    return SimpleUploadedFile(name, file_obj.read(), content_type='image/png')

@override_settings(MEDIA_ROOT=tempfile.gettempdir())
class DisputeEvidenceTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        self.other_user = User.objects.create_user(username='other', password='password123')
        self.other_profile = UserProfile.objects.create(user=self.other_user, rewards=500)

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Task Description',
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_other = Client()
        self.client_other.login(username='other', password='password123')

    def test_raise_dispute_sets_deadline_and_initial_evidence(self):
        image_file = create_image_file('proof.png')
        response = self.client_taker.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Incomplete work delivered by poster.', 'attachment': image_file},
            follow=True
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.raised_by, self.taker)
        self.assertEqual(dispute.dispute_stage, 'counter_evidence')
        self.assertIsNotNone(dispute.response_deadline)

        expected_deadline_min = timezone.now() + timedelta(hours=71, minutes=59)
        self.assertGreater(dispute.response_deadline, expected_deadline_min)

        evidences = dispute.evidences.all()
        self.assertEqual(evidences.count(), 1)
        initial_ev = evidences.first()
        self.assertEqual(initial_ev.submitter, self.taker)
        self.assertEqual(initial_ev.evidence_type, 'initial_proof')
        self.assertEqual(initial_ev.text, 'Incomplete work delivered by poster.')
        self.assertTrue(bool(initial_ev.file))

        # Check notification sent to poster
        notif = Notification.objects.filter(recipient=self.poster).first()
        self.assertIsNotNone(notif)
        self.assertIn('raised a dispute', notif.message)

    def test_file_validation_error_unsupported_extension(self):
        bad_file = SimpleUploadedFile('script.sh', b'#!/bin/bash\necho bad', content_type='text/x-shellscript')
        response = self.client_taker.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute with bad file.', 'attachment': bad_file},
            follow=True
        )
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

    def test_unauthorized_access_rejected(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Sample dispute',
            response_deadline=timezone.now() + timedelta(hours=72),
            dispute_stage='counter_evidence'
        )

        # Unauthorized user should be redirected
        response = self.client_other.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response, reverse('home'))

    def test_submit_counter_evidence(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Initial dispute reason',
            response_deadline=timezone.now() + timedelta(hours=72),
            dispute_stage='counter_evidence'
        )

        counter_file = create_image_file('counter.png')
        response = self.client_poster.post(
            reverse('dispute_detail', args=[dispute.id]),
            {'text': 'Here is counter proof.', 'attachment': counter_file},
            follow=True
        )

        dispute.refresh_from_db()
        self.assertEqual(dispute.dispute_stage, 'under_review')

        counter_ev = dispute.evidences.filter(submitter=self.poster).first()
        self.assertIsNotNone(counter_ev)
        self.assertEqual(counter_ev.evidence_type, 'counter_evidence')
        self.assertEqual(counter_ev.text, 'Here is counter proof.')
        self.assertTrue(bool(counter_ev.file))

        # Check notification sent to taker
        notif = Notification.objects.filter(recipient=self.taker).first()
        self.assertIsNotNone(notif)
        self.assertIn('submitted counter-evidence', notif.message)

    def test_process_dispute_timeouts_command_taker_wins(self):
        # Create an expired dispute raised by taker with NO counter-evidence from poster
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Taker dispute reason',
            response_deadline=timezone.now() - timedelta(hours=1),
            dispute_stage='counter_evidence'
        )
        DisputeEvidence.objects.create(
            dispute=dispute,
            submitter=self.taker,
            text='Taker dispute reason',
            evidence_type='initial_proof'
        )

        initial_taker_rewards = self.taker_profile.rewards

        # Run management command
        call_command('process_dispute_timeouts')

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.dispute_stage, 'timed_out')
        self.assertEqual(dispute.resolution_outcome, 'auto_settled_initiator')
        self.assertIsNotNone(dispute.resolved_at)

        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + self.task.reward)

        ledger = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='dispute_settlement').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, self.task.reward)

    def test_process_dispute_timeouts_skips_when_counter_evidence_submitted(self):
        # Create an expired dispute, but poster DID submit counter-evidence
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Taker dispute reason',
            response_deadline=timezone.now() - timedelta(hours=1),
            dispute_stage='counter_evidence'
        )
        DisputeEvidence.objects.create(
            dispute=dispute,
            submitter=self.taker,
            text='Taker dispute reason',
            evidence_type='initial_proof'
        )
        DisputeEvidence.objects.create(
            dispute=dispute,
            submitter=self.poster,
            text='Poster counter evidence',
            evidence_type='counter_evidence'
        )

        call_command('process_dispute_timeouts')

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Dispute should NOT be auto-settled in favor of taker
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.dispute_stage, 'under_review')
        self.assertEqual(self.task.status, 'in_progress')

    def test_process_dispute_timeouts_command_poster_wins(self):
        # Create an expired dispute raised by poster with NO response from taker
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.poster,
            reason='Poster dispute reason',
            response_deadline=timezone.now() - timedelta(hours=1),
            dispute_stage='counter_evidence'
        )
        DisputeEvidence.objects.create(
            dispute=dispute,
            submitter=self.poster,
            text='Poster dispute reason',
            evidence_type='initial_proof'
        )

        initial_poster_rewards = self.poster_profile.rewards

        call_command('process_dispute_timeouts')

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.dispute_stage, 'timed_out')
        self.assertEqual(dispute.resolution_outcome, 'auto_settled_initiator')

        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + self.task.reward)

        ledger = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='dispute_settlement').first()
        self.assertIsNotNone(ledger)

    def test_withdraw_dispute_updates_status(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute to withdraw',
            response_deadline=timezone.now() + timedelta(hours=72),
            dispute_stage='counter_evidence'
        )

        response = self.client_taker.post(
            reverse('withdraw_dispute', args=[dispute.id]),
            follow=True
        )

        dispute.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.dispute_stage, 'resolved')
        self.assertEqual(dispute.resolution_outcome, 'withdrawn')
        self.assertEqual(self.task.status, 'in_progress')

    def test_file_validation_error_exceeds_max_size(self):
        large_content = b'a' * (10 * 1024 * 1024 + 1) # 10MB + 1 byte
        large_file = SimpleUploadedFile('large.pdf', large_content, content_type='application/pdf')
        response = self.client_taker.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Too big file', 'attachment': large_file},
            follow=True
        )
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

