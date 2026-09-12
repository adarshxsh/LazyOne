from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from basic.models import UserProfile, Task, Dispute, RewardLedger, Notification, Conversation

class DisputeArbitrationAndAppealTests(TestCase):
    def setUp(self):
        # Create users
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.staff_user = User.objects.create_user(username='staff', password='password123', is_staff=True)
        self.random_user = User.objects.create_user(username='random', password='password123')

        # Create profiles with initial rewards
        self.poster_profile, _ = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})
        self.taker_profile, _ = UserProfile.objects.get_or_create(user=self.taker, defaults={'rewards': 500})
        self.staff_profile, _ = UserProfile.objects.get_or_create(user=self.staff_user, defaults={'rewards': 1000})

        # Task creation deducts reward (100 pts) from poster
        self.reward_amount = 100
        self.poster_profile.rewards -= self.reward_amount
        self.poster_profile.save()

        self.task = Task.objects.create(
            title='Test Task',
            description='Task Description',
            reward=self.reward_amount,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

        RewardLedger.objects.create(
            user=self.poster,
            task=self.task,
            amount=-self.reward_amount,
            transaction_type='task_creation',
            description=f"Reserved for task: '{self.task.title}'"
        )

        # Create Open Dispute
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Task deliverable issue',
            status='open'
        )
        self.task.status = 'disputed'
        self.task.save()

    def test_resolve_dispute_non_staff_forbidden(self):
        self.client.login(username='poster', password='password123')
        response = self.client.post(f'/dispute/resolve/{self.dispute.id}/', {
            'resolution_type': 'poster_wins',
            'resolution_notes': 'Poster should win'
        })
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'open')
        self.assertIsNone(self.dispute.resolution_type)

    def test_resolve_dispute_poster_wins(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(f'/dispute/resolve/{self.dispute.id}/', {
            'resolution_type': 'poster_wins',
            'resolution_notes': 'Poster provided clear requirements'
        })
        self.assertRedirects(response, f'/dispute/{self.dispute.id}/')

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_type, 'poster_wins')
        self.assertEqual(self.dispute.resolved_by, self.staff_user)
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1000) # 900 + 100 refund

        ledger_entry = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 100)

    def test_resolve_dispute_taker_wins(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(f'/dispute/resolve/{self.dispute.id}/', {
            'resolution_type': 'taker_wins',
            'resolution_notes': 'Taker completed work satisfactorily'
        })

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_type, 'taker_wins')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 600) # 500 + 100 payout

        ledger_entry = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_resolution').first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.amount, 100)

    def test_resolve_dispute_split(self):
        self.client.login(username='staff', password='password123')
        response = self.client.post(f'/dispute/resolve/{self.dispute.id}/', {
            'resolution_type': 'split',
            'resolution_notes': 'Fault on both sides'
        })

        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.resolution_type, 'split')
        self.assertEqual(self.poster_profile.rewards, 950) # 900 + 50
        self.assertEqual(self.taker_profile.rewards, 550) # 500 + 50

    def test_submit_appeal_workflow(self):
        # Resolve dispute first
        self.client.login(username='staff', password='password123')
        self.client.post(f'/dispute/resolve/{self.dispute.id}/', {
            'resolution_type': 'poster_wins',
            'resolution_notes': 'Initial ruling'
        })

        # Non-participant attempt
        self.client.login(username='random', password='password123')
        resp = self.client.post(f'/dispute/appeal/{self.dispute.id}/', {'appeal_reason': 'Unfair'})
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_status, 'none')

        # Taker files valid appeal
        self.client.login(username='taker', password='password123')
        resp = self.client.post(f'/dispute/appeal/{self.dispute.id}/', {'appeal_reason': 'Evidence was ignored'})
        self.assertRedirects(resp, f'/dispute/{self.dispute.id}/')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'appealed')
        self.assertEqual(self.dispute.appeal_status, 'appealed')
        self.assertEqual(self.dispute.appealed_by, self.taker)
        self.assertEqual(self.dispute.appeal_reason, 'Evidence was ignored')

        # Second appeal attempt should be blocked
        resp2 = self.client.post(f'/dispute/appeal/{self.dispute.id}/', {'appeal_reason': 'Second try'})
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.appeal_reason, 'Evidence was ignored')

    def test_review_appeal_uphold(self):
        # Resolve dispute & appeal it
        self.client.login(username='staff', password='password123')
        self.client.post(f'/dispute/resolve/{self.dispute.id}/', {'resolution_type': 'poster_wins'})
        self.client.login(username='taker', password='password123')
        self.client.post(f'/dispute/appeal/{self.dispute.id}/', {'appeal_reason': 'Appeal text'})

        # Senior staff upholds appeal
        self.client.login(username='staff', password='password123')
        resp = self.client.post(f'/dispute/appeal/review/{self.dispute.id}/', {
            'action': 'uphold',
            'appeal_notes': 'Original ruling stood after review'
        })
        self.assertRedirects(resp, f'/dispute/{self.dispute.id}/')

        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.appeal_status, 'upheld')
        self.assertEqual(self.dispute.appeal_reviewed_by, self.staff_user)

    def test_review_appeal_overturn(self):
        # Resolve dispute with poster_wins (poster +100) & appeal by taker
        self.client.login(username='staff', password='password123')
        self.client.post(f'/dispute/resolve/{self.dispute.id}/', {'resolution_type': 'poster_wins'})
        self.client.login(username='taker', password='password123')
        self.client.post(f'/dispute/appeal/{self.dispute.id}/', {'appeal_reason': 'New evidence'})

        # Senior staff overturns to taker_wins
        self.client.login(username='staff', password='password123')
        resp = self.client.post(f'/dispute/appeal/review/{self.dispute.id}/', {
            'action': 'overturn',
            'new_resolution_type': 'taker_wins',
            'appeal_notes': 'New evidence shows taker fulfilled task'
        })

        self.dispute.refresh_from_db()
        self.poster_profile.refresh_from_db()
        self.taker_profile.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.appeal_status, 'overturned')
        self.assertEqual(self.dispute.resolution_type, 'taker_wins')
        self.assertEqual(self.task.status, 'completed')

        # Check reward balances after reversal:
        # Poster initial: 900. After poster_wins: 1000. After overturn to taker_wins: 900.
        # Taker initial: 500. After poster_wins: 500. After overturn to taker_wins: 600.
        self.assertEqual(self.poster_profile.rewards, 900)
        self.assertEqual(self.taker_profile.rewards, 600)

        poster_reversal_ledger = RewardLedger.objects.filter(user=self.poster, transaction_type='dispute_reversal').first()
        taker_reversal_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_reversal').first()
        self.assertIsNotNone(poster_reversal_ledger)
        self.assertEqual(poster_reversal_ledger.amount, -100)
        self.assertIsNotNone(taker_reversal_ledger)
        self.assertEqual(taker_reversal_ledger.amount, 100)

    def test_dispute_detail_view_access(self):
        # Poster view access
        self.client.login(username='poster', password='password123')
        resp = self.client.get(f'/dispute/{self.dispute.id}/')
        self.assertEqual(resp.status_code, 200)

        # Staff view access
        self.client.login(username='staff', password='password123')
        resp = self.client.get(f'/dispute/{self.dispute.id}/')
        self.assertEqual(resp.status_code, 200)

        # Random user view access forbidden (redirects home)
        self.client.login(username='random', password='password123')
        resp = self.client.get(f'/dispute/{self.dispute.id}/')
        self.assertRedirects(resp, '/')
