from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from django.urls import reverse
from basic.models import UserProfile, Task, Dispute, JuryPool, JuryAssignment, DisputeVote, RewardLedger
from basic.jury_utils import assign_jurors, cast_juror_vote, check_and_aggregate_dispute, sync_jury_pool


class JuryVotingTestCase(TestCase):
    def setUp(self):
        self.client = Client()

        # Create poster and taker
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=500)

        # Create poster's friend and taker's friend
        self.poster_friend = User.objects.create_user(username='poster_friend', password='password123')
        self.poster_friend_profile = UserProfile.objects.create(user=self.poster_friend, rewards=500)
        self.poster_profile.friends.add(self.poster_friend_profile)

        self.taker_friend = User.objects.create_user(username='taker_friend', password='password123')
        self.taker_friend_profile = UserProfile.objects.create(user=self.taker_friend, rewards=500)
        self.taker_profile.friends.add(self.taker_friend_profile)

        # Create neutral users for jury pool
        self.neutral_users = []
        for i in range(7):
            user = User.objects.create_user(username=f'juror_{i}', password='password123')
            profile = UserProfile.objects.create(user=user, rewards=200)
            self.neutral_users.append(user)

        sync_jury_pool()

        # Create task and dispute
        self.task = Task.objects.create(
            title='Fix website bug',
            description='Fix CSS styling issue on landing page',
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='disputed'
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='Poster refuses to mark task complete after work delivered'
        )

    def test_juror_assignment_conflict_of_interest(self):
        """Verify jurors are assigned randomly without conflict of interest and count is odd."""
        assignments = assign_jurors(self.dispute, count=5)
        assigned_jurors = [a.juror for a in assignments]

        # Odd number of jurors assigned
        self.assertEqual(len(assigned_jurors) % 2, 1)
        self.assertGreater(len(assigned_jurors), 0)

        # Check conflict of interest guardrail
        self.assertNotIn(self.poster, assigned_jurors)
        self.assertNotIn(self.taker, assigned_jurors)
        self.assertNotIn(self.poster_friend, assigned_jurors)
        self.assertNotIn(self.taker_friend, assigned_jurors)

        # All assigned jurors must be neutral users
        for juror in assigned_jurors:
            self.assertIn(juror, self.neutral_users)

    def test_majority_consensus_poster_wins(self):
        """Verify 3-of-5 majority for poster cancels task and refunds points to poster."""
        assignments = assign_jurors(self.dispute, count=5)
        jurors = [a.juror for a in assignments[:3]]

        initial_poster_rewards = self.poster_profile.rewards

        # Cast 3 votes for poster (majority of 5)
        for juror in jurors:
            cast_juror_vote(juror, self.dispute, vote_choice='poster', rationale='Poster evidence valid')

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Consensus executed
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winning_party, 'poster')
        self.assertEqual(self.task.status, 'cancelled')

        # Escrow distribution: points refunded to poster
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, initial_poster_rewards + self.task.reward)

        # Participating jurors receive reward points
        for juror in jurors:
            juror.userprofile.refresh_from_db()
            self.assertEqual(juror.userprofile.rewards, 210)  # 200 initial + 10 reward
            self.assertTrue(RewardLedger.objects.filter(user=juror, transaction_type='jury_reward').exists())

    def test_majority_consensus_taker_wins(self):
        """Verify 3-of-5 majority for taker completes task and awards points to taker."""
        assignments = assign_jurors(self.dispute, count=5)
        jurors = [a.juror for a in assignments[:3]]

        initial_taker_rewards = self.taker_profile.rewards

        # Cast 3 votes for taker (majority of 5)
        for juror in jurors:
            cast_juror_vote(juror, self.dispute, vote_choice='taker', rationale='Taker completed work')

        self.dispute.refresh_from_db()
        self.task.refresh_from_db()

        # Consensus executed
        self.assertEqual(self.dispute.status, 'resolved')
        self.assertEqual(self.dispute.winning_party, 'taker')
        self.assertEqual(self.task.status, 'completed')

        # Escrow distribution: points awarded to taker
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, initial_taker_rewards + self.task.reward)

    def test_confidential_voting_guardrail(self):
        """Verify votes remain hidden while voting is open and revealed upon resolution."""
        assignments = assign_jurors(self.dispute, count=5)

        # First juror votes
        cast_juror_vote(assignments[0].juror, self.dispute, vote_choice='poster')

        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertEqual(response.status_code, 200)

        # While voting is open, individual vote list should not be exposed
        self.assertFalse(response.context['show_results'])
        self.assertIsNone(response.context['votes'])

        # Finalize voting with majority
        cast_juror_vote(assignments[1].juror, self.dispute, vote_choice='poster')
        cast_juror_vote(assignments[2].juror, self.dispute, vote_choice='poster')

        response = self.client.get(reverse('dispute_detail', args=[self.dispute.id]))
        self.assertTrue(response.context['show_results'])
        self.assertIsNotNone(response.context['votes'])

    def test_missed_deadline_disqualifies_non_voting_jurors(self):
        """Verify jurors who fail to vote before deadline lose juror eligibility status."""
        assignments = assign_jurors(self.dispute, count=5)

        # Set deadline to past
        self.dispute.voting_deadline = timezone.now() - timedelta(hours=1)
        self.dispute.save()

        # Juror 0 votes before deadline check, Jurors 1-4 do not
        cast_juror_vote(assignments[0].juror, self.dispute, vote_choice='poster')

        # Run aggregation upon deadline
        check_and_aggregate_dispute(self.dispute)

        # Non-voting jurors are disqualified
        for assignment in assignments[1:]:
            jury_pool = JuryPool.objects.get(user=assignment.juror)
            self.assertFalse(jury_pool.is_eligible)

        # Voting juror retains eligibility
        voting_juror_pool = JuryPool.objects.get(user=assignments[0].juror)
        self.assertTrue(voting_juror_pool.is_eligible)
