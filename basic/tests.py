from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import Task, Dispute, UserProfile, Conversation

class DisputeStatusGuardsTestCase(TestCase):
    def setUp(self):
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker1 = User.objects.create_user(username='taker1', password='password123')
        self.taker2 = User.objects.create_user(username='taker2', password='password123')

        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})
        self.taker1_profile, _ = UserProfile.objects.get_or_create(user=self.taker1, defaults={'rewards': 1500})
        self.taker2_profile, _ = UserProfile.objects.get_or_create(user=self.taker2, defaults={'rewards': 1500})

        self.task = Task.objects.create(
            title='Test Task',
            description='Test Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker1,
            status='in_progress',
            cancellation_requested=True
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker1)

        self.client_taker1 = Client()
        self.client_taker1.login(username='taker1', password='password123')

        self.client_taker2 = Client()
        self.client_taker2.login(username='taker2', password='password123')

    # --- accept_cancellation tests ---

    def test_accept_cancellation_success(self):
        """accept_cancellation succeeds for in_progress task with cancellation requested, deletes dispute if any."""
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason='Old dispute', status='open')
        initial_poster_rewards = self.poster_profile.rewards

        response = self.client_taker1.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'available')
        self.assertIsNone(self.task.taken_by)
        self.assertFalse(self.task.cancellation_requested)

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 100)
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_accept_cancellation_rejected_if_disputed_status(self):
        """accept_cancellation rejects request if task status is 'disputed'."""
        self.task.status = 'disputed'
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason='Open dispute', status='open')

        response = self.client_taker1.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(self.task.taken_by, self.taker1)

    def test_accept_cancellation_rejected_if_completed_status(self):
        """accept_cancellation rejects request if task status is 'completed'."""
        self.task.status = 'completed'
        self.task.save()

        response = self.client_taker1.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

    def test_accept_cancellation_rejected_if_cancelled_status(self):
        """accept_cancellation rejects request if task status is 'cancelled'."""
        self.task.status = 'cancelled'
        self.task.save()

        response = self.client_taker1.get(reverse('accept_cancellation', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

    # --- withdraw_dispute tests ---

    def test_withdraw_dispute_success(self):
        """withdraw_dispute reverts task status from 'disputed' to 'in_progress' and deletes open dispute."""
        self.task.status = 'disputed'
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason='Valid dispute', status='open')

        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

    def test_withdraw_dispute_rejected_if_not_open(self):
        """withdraw_dispute rejects request if dispute status is 'resolved'."""
        self.task.status = 'disputed'
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason='Resolved dispute', status='resolved')

        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    def test_withdraw_dispute_rejected_if_task_completed(self):
        """withdraw_dispute rejects request if task status is not 'disputed' (e.g. 'completed')."""
        self.task.status = 'completed'
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason='Dispute on completed task', status='open')

        response = self.client_taker1.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.assertTrue(Dispute.objects.filter(id=dispute.id).exists())

    # --- raise_dispute tests ---

    def test_raise_dispute_redirects_if_open_dispute_exists(self):
        """raise_dispute redirects to dispute detail if task already has an open dispute."""
        self.task.status = 'disputed'
        self.task.save()
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason='Open dispute', status='open')

        response = self.client_taker1.get(reverse('raise_dispute', args=[self.task.id]))
        self.assertRedirects(response, reverse('dispute_detail', args=[dispute.id]))

    def test_raise_dispute_cleans_up_resolved_dispute_for_new_taker(self):
        """raise_dispute removes stale/resolved dispute and creates new dispute for new task taker."""
        old_dispute = Dispute.objects.create(task=self.task, raised_by=self.taker1, reason='Old resolved dispute', status='resolved')

        self.task.taken_by = self.taker2
        self.task.status = 'in_progress'
        self.task.save()

        response = self.client_taker2.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'New dispute reason'})

        self.assertFalse(Dispute.objects.filter(id=old_dispute.id).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        new_dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(new_dispute.raised_by, self.taker2)
        self.assertEqual(new_dispute.reason, 'New dispute reason')
        self.assertEqual(new_dispute.status, 'open')
        self.assertRedirects(response, reverse('dispute_detail', args=[new_dispute.id]))
