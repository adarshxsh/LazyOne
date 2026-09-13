from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from .models import UserProfile, Task, Dispute, Conversation, RewardLedger
from .views.dispute import resolve_dispute

class EscrowLockAndJurorRewardsTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1500})

        # Create taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 1500})

        # Poster creates a task with 100 points reward
        self.task = Task.objects.create(
            title='Test Escrow Lock Task',
            description='Description of test task',
            reward=100,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='in_progress',
            taken_by=self.taker
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_complete_task_rejects_disputed_tasks(self):
        # Raise dispute on task
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Task doer has a dispute'
        )
        self.task.status = 'disputed'
        self.task.save()

        # Login poster
        self.client.login(username='poster', password='password123')

        initial_taker_rewards = self.taker.userprofile.rewards

        # Poster attempts to complete disputed task
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        # Reload task and taker profile
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        # Verify task remains disputed and taker did NOT get rewards
        self.assertEqual(self.task.status, 'disputed')
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards)

        # Check no task_completion RewardLedger entry was created
        completion_ledger = RewardLedger.objects.filter(
            task=self.task,
            transaction_type='task_completion'
        ).exists()
        self.assertFalse(completion_ledger)

    def test_my_tasks_ui_hides_mark_as_complete_for_disputed_tasks(self):
        # Create dispute
        Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason'
        )
        self.task.status = 'disputed'
        self.task.save()

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('my_tasks'))
        self.assertEqual(response.status_code, 200)

        # Confirm Mark as Complete is NOT present in response
        self.assertNotContains(response, 'Mark as Complete')
        self.assertNotContains(response, 'You can end the dispute by completing the task.')
        self.assertContains(response, 'This task is currently in dispute.')

    def test_expanded_ledger_transaction_types(self):
        juror = User.objects.create_user(username='juror1', password='password123')

        entry1 = RewardLedger.objects.create(
            user=juror, task=self.task, amount=10,
            transaction_type='juror_reward', description='Juror fee'
        )
        entry2 = RewardLedger.objects.create(
            user=self.taker, task=self.task, amount=90,
            transaction_type='dispute_settlement', description='Dispute payout'
        )
        entry3 = RewardLedger.objects.create(
            user=self.poster, task=self.task, amount=90,
            transaction_type='dispute_refund', description='Dispute refund'
        )
        entry4 = RewardLedger.objects.create(
            user=self.poster, task=self.task, amount=-20,
            transaction_type='dispute_penalty', description='Dispute penalty'
        )

        self.assertEqual(entry1.transaction_type, 'juror_reward')
        self.assertEqual(entry2.transaction_type, 'dispute_settlement')
        self.assertEqual(entry3.transaction_type, 'dispute_refund')
        self.assertEqual(entry4.transaction_type, 'dispute_penalty')

    def test_dispute_resolution_with_juror_reward_distribution(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Dispute reason'
        )
        self.task.status = 'disputed'
        self.task.save()

        juror1 = User.objects.create_user(username='juror_a', password='password123')
        UserProfile.objects.get_or_create(user=juror1, defaults={'rewards': 1000})
        juror2 = User.objects.create_user(username='juror_b', password='password123')
        UserProfile.objects.get_or_create(user=juror2, defaults={'rewards': 1000})

        initial_taker_rewards = self.taker.userprofile.rewards

        # Resolve dispute in favor of taker with 2 voting jurors
        resolve_dispute(dispute, winner='taker', voting_jurors=[juror1, juror2])

        self.task.refresh_from_db()
        dispute.refresh_from_db()

        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(dispute.status, 'resolved')

        # 10% of 100 reward = 10 total juror pool -> 5 points each to 2 jurors
        juror1_profile = UserProfile.objects.get(user=juror1)
        juror2_profile = UserProfile.objects.get(user=juror2)
        self.assertEqual(juror1_profile.rewards, 1005)
        self.assertEqual(juror2_profile.rewards, 1005)

        # 90 points to taker
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + 90)

        # Check RewardLedger entries
        juror1_ledger = RewardLedger.objects.get(user=juror1, task=self.task)
        self.assertEqual(juror1_ledger.amount, 5)
        self.assertEqual(juror1_ledger.transaction_type, 'juror_reward')

        taker_ledger = RewardLedger.objects.get(user=self.taker, task=self.task, transaction_type='dispute_settlement')
        self.assertEqual(taker_ledger.amount, 90)

    def test_dispute_resolution_poster_wins_refund(self):
        task2 = Task.objects.create(
            title='Poster Refund Task',
            description='Desc',
            reward=200,
            posted_by=self.poster,
            deadline=timezone.now() + timedelta(days=1),
            status='disputed',
            taken_by=self.taker
        )
        Conversation.objects.create(task=task2)
        dispute2 = Dispute.objects.create(
            task=task2,
            raised_by=self.taker,
            reason='Dispute reason'
        )

        juror1 = User.objects.create_user(username='juror_c', password='password123')
        UserProfile.objects.get_or_create(user=juror1, defaults={'rewards': 1000})

        initial_poster_rewards = self.poster.userprofile.rewards

        resolve_dispute(dispute2, winner='poster', voting_jurors=[juror1])

        task2.refresh_from_db()
        dispute2.refresh_from_db()

        self.assertEqual(task2.status, 'cancelled')
        self.assertEqual(dispute2.status, 'resolved')

        # 10% of 200 = 20 points to juror
        juror1_profile = UserProfile.objects.get(user=juror1)
        self.assertEqual(juror1_profile.rewards, 1020)

        # 180 points refund to poster
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + 180)

        poster_ledger = RewardLedger.objects.get(user=self.poster, task=task2, transaction_type='dispute_refund')
        self.assertEqual(poster_ledger.amount, 180)
