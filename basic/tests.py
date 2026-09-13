from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User
from basic.models import UserProfile, Task, Dispute, Friendship, Notification
from basic.services.juror_selection import (
    select_jurors_for_dispute,
    get_excluded_user_ids,
    InsufficientJurorsError,
    STAKE_THRESHOLD,
)


class JurorSelectionServiceTest(TestCase):
    def setUp(self):
        # Create Poster and Worker
        self.poster = User.objects.create_user(username="poster", password="password")
        self.worker = User.objects.create_user(username="worker", password="password")

        self.poster_profile = UserProfile.objects.get_or_create(user=self.poster, defaults={'rewards': 1000})[0]
        self.worker_profile = UserProfile.objects.get_or_create(user=self.worker, defaults={'rewards': 1000})[0]
        self.poster_profile.rewards = 1000
        self.poster_profile.save()
        self.worker_profile.rewards = 1000
        self.worker_profile.save()

        # Create Task & Dispute
        self.task = Task.objects.create(
            title="Disputed Task",
            description="Task details",
            reward=100,
            posted_by=self.poster,
            taken_by=self.worker,
            status="disputed",
        )
        self.dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.worker,
            reason="Work not accepted",
        )

    def test_select_jurors_success(self):
        """Verify selecting an odd-numbered panel of neutral jurors succeeds when enough candidates exist."""
        candidates = []
        for i in range(5):
            u = User.objects.create_user(username=f"juror_{i}", password="password")
            p = UserProfile.objects.get_or_create(user=u)[0]
            p.rewards = 100
            p.save()
            candidates.append(u)

        panel = select_jurors_for_dispute(self.dispute, panel_size=3)
        self.assertEqual(len(panel), 3)
        for juror in panel:
            self.assertNotIn(juror, [self.poster, self.worker])
            self.assertIn(juror, candidates)

        # Check summons notifications were created
        for juror in panel:
            self.assertTrue(Notification.objects.filter(recipient=juror).exists())

    def test_even_panel_size_raises_value_error(self):
        """Even panel_size or non-positive panel_size should raise ValueError."""
        with self.assertRaises(ValueError):
            select_jurors_for_dispute(self.dispute, panel_size=4)
        with self.assertRaises(ValueError):
            select_jurors_for_dispute(self.dispute, panel_size=0)

    def test_exclusion_of_poster_and_worker(self):
        """Poster and worker must be in excluded user IDs set."""
        excluded = get_excluded_user_ids(self.dispute)
        self.assertIn(self.poster.id, excluded)
        self.assertIn(self.worker.id, excluded)

    def test_exclusion_of_direct_friends_userprofile_friends(self):
        """Friends in UserProfile.friends (forward and reverse) must be excluded."""
        friend_forward = User.objects.create_user(username="friend_fwd", password="password")
        friend_fwd_profile = UserProfile.objects.get_or_create(user=friend_forward, defaults={'rewards': 100})[0]
        self.poster_profile.friends.add(friend_fwd_profile)

        friend_reverse = User.objects.create_user(username="friend_rev", password="password")
        friend_rev_profile = UserProfile.objects.get_or_create(user=friend_reverse, defaults={'rewards': 100})[0]
        friend_rev_profile.friends.add(self.worker_profile)

        excluded = get_excluded_user_ids(self.dispute)
        self.assertIn(friend_forward.id, excluded)
        self.assertIn(friend_reverse.id, excluded)

    def test_exclusion_of_direct_friends_friendship_model(self):
        """Friends in Friendship model (from_user and to_user) must be excluded."""
        f1_user = User.objects.create_user(username="friendship_1", password="password")
        f1_profile = UserProfile.objects.get_or_create(user=f1_user, defaults={'rewards': 100})[0]
        Friendship.objects.create(from_user=self.poster_profile, to_user=f1_profile)

        f2_user = User.objects.create_user(username="friendship_2", password="password")
        f2_profile = UserProfile.objects.get_or_create(user=f2_user, defaults={'rewards': 100})[0]
        Friendship.objects.create(from_user=f2_profile, to_user=self.worker_profile)

        excluded = get_excluded_user_ids(self.dispute)
        self.assertIn(f1_user.id, excluded)
        self.assertIn(f2_user.id, excluded)

    def test_exclusion_of_active_task_partners(self):
        """Users currently engaged in active (in_progress or disputed) tasks with poster or worker are excluded."""
        partner_1 = User.objects.create_user(username="partner_1", password="password")
        UserProfile.objects.get_or_create(user=partner_1, defaults={'rewards': 100})
        # Active task where poster is taken_by and partner_1 is posted_by
        Task.objects.create(
            title="Active Task 1", description="desc", reward=10,
            posted_by=partner_1, taken_by=self.poster, status="in_progress"
        )

        partner_2 = User.objects.create_user(username="partner_2", password="password")
        UserProfile.objects.get_or_create(user=partner_2, defaults={'rewards': 100})
        # Active task where worker is posted_by and partner_2 is taken_by
        Task.objects.create(
            title="Active Task 2", description="desc", reward=10,
            posted_by=self.worker, taken_by=partner_2, status="disputed"
        )

        # Inactive partner (task is completed)
        inactive_partner = User.objects.create_user(username="completed_partner", password="password")
        UserProfile.objects.get_or_create(user=inactive_partner, defaults={'rewards': 100})
        Task.objects.create(
            title="Completed Task", description="desc", reward=10,
            posted_by=self.poster, taken_by=inactive_partner, status="completed"
        )

        excluded = get_excluded_user_ids(self.dispute)
        self.assertIn(partner_1.id, excluded)
        self.assertIn(partner_2.id, excluded)
        self.assertNotIn(inactive_partner.id, excluded)

    def test_stake_threshold_filtering(self):
        """Candidates with rewards below STAKE_THRESHOLD (50) or inactive status are excluded."""
        low_reward_user = User.objects.create_user(username="low_reward", password="password")
        low_profile = UserProfile.objects.get_or_create(user=low_reward_user)[0]
        low_profile.rewards = 49
        low_profile.save()

        inactive_user = User.objects.create_user(username="inactive_user", password="password", is_active=False)
        in_profile = UserProfile.objects.get_or_create(user=inactive_user)[0]
        in_profile.rewards = 100
        in_profile.save()

        valid_user = User.objects.create_user(username="valid_user", password="password")
        v_profile = UserProfile.objects.get_or_create(user=valid_user)[0]
        v_profile.rewards = 50
        v_profile.save()

        # Try to select 1 juror (pass panel_size=1)
        jurors = select_jurors_for_dispute(self.dispute, panel_size=1)
        self.assertEqual(len(jurors), 1)
        self.assertEqual(jurors[0], valid_user)

    def test_insufficient_jurors_error(self):
        """Raises InsufficientJurorsError when eligible neutral candidate count < panel_size."""
        # Only 2 eligible candidates exist, but requested panel_size is 3
        for i in range(2):
            u = User.objects.create_user(username=f"candidate_{i}", password="password")
            p = UserProfile.objects.get_or_create(user=u)[0]
            p.rewards = 100
            p.save()

        with self.assertRaises(InsufficientJurorsError):
            select_jurors_for_dispute(self.dispute, panel_size=3)

    def test_raise_dispute_integration(self):
        """Raising a dispute via HTTP view triggers juror selection and handles candidate pool gracefully."""
        # Create eligible candidates
        for i in range(3):
            u = User.objects.create_user(username=f"juror_candidate_{i}", password="password")
            p = UserProfile.objects.get_or_create(user=u)[0]
            p.rewards = 100
            p.save()

        # Worker logs in and raises dispute for an in_progress task
        new_task = Task.objects.create(
            title="In Progress Task", description="Desc", reward=50,
            posted_by=self.poster, taken_by=self.worker, status="in_progress"
        )

        self.client.force_login(self.worker)
        response = self.client.post(reverse('raise_dispute', args=[new_task.id]), {'reason': 'Unsatisfied with conditions'})

        self.assertEqual(response.status_code, 302)
        new_task.refresh_from_db()
        self.assertEqual(new_task.status, 'disputed')
        self.assertTrue(hasattr(new_task, 'dispute'))
