from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from basic.models import UserProfile, Task, Dispute, JuryAssignment, DisputeVote, RewardLedger, Notification, Conversation

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class StakedJuryArbitrationTests(TestCase):
    def setUp(self):
        # Create poster and taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)
        
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=1000)

        # Create 3 potential neutral community jurors with sufficient points
        self.juror1 = User.objects.create_user(username='juror1', password='password123')
        self.juror1_profile = UserProfile.objects.create(user=self.juror1, rewards=500)

        self.juror2 = User.objects.create_user(username='juror2', password='password123')
        self.juror2_profile = UserProfile.objects.create(user=self.juror2, rewards=500)

        self.juror3 = User.objects.create_user(username='juror3', password='password123')
        self.juror3_profile = UserProfile.objects.create(user=self.juror3, rewards=500)

        # Create a user with insufficient points
        self.poor_user = User.objects.create_user(username='poor_user', password='password123')
        self.poor_profile = UserProfile.objects.create(user=self.poor_user, rewards=10)

        # Create an in_progress task
        self.task = Task.objects.create(
            title="Clean Room",
            description="Clean my room thoroughly",
            reward=200,
            posted_by=self.poster,
            taken_by=self.taker,
            deadline=timezone.now() + timedelta(days=1),
            status='in_progress'
        )
        self.conversation = Conversation.objects.create(task=self.task)
        self.conversation.participants.add(self.poster, self.taker)

    def test_raise_dispute_creates_odd_numbered_neutral_jury(self):
        client = Client()
        client.login(username='taker', password='password123')

        url = reverse('raise_dispute', args=[self.task.id])
        response = client.post(url, {'reason': 'Poster refused to accept completed work.'})
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        
        dispute = getattr(self.task, 'dispute', None)
        self.assertIsNotNone(dispute)
        self.assertEqual(dispute.status, 'open')

        # Check jury pool assignment
        assignments = list(dispute.assignments.all())
        juror_ids = [a.juror.id for a in assignments]
        
        # Panel should be odd-numbered (3 neutral jurors)
        self.assertEqual(len(assignments), 3)
        self.assertNotIn(self.poster.id, juror_ids)
        self.assertNotIn(self.taker.id, juror_ids)
        self.assertNotIn(self.poor_user.id, juror_ids)

        # Check notification sent to jurors
        for juror in [self.juror1, self.juror2, self.juror3]:
            self.assertTrue(Notification.objects.filter(recipient=juror).exists())

    def test_juror_authorization_and_private_dashboard(self):
        # Raise dispute first
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disagreed on quality')
        self.task.status = 'disputed'
        self.task.save()
        
        JuryAssignment.objects.create(dispute=dispute, juror=self.juror1, stake_amount=50)

        # Assigned juror can access dispute detail
        client = Client()
        client.login(username='juror1', password='password123')
        response = client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Dispute Arbitration Dashboard')

        # Unauthorized random user cannot access dispute detail
        client.login(username='poor_user', password='password123')
        response_unauth = client.get(reverse('dispute_detail', args=[dispute.id]))
        self.assertRedirects(response_unauth, reverse('home'), fetch_redirect_response=False)

    def test_voting_stakes_points_and_supermajority_settles_task(self):
        # Raise dispute and assign 3 jurors
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disagreed on quality')
        self.task.status = 'disputed'
        self.task.save()

        JuryAssignment.objects.create(dispute=dispute, juror=self.juror1, stake_amount=50)
        JuryAssignment.objects.create(dispute=dispute, juror=self.juror2, stake_amount=50)
        JuryAssignment.objects.create(dispute=dispute, juror=self.juror3, stake_amount=50)

        # Juror 1 votes for taker
        client1 = Client()
        client1.login(username='juror1', password='password123')
        resp1 = client1.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})
        
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 450) # 500 - 50 staked
        self.assertTrue(RewardLedger.objects.filter(user=self.juror1, transaction_type='juror_stake').exists())
        
        # Dispute should still be open after 1 vote (supermajority requires 2)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, 'open')

        # Juror 2 votes for taker (reaching 2/3 supermajority consensus)
        client2 = Client()
        client2.login(username='juror2', password='password123')
        resp2 = client2.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'completed')

        # Check reward transfer to taker (200 pts)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 1200) # 1000 + 200

        # Check payout to majority jurors (stake returned + bonus)
        self.juror1_profile.refresh_from_db()
        self.juror2_profile.refresh_from_db()
        self.assertGreaterEqual(self.juror1_profile.rewards, 510) # 450 + 50 returned + bonus
        self.assertGreaterEqual(self.juror2_profile.rewards, 510)

    def test_poster_supermajority_consensus(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Disagreed on quality')
        self.task.status = 'disputed'
        self.task.save()

        JuryAssignment.objects.create(dispute=dispute, juror=self.juror1, stake_amount=50)
        JuryAssignment.objects.create(dispute=dispute, juror=self.juror2, stake_amount=50)
        JuryAssignment.objects.create(dispute=dispute, juror=self.juror3, stake_amount=50)

        client1 = Client()
        client1.login(username='juror1', password='password123')
        client1.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'poster'})

        client2 = Client()
        client2.login(username='juror2', password='password123')
        client2.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'poster'})

        dispute.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(dispute.status, 'resolved')
        self.assertEqual(self.task.status, 'cancelled')

        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1200) # 1000 + 200 refunded

    def test_poster_blocked_from_unilateral_completion_during_dispute(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Active dispute')
        self.task.status = 'disputed'
        self.task.save()

        client = Client()
        client.login(username='poster', password='password123')
        response = client.get(reverse('complete_task', args=[self.task.id]))
        
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')
        self.assertRedirects(response, reverse('my_tasks'), fetch_redirect_response=False)

    def test_withdraw_dispute_refunds_staked_jurors(self):
        dispute = Dispute.objects.create(task=self.task, raised_by=self.taker, reason='Misunderstanding')
        self.task.status = 'disputed'
        self.task.save()

        JuryAssignment.objects.create(dispute=dispute, juror=self.juror1, stake_amount=50)
        JuryAssignment.objects.create(dispute=dispute, juror=self.juror2, stake_amount=50)
        JuryAssignment.objects.create(dispute=dispute, juror=self.juror3, stake_amount=50)
        
        # Juror 1 stakes points by voting (1 out of 3 votes cast, dispute remains open)
        client1 = Client()
        client1.login(username='juror1', password='password123')
        client1.post(reverse('submit_dispute_vote', args=[dispute.id]), {'choice': 'taker'})
        
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 450)

        # Taker withdraws dispute
        client_taker = Client()
        client_taker.login(username='taker', password='password123')
        response = client_taker.post(reverse('withdraw_dispute', args=[dispute.id]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(id=dispute.id).exists())

        # Check juror stake refunded
        self.juror1_profile.refresh_from_db()
        self.assertEqual(self.juror1_profile.rewards, 500)
