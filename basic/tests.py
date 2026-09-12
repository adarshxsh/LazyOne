from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, JuryPool, DisputeVote, Conversation, RewardLedger, Friendship, Notification

class JuryArbitrationTests(TestCase):
    def setUp(self):
        self.client = Client()
        # Create poster and taker
        self.poster = User.objects.create_user(username='poster', email='poster@example.com', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', email='taker@example.com', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Create poster's friend and taker's friend
        self.poster_friend = User.objects.create_user(username='poster_friend', email='pf@example.com', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend)
        self.poster_profile.friends.add(self.poster_friend_profile)

        self.taker_friend = User.objects.create_user(username='taker_friend', email='tf@example.com', password='password123')
        self.taker_friend_profile = UserProfile.objects.create(user=self.taker_friend)
        self.taker_profile.friends.add(self.taker_friend_profile)

        # Create 6 neutral community users
        self.juror_users = []
        for i in range(1, 7):
            u = User.objects.create_user(username=f'juror_{i}', email=f'j{i}@example.com', password='password123')
            UserProfile.objects.create(user=u)
            self.juror_users.append(u)

        # Create external non-juror user
        self.outsider = User.objects.create_user(username='outsider', email='out@example.com', password='password123')
        UserProfile.objects.create(user=self.outsider)

        # Create task & conversation
        self.task = Task.objects.create(
            title='Test Cleaning Task',
            description='Clean up the common room',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_dispute_creation_spawns_jury_pool_excluding_conflict_of_interest(self):
        """Test AC1: Raising a dispute automatically creates a JuryPool excluding poster, taker, and friends."""
        self.client.force_login(self.taker)
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task not completed satisfactorily'})
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertTrue(hasattr(self.task, 'dispute'))

        dispute = self.task.dispute
        self.assertTrue(hasattr(dispute, 'jury_pool'))
        jury_pool = dispute.jury_pool
        jurors = list(jury_pool.jurors.all())

        # Pool size up to 5 neutral users
        self.assertGreater(len(jurors), 0)
        self.assertLessEqual(len(jurors), 5)

        # Ensure conflict of interest exclusions
        juror_ids = [j.id for j in jurors]
        self.assertNotIn(self.poster.id, juror_ids)
        self.assertNotIn(self.taker.id, juror_ids)
        self.assertNotIn(self.poster_friend.id, juror_ids)
        self.assertNotIn(self.taker_friend.id, juror_ids)

        # Ensure notifications sent to jurors
        for juror in jurors:
            self.assertTrue(Notification.objects.filter(recipient=juror).exists())

    def test_chat_access_for_jurors_and_restriction_on_sending_messages(self):
        """Test AC2: Assigned jurors get HTTP 200 read-only access to chat, but write is forbidden; non-jurors blocked."""
        # Create dispute and jury pool
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute over work quality')
        self.task.status = 'disputed'
        self.task.save()
        jury_pool = JuryPool.create_for_dispute(dispute, pool_size=5)
        assigned_juror = jury_pool.jurors.first()

        # 1. Assigned Juror can access chat_view with HTTP 200
        self.client.force_login(assigned_juror)
        response = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_read_only'])
        self.assertTrue(response.context['is_juror'])

        # 2. Assigned Juror cannot send message (HTTP 403 Forbidden)
        response_post = self.client.post(reverse('send_message', args=[self.conversation.id]), {'content': 'Unauthorized juror message'})
        self.assertEqual(response_post.status_code, 403)

        # 3. Non-assigned external user is blocked and redirected to home
        self.client.force_login(self.outsider)
        response_outsider = self.client.get(reverse('chat_view', args=[self.conversation.id]))
        self.assertEqual(response_outsider.status_code, 302)
        self.assertIn(reverse('home'), response_outsider.url)

    def test_juror_voting_and_majority_resolution_favor_taker(self):
        """Test AC3 & AC4: Jurors cast votes, achieving majority resolves dispute in favor of Taker."""
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute over work quality')
        self.task.status = 'disputed'
        self.task.save()
        jury_pool = JuryPool.create_for_dispute(dispute, pool_size=5)
        jurors = list(jury_pool.jurors.all())
        self.assertEqual(len(jurors), 5)

        # Majority threshold for 5 is 3
        # First 2 jurors vote 'taker'
        for juror in jurors[:2]:
            self.client.force_login(juror)
            resp = self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'vote': 'taker', 'rationale': 'Work looks done'})
            self.assertEqual(resp.status_code, 302)

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.votes.count(), 2)

        # 3rd juror votes 'taker' -> triggers majority (3/5)
        self.client.force_login(jurors[2])
        resp = self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'vote': 'taker', 'rationale': 'I agree with completion'})
        self.assertEqual(resp.status_code, 302)

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.taker_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')
        self.assertEqual(self.taker_profile.rewards, 600) # 500 + 100 reward

        # Check RewardLedger transaction recorded
        ledger_entry = RewardLedger.objects.filter(user=self.taker, task=self.task, transaction_type='task_completion').first()
        self.assertIsNotNone(ledger_entry)

    def test_juror_voting_and_majority_resolution_favor_poster(self):
        """Test AC4: Achieving majority vote in favor of Poster refunds points and cancels task."""
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Dispute over work quality')
        self.task.status = 'disputed'
        self.task.save()
        jury_pool = JuryPool.create_for_dispute(dispute, pool_size=5)
        jurors = list(jury_pool.jurors.all())

        # 3 jurors vote 'poster'
        for juror in jurors[:3]:
            self.client.force_login(juror)
            resp = self.client.post(reverse('cast_dispute_vote', args=[dispute.id]), {'vote': 'poster', 'rationale': 'Task incomplete'})
            self.assertEqual(resp.status_code, 302)

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.poster_profile.refresh_from_db()

        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')
        self.assertEqual(self.poster_profile.rewards, 1100) # 1000 + 100 refund

        # Check RewardLedger transaction recorded
        ledger_entry = RewardLedger.objects.filter(user=self.poster, task=self.task, transaction_type='task_cancellation').first()
        self.assertIsNotNone(ledger_entry)

    def test_home_page_disputed_tasks_progress_display(self):
        """Test AC5: Home page disputed_tasks section accurately displays active jury progress."""
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Unfair rating expectation')
        self.task.status = 'disputed'
        self.task.save()
        jury_pool = JuryPool.create_for_dispute(dispute, pool_size=5)
        jurors = list(jury_pool.jurors.all())

        # Cast 1 vote
        DisputeVote.objects.create(dispute=dispute, juror=jurors[0], vote='poster', rationale='Testing')

        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Active Jury Review')
        self.assertContains(response, '1 / 5 votes cast')
