from django.db.models import Q
from django.urls import reverse
from .models import UserProfile, Dispute, DisputeVote, RewardLedger, FriendRequest, Friendship, Notification, User

def select_jurors_for_dispute(dispute, panel_size=3):
    """
    Selects neutral jurors for a dispute using anti-collusion filters.
    Excludes task poster, worker, direct friends, and pending friend request connections.
    Candidates must have verified contact details and sufficient reward balance.
    """
    dispute.status = 'selection'
    dispute.save()

    poster = dispute.task.posted_by
    worker = dispute.task.taken_by

    excluded_user_ids = set()
    if poster:
        excluded_user_ids.add(poster.id)
    if worker:
        excluded_user_ids.add(worker.id)

    # Exclude direct friends via UserProfile.friends
    if poster and hasattr(poster, 'userprofile'):
        for f in poster.userprofile.friends.all():
            excluded_user_ids.add(f.user.id)
    if worker and hasattr(worker, 'userprofile'):
        for f in worker.userprofile.friends.all():
            excluded_user_ids.add(f.user.id)

    # Exclude friends via Friendship model
    friendships = Friendship.objects.filter(
        Q(from_user__user=poster) | Q(to_user__user=poster) |
        (Q(from_user__user=worker) | Q(to_user__user=worker) if worker else Q())
    )
    for fs in friendships:
        if fs.from_user and fs.from_user.user:
            excluded_user_ids.add(fs.from_user.user.id)
        if fs.to_user and fs.to_user.user:
            excluded_user_ids.add(fs.to_user.user.id)

    # Exclude active/pending FriendRequest connections
    friend_requests = FriendRequest.objects.filter(
        Q(from_user=poster) | Q(to_user=poster) |
        (Q(from_user=worker) | Q(to_user=worker) if worker else Q())
    )
    for fr in friend_requests:
        excluded_user_ids.add(fr.from_user.id)
        excluded_user_ids.add(fr.to_user.id)

    # Filter eligible candidates: active, not excluded, verified contact, sufficient rewards
    candidates = User.objects.filter(
        is_active=True
    ).exclude(
        id__in=excluded_user_ids
    ).filter(
        Q(userprofile__is_phone_verified=True) | Q(userprofile__is_instagram_verified=True),
        userprofile__rewards__gte=dispute.stake_amount
    ).order_by('?')

    selected_jurors = list(candidates[:panel_size])
    dispute.jurors.set(selected_jurors)

    dispute.status = 'voting_open'
    dispute.save()

    for juror in selected_jurors:
        Notification.objects.create(
            recipient=juror,
            message=f"You have been selected as a neutral juror for dispute on task '{dispute.task.title}'. Please review and cast your vote.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return selected_jurors

def cast_juror_vote(dispute, voter, voted_for):
    """
    Casts a vote for a juror on a dispute after locking required reward stake.
    """
    if dispute.status != 'voting_open':
        raise ValueError("Dispute is not open for voting.")

    if voter not in dispute.jurors.all():
        raise ValueError("You are not an assigned juror for this dispute.")

    task = dispute.task
    if voter in (task.posted_by, task.taken_by):
        raise ValueError("Task counterparties are strictly prohibited from voting.")

    if DisputeVote.objects.filter(dispute=dispute, voter=voter).exists():
        raise ValueError("You have already voted on this dispute.")

    if voted_for not in (task.posted_by, task.taken_by):
        raise ValueError("Invalid vote target. You must vote for either the task poster or worker.")

    voter_profile = voter.userprofile
    if voter_profile.rewards < dispute.stake_amount:
        raise ValueError("Insufficient reward balance to lock required stake.")

    # Lock stake
    voter_profile.rewards -= dispute.stake_amount
    voter_profile.save()

    RewardLedger.objects.create(
        user=voter,
        task=task,
        amount=-dispute.stake_amount,
        transaction_type='dispute_stake_lock',
        description=f"Stake locked for dispute #{dispute.id}"
    )

    vote = DisputeVote.objects.create(
        dispute=dispute,
        voter=voter,
        voted_for=voted_for,
        stake_amount=dispute.stake_amount
    )

    total_votes = dispute.votes.count()
    assigned_count = dispute.jurors.count()

    if total_votes >= dispute.quorum or (assigned_count > 0 and total_votes >= assigned_count):
        dispute.status = 'quorum_reached'
        dispute.save()
        resolve_dispute(dispute)

    return vote

def resolve_dispute(dispute):
    """
    Resolves dispute based on vote counts, updates task status, returns majority stakes with reward bonus,
    and slashes minority stakes.
    """
    task = dispute.task
    poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
    worker_votes = dispute.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

    if worker_votes > poster_votes:
        winner = task.taken_by
    elif poster_votes > worker_votes:
        winner = task.posted_by
    else:
        # Tie-breaker: worker if assigned, else poster
        winner = task.taken_by if task.taken_by else task.posted_by

    dispute.winner = winner
    dispute.status = 'resolved'
    dispute.save()

    # Process deposit bond
    if dispute.raised_by == winner:
        dispute.refund_deposit()
    else:
        dispute.forfeit_deposit(beneficiary=winner)

    # Update task state and handle reward transfer
    if winner == task.taken_by:
        task.status = 'completed'
        task.save()
        if winner and hasattr(winner, 'userprofile'):
            winner_profile = winner.userprofile
            winner_profile.rewards += task.reward
            winner_profile.save()
            RewardLedger.objects.create(
                user=winner,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Task points awarded via dispute resolution for '{task.title}'"
            )
    else:
        task.status = 'cancelled'
        task.save()
        if winner and hasattr(winner, 'userprofile'):
            winner_profile = winner.userprofile
            winner_profile.rewards += task.reward
            winner_profile.save()
            RewardLedger.objects.create(
                user=winner,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task points refunded via dispute resolution for '{task.title}'"
            )

    # Stake redistribution
    majority_votes = dispute.votes.filter(voted_for=winner)
    minority_votes = dispute.votes.exclude(voted_for=winner)

    total_slashed = sum(v.stake_amount for v in minority_votes)
    majority_count = majority_votes.count()
    pro_rata_share = (total_slashed // majority_count) if majority_count > 0 else 0

    for vote in minority_votes:
        RewardLedger.objects.create(
            user=vote.voter,
            task=task,
            amount=-vote.stake_amount,
            transaction_type='dispute_stake_slashed',
            description=f"Stake slashed for dispute #{dispute.id}"
        )

    for vote in majority_votes:
        voter_profile = vote.voter.userprofile
        refund_amount = vote.stake_amount
        payout_amount = pro_rata_share

        voter_profile.rewards += (refund_amount + payout_amount)
        voter_profile.save()

        RewardLedger.objects.create(
            user=vote.voter,
            task=task,
            amount=refund_amount,
            transaction_type='dispute_stake_refund',
            description=f"Stake refunded for majority vote on dispute #{dispute.id}"
        )
        if payout_amount > 0:
            RewardLedger.objects.create(
                user=vote.voter,
                task=task,
                amount=payout_amount,
                transaction_type='dispute_reward_payout',
                description=f"Reward payout from slashed stakes for dispute #{dispute.id}"
            )

    # Send notifications
    Notification.objects.create(
        recipient=task.posted_by,
        message=f"Dispute for task '{task.title}' resolved in favor of {winner.username}.",
        link=reverse('dispute_detail', args=[dispute.id])
    )
    if task.taken_by:
        Notification.objects.create(
            recipient=task.taken_by,
            message=f"Dispute for task '{task.title}' resolved in favor of {winner.username}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    for juror in dispute.jurors.all():
        Notification.objects.create(
            recipient=juror,
            message=f"Dispute on task '{task.title}' has been resolved. Results and payouts are updated.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return dispute

def expire_dispute(dispute):
    """
    Expires a dispute if voting window/timer elapsed without reaching quorum, refunding juror stakes.
    """
    dispute.status = 'expired'
    dispute.save()
    dispute.refund_deposit(reason_description=f"Security deposit bond refunded due to dispute #{dispute.id} expiration")

    for vote in dispute.votes.all():
        voter_profile = vote.voter.userprofile
        voter_profile.rewards += vote.stake_amount
        voter_profile.save()

        RewardLedger.objects.create(
            user=vote.voter,
            task=dispute.task,
            amount=vote.stake_amount,
            transaction_type='dispute_stake_refund',
            description=f"Stake refunded due to dispute #{dispute.id} expiration"
        )

    Notification.objects.create(
        recipient=dispute.task.posted_by,
        message=f"Dispute for task '{dispute.task.title}' expired without quorum.",
        link=reverse('dispute_detail', args=[dispute.id])
    )
    if dispute.task.taken_by:
        Notification.objects.create(
            recipient=dispute.task.taken_by,
            message=f"Dispute for task '{dispute.task.title}' expired without quorum.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    return dispute
