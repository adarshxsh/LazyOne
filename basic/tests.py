from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from .models import Task, Dispute, Conversation, UserProfile

class DisputeCategoryAndEvidenceTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        UserProfile.objects.get_or_create(user=self.poster)
        UserProfile.objects.get_or_create(user=self.taker)

        self.task = Task.objects.create(
            title='Sample Task',
            description='Sample task description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_dispute_default_fields(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Test reason'
        )
        self.assertEqual(dispute.category, 'other')
        self.assertEqual(dispute.evidence_details, '')
        self.assertEqual(dispute.get_category_display(), 'Other')

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'category': 'quality_issue',
                'reason': 'Deliverables do not meet expectations.',
                'evidence_details': 'Attached screenshots show incomplete work.'
            },
            follow=True
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        
        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.category, 'quality_issue')
        self.assertEqual(dispute.reason, 'Deliverables do not meet expectations.')
        self.assertEqual(dispute.evidence_details, 'Attached screenshots show incomplete work.')
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

    def test_raise_dispute_missing_or_invalid_category(self):
        self.client.login(username='taker', password='password123')
        
        # Test invalid category choice
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'category': 'invalid_category_code',
                'reason': 'Deliverables do not meet expectations.',
                'evidence_details': 'Some evidence'
            },
            follow=False
        )
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())
        self.assertRedirects(response, reverse('my_tasks'))

        # Test missing category
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'reason': 'Deliverables do not meet expectations.',
                'evidence_details': 'Some evidence'
            },
            follow=False
        )
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())
        self.assertRedirects(response, reverse('my_tasks'))

    def test_raise_dispute_missing_or_blank_evidence(self):
        self.client.login(username='taker', password='password123')
        
        # Test blank evidence details
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {
                'category': 'incomplete_work',
                'reason': 'Deliverables do not meet expectations.',
                'evidence_details': '   '
            },
            follow=False
        )
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())
        self.assertRedirects(response, reverse('my_tasks'))

    def test_dispute_detail_view_renders_category_and_evidence(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            category='unresponsive_partner',
            reason='The task poster stopped responding.',
            evidence_details='Sent 5 messages over 3 days without reply.'
        )
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='taker', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Unresponsive Partner')
        self.assertContains(response, 'Sent 5 messages over 3 days without reply.')
