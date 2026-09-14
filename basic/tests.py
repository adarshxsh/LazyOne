from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, Conversation, UserProfile, Notification
from basic.forms import DisputeForm

class DisputeModelAndFormTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )

    def test_dispute_model_creation_and_choices(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            category='incomplete_requirements',
            reason='The task requirements were incomplete.',
            evidence_url='https://example.com/evidence.jpg',
            evidence_details='Here is the detailed evidence explaining why requirements were incomplete.'
        )
        self.assertEqual(dispute.category, 'incomplete_requirements')
        self.assertEqual(dispute.get_category_display(), 'Incomplete Requirements')
        self.assertEqual(dispute.evidence_url, 'https://example.com/evidence.jpg')
        self.assertEqual(dispute.evidence_details, 'Here is the detailed evidence explaining why requirements were incomplete.')

    def test_legacy_dispute_compatibility(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Legacy reason without category or evidence'
        )
        self.assertIsNone(dispute.category)
        self.assertIsNone(dispute.evidence_url)
        self.assertIsNone(dispute.evidence_details)

    def test_dispute_form_valid_data(self):
        form = DisputeForm(data={
            'category': 'deliverable_issue',
            'reason': 'Deliverable was defective',
            'evidence_url': 'https://example.com/proof',
            'evidence_details': 'Detailed description of the deliverable issue exceeding twenty characters.'
        })
        self.assertTrue(form.is_valid())

    def test_dispute_form_missing_category(self):
        form = DisputeForm(data={
            'category': '',
            'reason': 'Deliverable was defective',
            'evidence_details': 'Detailed description of the deliverable issue exceeding twenty characters.'
        })
        self.assertFalse(form.is_valid())
        self.assertIn('category', form.errors)

    def test_dispute_form_invalid_category(self):
        form = DisputeForm(data={
            'category': 'invalid_cat',
            'reason': 'Deliverable was defective',
            'evidence_details': 'Detailed description of the deliverable issue exceeding twenty characters.'
        })
        self.assertFalse(form.is_valid())
        self.assertIn('category', form.errors)

    def test_dispute_form_missing_evidence_details(self):
        form = DisputeForm(data={
            'category': 'deliverable_issue',
            'reason': 'Deliverable was defective',
            'evidence_details': ''
        })
        self.assertFalse(form.is_valid())
        self.assertIn('evidence_details', form.errors)

    def test_dispute_form_short_evidence_details(self):
        form = DisputeForm(data={
            'category': 'deliverable_issue',
            'reason': 'Deliverable was defective',
            'evidence_details': 'Too short'
        })
        self.assertFalse(form.is_valid())
        self.assertIn('evidence_details', form.errors)


class RaiseDisputeViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)
        self.task = Task.objects.create(
            title='View Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {
            'category': 'unresponsive_counterparty',
            'reason': 'Poster is unresponsive',
            'evidence_url': 'https://example.com/chat-log',
            'evidence_details': 'I sent multiple messages over three days and received no response from the poster.'
        }, follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        dispute = self.task.dispute
        self.assertEqual(dispute.category, 'unresponsive_counterparty')
        self.assertEqual(dispute.evidence_details, 'I sent multiple messages over three days and received no response from the poster.')
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))
        self.assertContains(response, 'Unresponsive Counterparty')

    def test_raise_dispute_missing_category_fails(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {
            'category': '',
            'reason': 'Some reason for dispute',
            'evidence_details': 'This is a long enough evidence detail text that passes minimum length.'
        }, follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))
        self.assertRedirects(response, reverse('my_tasks'))

    def test_raise_dispute_short_evidence_details_fails(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {
            'category': 'payment_points_dispute',
            'reason': 'Points not credited',
            'evidence_details': 'Short details'
        }, follow=True)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))
        self.assertRedirects(response, reverse('my_tasks'))

    def test_dispute_detail_view_renders_category_and_evidence(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            category='deliverable_issue',
            reason='Flawed deliverable',
            evidence_url='https://example.com/evidence-link',
            evidence_details='The delivered item did not meet the specified standard.'
        )
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Deliverable Issue')
        self.assertContains(response, 'https://example.com/evidence-link')
        self.assertContains(response, 'The delivered item did not meet the specified standard.')
