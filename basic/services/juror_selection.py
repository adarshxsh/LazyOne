import random
from django.contrib.auth.models import User
from basic.models import UserProfile, DisputeJuror, Friendship

class ORMFilteredJurorSelectionService:
    """
    Active User Pool Sampling with Explicit Friendship and Mutual Connection Filter.
    Excludes direct 1st-degree friends and 1-hop mutual connections of disputants directly at the database layer.
    """

    @classmethod
    def get_exclusion_set(cls, dispute_or_task):
        """
        Builds combined exclusion set E = direct_friend_ids U mutual_friend_ids U {poster.id, worker.id, raised_by.id}
        """
        if hasattr(dispute_or_task, 'task'):
            dispute = dispute_or_task
            task = dispute.task
        else:
            task = dispute_or_task
            dispute = getattr(task, 'dispute', None)

        exclusion_ids = set()

        participant_ids = [uid for uid in [task.posted_by_id, task.taken_by_id, getattr(dispute, 'raised_by_id', None)] if uid]
        exclusion_ids.update(participant_ids)

        poster_profile = getattr(task.posted_by, 'userprofile', None) if task.posted_by else None
        worker_profile = getattr(task.taken_by, 'userprofile', None) if task.taken_by else None

        participant_profiles = [p for p in [poster_profile, worker_profile] if p is not None]
        participant_profile_ids = [p.id for p in participant_profiles]

        if not participant_profile_ids:
            return exclusion_ids

        # Direct 1st-degree friends extraction via ORM
        through_direct = UserProfile.friends.through.objects.filter(
            from_userprofile_id__in=participant_profile_ids
        )
        direct_friend_profile_ids = set(through_direct.values_list('to_userprofile_id', flat=True))
        direct_friend_user_ids = set(through_direct.values_list('to_userprofile__user_id', flat=True))

        # Direct friends from Friendship model
        fs1 = list(Friendship.objects.filter(from_user_id__in=participant_profile_ids).values_list('to_user__user_id', 'to_user_id'))
        fs2 = list(Friendship.objects.filter(to_user_id__in=participant_profile_ids).values_list('from_user__user_id', 'from_user_id'))
        for u_id, p_id in fs1 + fs2:
            direct_friend_user_ids.add(u_id)
            direct_friend_profile_ids.add(p_id)

        exclusion_ids.update(direct_friend_user_ids)

        # 1-Hop Mutual Connections Extraction
        if direct_friend_profile_ids:
            fof_user_ids = UserProfile.friends.through.objects.filter(
                from_userprofile_id__in=direct_friend_profile_ids
            ).values_list('to_userprofile__user_id', flat=True)
            exclusion_ids.update(fof_user_ids)

            fs_m1 = Friendship.objects.filter(from_user_id__in=direct_friend_profile_ids).values_list('to_user__user_id', flat=True)
            fs_m2 = Friendship.objects.filter(to_user_id__in=direct_friend_profile_ids).values_list('from_user__user_id', flat=True)
            exclusion_ids.update(fs_m1)
            exclusion_ids.update(fs_m2)

        return exclusion_ids

    @classmethod
    def get_eligible_candidates(cls, dispute_or_task):
        """
        Queries active users excluding the combined exclusion set.
        """
        exclusion_set = cls.get_exclusion_set(dispute_or_task)
        return User.objects.filter(is_active=True).exclude(id__in=exclusion_set)

    @classmethod
    def select_and_assign_jurors(cls, dispute, k=3):
        """
        Samples K active eligible candidates and assigns them as DisputeJuror entries.
        """
        eligible_qs = cls.get_eligible_candidates(dispute)
        candidate_ids = list(eligible_qs.values_list('id', flat=True))

        if not candidate_ids:
            dispute.juror_pool_status = 'pending'
            dispute.save()
            return []

        selected_count = min(k, len(candidate_ids))
        selected_juror_ids = random.sample(candidate_ids, selected_count)

        jurors = []
        for u_id in selected_juror_ids:
            dj, _ = DisputeJuror.objects.get_or_create(dispute=dispute, user_id=u_id)
            jurors.append(dj)

        dispute.juror_pool_status = 'assigned'
        dispute.save()
        return jurors

    @classmethod
    def select_jurors(cls, dispute, k=3):
        """
        Alias for select_and_assign_jurors.
        """
        return cls.select_and_assign_jurors(dispute, k=k)
