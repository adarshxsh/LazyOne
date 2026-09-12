from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, Notification, UserProfile, Conversation


class DualPartyDisputeTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')

        UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})
        UserProfile.objects.get_or_create(user=self.other_user, defaults={'rewards': 1500})

        self.client_poster = Client()
        self.client_poster.login(username='poster', password='password123')

        self.client_taker = Client()
        self.client_taker.login(username='taker', password='password123')

        self.client_other = Client()
        self.client_other.login(username='other', password='password123')

        self.task = Task.objects.create(
            title='Test Task',
            description='Task description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_poster_can_raise_dispute_and_notifies_taker(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_poster.post(url, {'reason': 'Taker stopped communicating'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.poster)
        self.assertEqual(self.task.dispute.reason, 'Taker stopped communicating')
        
        # Check notification sent to taker
        notification = Notification.objects.get(recipient=self.taker)
        self.assertIn('poster has raised a dispute', notification.message)
        self.assertIn(self.task.title, notification.message)
        self.assertEqual(notification.link, reverse('dispute_detail', args=[self.task.dispute.id]))

    def test_taker_can_raise_dispute_and_notifies_poster(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_taker.post(url, {'reason': 'Poster demands extra work'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))
        self.assertEqual(self.task.dispute.raised_by, self.taker)
        
        # Check notification sent to poster
        notification = Notification.objects.get(recipient=self.poster)
        self.assertIn('taker has raised a dispute', notification.message)
        self.assertIn(self.task.title, notification.message)

    def test_non_participant_cannot_raise_dispute(self):
        url = reverse('raise_dispute', args=[self.task.id])
        response = self.client_other.post(url, {'reason': 'Unrelated user dispute'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(hasattr(self.task, 'dispute'))

    def test_cannot_raise_dispute_on_available_task(self):
        available_task = Task.objects.create(
            title='Available Task',
            description='Desc',
            reward=50,
            posted_by=self.poster,
            status='available'
        )
        url = reverse('raise_dispute', args=[available_task.id])
        self.client_poster.post(url, {'reason': 'Dispute available task'})
        
        available_task.refresh_from_db()
        self.assertEqual(available_task.status, 'available')
        self.assertFalse(hasattr(available_task, 'dispute'))

    def test_only_creator_can_withdraw_dispute(self):
        # Poster raises dispute
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        withdraw_url = reverse('withdraw_dispute', args=[dispute.id])

        # Taker tries to withdraw poster's dispute -> blocked
        response_taker = self.client_taker.post(withdraw_url)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

        # Poster withdraws poster's dispute -> success
        response_poster = self.client_poster.post(withdraw_url)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

        # Notification sent to taker upon poster withdrawal
        notification = Notification.objects.get(recipient=self.taker)
        self.assertIn('poster has withdrawn the dispute', notification.message)

    def test_poster_can_mark_disputed_task_as_complete(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Taker dispute')
        self.task.status = 'disputed'
        self.task.save()

        complete_url = reverse('complete_task', args=[self.task.id])
        self.client_poster.get(complete_url)

        self.task.refresh_from_db()
        dispute.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')
        
        taker_profile = UserProfile.objects.get(user=self.taker)
        self.assertEqual(taker_profile.rewards, 1600)  # 1500 + 100

    def test_my_tasks_ui_rendering_permissions(self):
        # In progress task: poster should see Raise Dispute button modal trigger
        response_poster = self.client_poster.get(reverse('my_tasks'))
        self.assertContains(response_poster, f"openDisputeModal('{self.task.id}')")

        # Now raise dispute by poster
        dispute = Dispute.objects.create(task=self.task, raised_by=self.poster, reason='Poster dispute')
        self.task.status = 'disputed'
        self.task.save()

        # Poster views my_tasks -> sees Withdraw Dispute for poster-raised dispute
        response_poster = self.client_poster.get(reverse('my_tasks'))
        self.assertContains(response_poster, reverse('withdraw_dispute', args=[dispute.id]))

        # Taker views my_tasks -> does NOT see Withdraw Dispute for poster-raised dispute
        response_taker = self.client_taker.get(reverse('my_tasks'))
        self.assertNotContains(response_taker, f'action="{reverse("withdraw_dispute", args=[dispute.id])}"')
