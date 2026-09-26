import random
from datetime import timedelta
from django.utils import timezone
from django.db import transaction
from django.contrib.auth.models import User
from django.urls import reverse
from ..models import Dispute, DisputeJuror, UserProfile, Friendship, FriendRequest, Conversation, Notification, RewardLedger


def get_1st_degree_friends(user):
    friend_ids = set()
    profile = getattr(user, 'userprofile', None)
    if profile:
        friend_ids.update(profile.friends.values_list('user_id', flat=True))
        friend_ids.update(Friendship.objects.filter(from_user=profile).values_list('to_user__user_id', flat=True))
        friend_ids.update(Friendship.objects.filter(to_user=profile).values_list('from_user__user_id', flat=True))
    
    friend_ids.update(FriendRequest.objects.filter(from_user=user, is_accepted=True).values_list('to_user_id', flat=True))
    friend_ids.update(FriendRequest.objects.filter(to_user=user, is_accepted=True).values_list('from_user_id', flat=True))
    friend_ids.discard(user.id)
    return friend_ids


def get_1st_and_2nd_degree_friends(user):
    first_degree = get_1st_degree_friends(user)
    all_friends = set(first_degree)
    for f_id in first_degree:
        try:
            f_user = User.objects.get(id=f_id)
            second_degree = get_1st_degree_friends(f_user)
            all_friends.update(second_degree)
        except User.DoesNotExist:
            pass
    all_friends.discard(user.id)
    return all_friends


def get_excluded_user_ids(dispute):
    excluded = set()
    task = dispute.task

    # Disputants
    disputants = []
    if task.posted_by:
        disputants.append(task.posted_by)
        excluded.add(task.posted_by.id)
    if task.taken_by:
        disputants.append(task.taken_by)
        excluded.add(task.taken_by.id)
    if dispute.raised_by and dispute.raised_by not in disputants:
        disputants.append(dispute.raised_by)
        excluded.add(dispute.raised_by.id)

    # 1st and 2nd degree friends
    for user in disputants:
        excluded.update(get_1st_and_2nd_degree_friends(user))

    # Hostel / Batch / Room_no cohort exclusions
    for user in disputants:
        profile = getattr(user, 'userprofile', None)
        if profile and profile.hostel and profile.hostel.strip():
            hostel_val = profile.hostel.strip()
            if profile.batch:
                cohort_batch = UserProfile.objects.filter(
                    hostel__iexact=hostel_val,
                    batch=profile.batch
                ).values_list('user_id', flat=True)
                excluded.update(cohort_batch)
            if profile.room_no and profile.room_no.strip():
                room_val = profile.room_no.strip()
                cohort_room = UserProfile.objects.filter(
                    hostel__iexact=hostel_val,
                    room_no__iexact=room_val
                ).values_list('user_id', flat=True)
                excluded.update(cohort_room)

    # Currently or previously assigned jurors
    assigned_juror_ids = dispute.jurors.values_list('user_id', flat=True)
    excluded.update(assigned_juror_ids)

    return excluded


def select_and_assign_jurors(dispute):
    with transaction.atomic():
        current_jurors = list(dispute.jurors.all())
        current_count = len(current_jurors)
        needed_count = 3 - current_count
        if needed_count <= 0:
            return current_jurors

        excluded_ids = get_excluded_user_ids(dispute)
        candidates = list(User.objects.filter(
            is_active=True,
            userprofile__rewards__gte=100
        ).exclude(id__in=excluded_ids))

        if len(candidates) < needed_count:
            # Fallback to staff intervention if candidates < needed_count
            return []

        selected_users = random.sample(candidates, needed_count)
        
        conversation, _ = Conversation.objects.get_or_create(task=dispute.task)
        if dispute.task.posted_by:
            conversation.participants.add(dispute.task.posted_by)
        if dispute.task.taken_by:
            conversation.participants.add(dispute.task.taken_by)

        assigned = []
        for user in selected_users:
            juror = DisputeJuror.objects.create(dispute=dispute, user=user)
            conversation.participants.add(user)
            
            Notification.objects.create(
                recipient=user,
                message=f"You have been assigned as a juror for dispute on task: '{dispute.task.title}'",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            
            RewardLedger.objects.create(
                user=user,
                task=dispute.task,
                amount=0,
                transaction_type='dispute_deposit',
                description=f"Assigned as juror for dispute on task: '{dispute.task.title}'"
            )
            assigned.append(juror)

        return list(dispute.jurors.all())


def check_and_replace_expired_jurors(dispute):
    if dispute.status != 'open':
        return

    cutoff = timezone.now() - timedelta(hours=24)
    expired_jurors = dispute.jurors.filter(vote='pending', assigned_at__lte=cutoff)

    for juror_assignment in expired_jurors:
        conversation = getattr(dispute.task, 'conversation', None)
        if conversation and juror_assignment.user not in [dispute.task.posted_by, dispute.task.taken_by]:
            conversation.participants.remove(juror_assignment.user)

        RewardLedger.objects.create(
            user=juror_assignment.user,
            task=dispute.task,
            amount=0,
            transaction_type='dispute_deposit',
            description=f"Unassigned as juror due to 24h timeout on dispute for task: '{dispute.task.title}'"
        )
        juror_assignment.delete()

    select_and_assign_jurors(dispute)


def process_juror_vote(juror_assignment, vote_choice, reasoning):
    if juror_assignment.vote != 'pending':
        raise ValueError("Vote has already been submitted and cannot be changed.")

    if vote_choice not in ['poster', 'taker']:
        raise ValueError("Invalid vote choice.")

    with transaction.atomic():
        juror_assignment.vote = vote_choice
        juror_assignment.reasoning = reasoning
        juror_assignment.voted_at = timezone.now()
        juror_assignment.save()

        RewardLedger.objects.create(
            user=juror_assignment.user,
            task=juror_assignment.dispute.task,
            amount=0,
            transaction_type='dispute_deposit',
            description=f"Submitted juror vote ({vote_choice}) for dispute on task: '{juror_assignment.dispute.task.title}'"
        )

        check_dispute_resolution(juror_assignment.dispute)


def check_dispute_resolution(dispute):
    if dispute.status != 'open':
        return False

    task = dispute.task
    poster_votes = dispute.jurors.filter(vote='poster').count()
    taker_votes = dispute.jurors.filter(vote='taker').count()

    if poster_votes >= 2:
        with transaction.atomic():
            dispute.status = 'resolved'
            if dispute.raised_by == task.posted_by:
                dispute.refund_deposit(
                    reason_description=f"Deposit bond refunded upon dispute resolution in favor of poster for task: '{task.title}'"
                )
            else:
                dispute.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Deposit bond forfeited to poster upon dispute resolution for task: '{task.title}'"
                )

            # Refund task reward reserved from poster
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for task reward upon dispute resolution in poster favor: '{task.title}'"
            )

            task.status = 'cancelled'
            task.save()

            RewardLedger.objects.create(
                user=dispute.raised_by,
                task=task,
                amount=0,
                transaction_type='dispute_deposit',
                description=f"Dispute resolved by majority juror vote in favor of poster for task: '{task.title}'"
            )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' resolved in your favor by community jurors.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' resolved in favor of the poster by community jurors.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
        return True

    elif taker_votes >= 2:
        with transaction.atomic():
            dispute.status = 'resolved'
            if dispute.raised_by == task.taken_by:
                dispute.refund_deposit(
                    reason_description=f"Deposit bond refunded upon dispute resolution in favor of taker for task: '{task.title}'"
                )
            else:
                dispute.forfeit_deposit(
                    beneficiary=task.taken_by,
                    reason_description=f"Deposit bond forfeited to taker upon dispute resolution for task: '{task.title}'"
                )

            # Award task reward to taker
            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Completed task reward upon dispute resolution in taker favor: '{task.title}'"
                )

            task.status = 'completed'
            task.save()

            RewardLedger.objects.create(
                user=dispute.raised_by,
                task=task,
                amount=0,
                transaction_type='dispute_deposit',
                description=f"Dispute resolved by majority juror vote in favor of taker for task: '{task.title}'"
            )

            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' resolved in your favor by community jurors.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' resolved in favor of the taker by community jurors.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        return True

    return False
