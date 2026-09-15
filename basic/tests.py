import tempfile
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, DisputeEvidence, Conversation

@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    MEDIA_ROOT=tempfile.mkdtemp()
)
class DisputeEvidenceTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        UserProfile.objects.get_or_create(user=self.poster)

        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.get_or_create(user=self.taker)

        self.other_user = User.objects.create_user(username='other_user', password='password123')
        UserProfile.objects.get_or_create(user=self.other_user)

        self.staff_user = User.objects.create_superuser(username='staff_user', password='password123', email='staff@example.com')
        UserProfile.objects.get_or_create(user=self.staff_user)

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_raise_dispute_with_initial_evidence(self):
        self.client.login(username='taker', password='password123')
        dummy_file = SimpleUploadedFile("proof.png", b"file_content_data", content_type="image/png")
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'reason': 'Work not as expected',
                'file': dummy_file,
                'evidence_description': 'Proof screenshot of completed work'
            },
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        
        dispute = self.task.dispute
        self.assertEqual(dispute.raised_by, self.taker)
        self.assertEqual(dispute.evidence_entries.count(), 1)

        evidence = dispute.evidence_entries.first()
        self.assertEqual(evidence.uploaded_by, self.taker)
        self.assertEqual(evidence.description, 'Proof screenshot of completed work')
        self.assertTrue(evidence.is_image)
        self.assertEqual(evidence.filename, 'proof.png')

    def test_raise_dispute_invalid_file_extension(self):
        self.client.login(username='taker', password='password123')
        invalid_file = SimpleUploadedFile("malicious.exe", b"executable_data", content_type="application/octet-stream")
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'reason': 'Work not as expected',
                'file': invalid_file
            },
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        # Dispute should not be raised due to invalid file
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_raise_dispute_oversized_file(self):
        self.client.login(username='taker', password='password123')
        large_content = b"0" * (10 * 1024 * 1024 + 1)  # > 10MB
        large_file = SimpleUploadedFile("big.zip", large_content, content_type="application/zip")
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'reason': 'Work not as expected',
                'file': large_file
            },
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_counter_evidence_upload_by_poster(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Initial dispute reason')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        counter_file = SimpleUploadedFile("specs.pdf", b"pdf_data_content", content_type="application/pdf")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {
                'file': counter_file,
                'description': 'Counter evidence document'
            },
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(dispute.evidence_entries.count(), 1)
        evidence = dispute.evidence_entries.first()
        self.assertEqual(evidence.uploaded_by, self.poster)
        self.assertEqual(evidence.description, 'Counter evidence document')
        self.assertFalse(evidence.is_image)

    def test_unauthorized_user_cannot_view_or_upload_evidence(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Initial dispute reason')
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='other_user', password='password123')
        # View attempt
        view_response = self.client.get(reverse('dispute_detail', args=[dispute.id]), fetch_redirect_response=False)
        self.assertRedirects(view_response, reverse('home'))

        # Upload attempt
        dummy_file = SimpleUploadedFile("test.txt", b"data", content_type="text/plain")
        upload_response = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'file': dummy_file},
            fetch_redirect_response=False
        )
        self.assertRedirects(upload_response, reverse('home'))
        self.assertEqual(dispute.evidence_entries.count(), 0)

    def test_dispute_detail_view_renders_evidence_and_download_link(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Initial dispute reason')
        self.task.status = 'disputed'
        self.task.save()

        dummy_file = SimpleUploadedFile("sample.jpg", b"image_data", content_type="image/jpeg")
        evidence = DisputeEvidence.objects.create(
            dispute=dispute,
            uploaded_by=self.taker,
            file=dummy_file,
            description='Sample screenshot caption'
        )

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Uploaded by: taker')
        self.assertContains(response, 'Sample screenshot caption')
        self.assertContains(response, evidence.file.url)
        self.assertContains(response, 'sample.jpg')

    def test_immutability_on_resolved_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Initial dispute reason', status='resolved')

        self.client.login(username='taker', password='password123')
        dummy_file = SimpleUploadedFile("late.png", b"late_data", content_type="image/png")
        response = self.client.post(
            reverse('upload_dispute_evidence', args=[dispute.id]),
            {'file': dummy_file},
            follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(dispute.evidence_entries.count(), 0)
        self.assertContains(response, "Cannot upload evidence to a resolved or closed dispute.")

        # Detail page should indicate evidence upload is closed
        detail_response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertContains(detail_response, "Evidence uploads are closed because this dispute is resolved.")
