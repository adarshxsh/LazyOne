from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from django.urls import reverse
from basic.models import (
    UserProfile, Task, Dispute, DisputeJuror, DisputeVote,
    RewardLedger, Conversation, Message, Notification
)
from basic.views.dispute import select_neutral_jurors, evaluate_dispute

class PeerJuryDisputeSystemTests(TestCase):
    def setUp(self):
        # Create poster and worker with distinct usernames
        self.poster = User.objects.create_user(username='poster_user_alice', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000, is_phone_verified=True)

        self.worker = User.objects.create_user(username='worker_user_bob', password='password123')
        self.worker_profile = UserProfile.objects.create(user=self.worker, rewards=500, is_phone_verified=True)

        # Create friends
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, is_phone_verified=True)
        self.poster_profile.friends.add(self.poster_friend_profile)

        self.worker_friend = User.objects.create_user(username='worker_friend', password='password123')
        self.worker_friend_profile = UserProfile.objects.create(user=self.worker_friend, is_phone_verified=True)
        self.worker_profile.friends.add(self.worker_friend_profile)

        # Create neutral potential jurors
        self.jurors = []
        for i in range(5):
            u = User.objects.create_user(username=f'juror_{i}', password='password123')
            p = UserProfile.objects.create(user=u, rewards=200, is_phone_verified=True)
            self.jurors.append(u)

        # Create task
        self.task = Task.objects.create(
            title='Test Task',
            description='Test Task Description',
            reward=100,
            posted_by=self.poster,
            taken_by=self.worker,
            status='in_progress',
            deadline=timezone.now() + timedelta(days=1)
        )

        # Create task conversation with extra participant
        self.chat_participant = User.objects.create_user(username='chat_participant', password='password123')
        UserProfile.objects.create(user=self.chat_participant)
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.worker, self.chat_participant)
        Message.objects.create(conversation=self.conversation, sender=self.worker, content="I finished the work.")
        Message.objects.create(conversation=self.conversation, sender=self.poster, content="No you didn't.")

        self.client = Client()

    def test_neutral_juror_filtering(self):
        """Task poster, worker, direct friends, and conversation participants must be excluded from jury duty."""
        selected_jurors = select_neutral_jurors(self.task, panel_size=3)
        selected_user_ids = {u.id for u in selected_jurors}

        # Check exclusions
        self.assertNotIn(self.poster.id, selected_user_ids)
        self.assertNotIn(self.worker.id, selected_user_ids)
        self.assertNotIn(self.poster_friend.id, selected_user_ids)
        self.assertNotIn(self.worker_friend.id, selected_user_ids)
        self.assertNotIn(self.chat_participant.id, selected_user_ids)

        # All selected jurors must be from neutral pool
        neutral_ids = {u.id for u in self.jurors}
        for j_id in selected_user_ids:
            self.assertIn(j_id, neutral_ids)

    def test_dispute_creation_triggers_peer_jury_assignment(self):
        """Raising a dispute automatically creates a 3-person peer jury and sends notifications."""
        self.client.login(username=self.worker.username, password='password123')
        response = self.client.post(reverse('raise_dispute', args=[self.task.id]), {'reason': 'Task not acknowledged'})

        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.status, 'open')
        self.assertIsNotNone(dispute.voting_deadline)

        assigned_jurors = DisputeJuror.objects.filter(dispute=dispute)
        self.assertEqual(assigned_jurors.count(), 3)

        # Check notifications sent to assigned jurors
        for assignment in assigned_jurors:
            self.assertTrue(Notification.objects.filter(recipient=assignment.juror).exists())

        # Check notification sent to poster
        self.assertTrue(Notification.objects.filter(recipient=self.poster).exists())

    def test_juror_adjudication_dashboard_and_anonymity(self):
        """Assigned jurors can access adjudication dashboard with anonymized conversation logs."""
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Incomplete work claims',
            voting_deadline=timezone.now() + timedelta(hours=48),
            status='open'
        )
        assigned_juror = self.jurors[0]
        DisputeJuror.objects.create(dispute=dispute, juror=assigned_juror)

        self.client.login(username=assigned_juror.username, password='password123')
        url = reverse('dispute_adjudicate', args=[dispute.id])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Task Poster")
        self.assertContains(response, "Task Worker")
        self.assertNotContains(response, self.poster.username)
        self.assertNotContains(response, self.worker.username)

    def test_unauthorized_access_to_adjudication_dashboard(self):
        """Non-assigned users cannot access the juror adjudication panel."""
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Incomplete work claims',
            voting_deadline=timezone.now() + timedelta(hours=48),
            status='open'
        )
        non_juror = self.jurors[4]

        self.client.login(username=non_juror.username, password='password123')
        response = self.client.get(reverse('dispute_adjudicate', args=[dispute.id]))
        self.assertEqual(response.status_code, 302)

    def test_single_choice_voting_and_majority_resolution_worker_wins(self):
        """Majority votes (2 out of 3) for worker_wins completes task, credits worker, and rewards participating jurors."""
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Payment refused',
            voting_deadline=timezone.now() + timedelta(hours=48),
            status='open'
        )
        j1, j2, j3 = self.jurors[0], self.jurors[1], self.jurors[2]
        DisputeJuror.objects.create(dispute=dispute, juror=j1)
        DisputeJuror.objects.create(dispute=dispute, juror=j2)
        DisputeJuror.objects.create(dispute=dispute, juror=j3)

        # First juror votes worker_wins
        self.client.login(username=j1.username, password='password123')
        self.client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'worker_wins'})
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Second juror votes worker_wins -> Simple majority (2/3) reached!
        self.client.login(username=j2.username, password='password123')
        self.client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'worker_wins'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.resolution, 'worker_wins')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        # Check worker received reward points (500 + 100 = 600)
        self.worker_profile.refresh_from_db()
        self.assertEqual(self.worker_profile.rewards, 600)

        # Check participating jurors (j1, j2) received juror reward points
        j1.userprofile.refresh_from_db()
        j2.userprofile.refresh_from_db()
        self.assertGreater(j1.userprofile.rewards, 200)
        self.assertGreater(j2.userprofile.rewards, 200)

        # Check reward ledger records
        self.assertTrue(RewardLedger.objects.filter(user=self.worker, transaction_type='task_completion').exists())
        self.assertTrue(RewardLedger.objects.filter(user=j1, transaction_type='juror_reward').exists())

    def test_simple_majority_resolution_poster_wins(self):
        """Majority votes for poster_wins cancels task, refunds poster points, and rewards participating jurors."""
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Work incomplete dispute',
            voting_deadline=timezone.now() + timedelta(hours=48),
            status='open'
        )
        j1, j2, j3 = self.jurors[0], self.jurors[1], self.jurors[2]
        DisputeJuror.objects.create(dispute=dispute, juror=j1)
        DisputeJuror.objects.create(dispute=dispute, juror=j2)
        DisputeJuror.objects.create(dispute=dispute, juror=j3)

        self.client.login(username=j1.username, password='password123')
        self.client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'poster_wins'})

        self.client.login(username=j2.username, password='password123')
        self.client.post(reverse('submit_jury_vote', args=[dispute.id]), {'vote': 'poster_wins'})

        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(dispute.resolution, 'poster_wins')

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1100) # 1000 + 100 refund

        self.assertTrue(RewardLedger.objects.filter(user=self.poster, transaction_type='task_cancellation').exists())

    def test_expired_voting_deadline_escalates_if_no_majority(self):
        """When voting window expires without majority consensus, dispute escalates to unresolved."""
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason='Expired case',
            voting_deadline=timezone.now() - timedelta(hours=1),
            status='open'
        )
        j1, j2, j3 = self.jurors[0], self.jurors[1], self.jurors[2]
        DisputeJuror.objects.create(dispute=dispute, juror=j1)
        DisputeJuror.objects.create(dispute=dispute, juror=j2)

        evaluate_dispute(dispute)
        dispute.refresh_from_db()

        self.assertEqual(dispute.status, 'unresolved')
        self.assertEqual(dispute.resolution, 'unresolved')
