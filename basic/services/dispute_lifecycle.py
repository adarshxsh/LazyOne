import random
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.conf import settings
from django.urls import reverse
from django.contrib.auth.models import User

from ..models import (
    Dispute, DisputeEvidence, JuryAssignment, DisputeVote,
    Task, UserProfile, RewardLedger, Notification, Friendship
)


class DisputeLifecycleService:
    VALID_TRANSITIONS = {
        Dispute.OPEN: [Dispute.EVIDENCE_COLLECTION, Dispute.RESOLVED_POSTER, Dispute.RESOLVED_TAKER, Dispute.CANCELLED],
        Dispute.EVIDENCE_COLLECTION: [Dispute.JURY_SELECTION, Dispute.RESOLVED_POSTER, Dispute.RESOLVED_TAKER, Dispute.CANCELLED],
        Dispute.JURY_SELECTION: [Dispute.VOTING, Dispute.RESOLVED_POSTER, Dispute.RESOLVED_TAKER, Dispute.CANCELLED],
        Dispute.VOTING: [Dispute.APPEAL, Dispute.RESOLVED_POSTER, Dispute.RESOLVED_TAKER, Dispute.CANCELLED],
        Dispute.APPEAL: [Dispute.RESOLVED_POSTER, Dispute.RESOLVED_TAKER, Dispute.CANCELLED],
        Dispute.RESOLVED_POSTER: [],
        Dispute.RESOLVED_TAKER: [],
        Dispute.CANCELLED: [],
    }

    @classmethod
    def validate_transition(cls, current_status, new_status):
        if current_status == new_status:
            return
        allowed = cls.VALID_TRANSITIONS.get(current_status, [])
        if new_status not in allowed:
            raise ValidationError(f"Invalid state transition from '{current_status}' to '{new_status}'")

    @classmethod
    def raise_dispute(cls, task, user, reason):
        if user not in [task.posted_by, task.taken_by]:
            raise ValidationError("Only task poster or taker can raise a dispute.")
        if task.status not in ['in_progress', 'disputed']:
            raise ValidationError("Disputes can only be raised for tasks currently in progress.")

        deposit_amount = task.deposit_bond_amount
        if hasattr(user, 'userprofile'):
            user_profile = user.userprofile
            if user_profile.rewards < deposit_amount:
                raise ValidationError(f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to raise a dispute, but you only have {user_profile.rewards} points.")

        evidence_hours = getattr(settings, 'DISPUTE_EVIDENCE_HOURS', 48)
        evidence_deadline = timezone.now() + timedelta(hours=evidence_hours)

        with transaction.atomic():
            if hasattr(user, 'userprofile'):
                user_profile.rewards -= deposit_amount
                user_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = user
                dispute.reason = reason
                dispute.status = Dispute.EVIDENCE_COLLECTION
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.evidence_deadline = evidence_deadline
                dispute.winning_party = None
                dispute.winning_side = None
                dispute.resolved_at = None
                dispute.save()
                dispute.evidence_entries.all().delete()
                dispute.jury_assignments.all().delete()
                dispute.votes.all().delete()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=user,
                    reason=reason,
                    status=Dispute.EVIDENCE_COLLECTION,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    evidence_deadline=evidence_deadline
                )

            RewardLedger.objects.create(
                user=user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

            counterparty = task.taken_by if user == task.posted_by else task.posted_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        return dispute

    @classmethod
    def submit_evidence(cls, dispute, user, text_evidence="", external_link=None, file_attachment=None):
        if dispute.status not in [Dispute.EVIDENCE_COLLECTION, Dispute.OPEN]:
            raise ValidationError("Evidence can only be submitted during the EVIDENCE_COLLECTION stage.")

        if user not in [dispute.task.posted_by, dispute.task.taken_by]:
            raise ValidationError("Only disputing parties can submit evidence.")

        if dispute.evidence_deadline and timezone.now() > dispute.evidence_deadline:
            cls.transition_to_jury_selection(dispute)
            raise ValidationError("Evidence collection deadline has passed.")

        evidence = DisputeEvidence.objects.create(
            dispute=dispute,
            submitted_by=user,
            text_evidence=text_evidence,
            external_link=external_link,
            file_attachment=file_attachment
        )

        poster_has_evidence = dispute.evidence_entries.filter(submitted_by=dispute.task.posted_by).exists()
        taker_has_evidence = dispute.evidence_entries.filter(submitted_by=dispute.task.taken_by).exists()

        if poster_has_evidence and taker_has_evidence:
            cls.transition_to_jury_selection(dispute)

        return evidence

    @classmethod
    def transition_to_jury_selection(cls, dispute):
        cls.validate_transition(dispute.status, Dispute.JURY_SELECTION)
        dispute.status = Dispute.JURY_SELECTION
        dispute.save()

        cls.select_and_assign_jurors(dispute, target_count=5)

    @classmethod
    def select_and_assign_jurors(cls, dispute, target_count=5):
        task = dispute.task
        excluded_user_ids = {task.posted_by_id}
        if task.taken_by_id:
            excluded_user_ids.add(task.taken_by_id)

        # Exclude friends of posted_by and taken_by
        for party in [task.posted_by, task.taken_by]:
            if not party:
                continue
            if hasattr(party, 'userprofile'):
                friends_ids = party.userprofile.friends.all().values_list('user_id', flat=True)
                excluded_user_ids.update(friends_ids)

            # Check Friendship model
            fs_from = Friendship.objects.filter(from_user__user=party).values_list('to_user__user_id', flat=True)
            fs_to = Friendship.objects.filter(to_user__user=party).values_list('from_user__user_id', flat=True)
            excluded_user_ids.update(fs_from)
            excluded_user_ids.update(fs_to)

        candidates = list(User.objects.filter(is_active=True).exclude(id__in=excluded_user_ids))

        if len(candidates) < target_count:
            # Fallback to non-participants if not enough non-friends available
            fallback_excluded = {task.posted_by_id}
            if task.taken_by_id:
                fallback_excluded.add(task.taken_by_id)
            candidates = list(User.objects.filter(is_active=True).exclude(id__in=fallback_excluded))

        selected_count = min(target_count, len(candidates))
        selected_jurors = random.sample(candidates, selected_count) if len(candidates) >= selected_count else candidates

        for juror in selected_jurors:
            JuryAssignment.objects.get_or_create(dispute=dispute, juror=juror)
            Notification.objects.create(
                recipient=juror,
                message=f"You have been selected as a peer juror for a dispute on task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        cls.validate_transition(dispute.status, Dispute.VOTING)
        dispute.status = Dispute.VOTING
        dispute.save()

    @classmethod
    def cast_vote(cls, dispute, juror, choice):
        if dispute.status != Dispute.VOTING:
            raise ValidationError("Votes can only be cast during the VOTING stage.")

        if not dispute.jury_assignments.filter(juror=juror).exists():
            raise ValidationError("You are not assigned as a juror for this dispute.")

        if dispute.votes.filter(voter=juror).exists():
            raise ValidationError("You have already voted on this dispute.")

        clean_choice = choice.lower().replace('resolved_', '')
        if clean_choice not in ['poster', 'taker']:
            raise ValidationError("Invalid vote choice. Must be 'poster' or 'taker'.")

        voted_for = dispute.task.posted_by if clean_choice == 'poster' else dispute.task.taken_by

        vote = DisputeVote.objects.create(
            dispute=dispute,
            voter=juror,
            choice=clean_choice,
            voted_for=voted_for
        )

        total_jurors = dispute.jury_assignments.count()
        total_votes = dispute.votes.count()

        poster_votes = dispute.votes.filter(
            Q(choice__in=['poster', 'resolved_poster']) | Q(voted_for=dispute.task.posted_by)
        ).count()
        taker_votes = dispute.votes.filter(
            Q(choice__in=['taker', 'resolved_taker']) | Q(voted_for=dispute.task.taken_by)
        ).count()

        quorum_threshold = (total_jurors // 2) + 1 if total_jurors > 0 else 1

        if poster_votes >= quorum_threshold or taker_votes >= quorum_threshold or total_votes >= total_jurors:
            winning_side = 'poster' if poster_votes >= taker_votes else 'taker'
            cls.transition_to_appeal(dispute, winning_side)

        return vote

    @classmethod
    def transition_to_appeal(cls, dispute, winning_side):
        cls.validate_transition(dispute.status, Dispute.APPEAL)
        appeal_hours = getattr(settings, 'DISPUTE_APPEAL_HOURS', 24)

        dispute.status = Dispute.APPEAL
        dispute.winning_side = winning_side
        dispute.appeal_deadline = timezone.now() + timedelta(hours=appeal_hours)
        dispute.save()

        msg = f"Voting for task '{dispute.task.title}' has ended. Preliminary result: {winning_side.capitalize()} wins. Appeal window is open."
        for party in [dispute.task.posted_by, dispute.task.taken_by]:
            if party:
                Notification.objects.create(
                    recipient=party,
                    message=msg,
                    link=reverse('dispute_detail', args=[dispute.id])
                )

    @classmethod
    def request_appeal(cls, dispute, user, reason=""):
        if user not in [dispute.task.posted_by, dispute.task.taken_by]:
            raise ValidationError("Only disputing parties can request an appeal.")

        if dispute.status not in [Dispute.VOTING, Dispute.APPEAL]:
            raise ValidationError("Dispute is not in an appealable state.")

        dispute.status = Dispute.APPEAL
        dispute.save()

        # Notify staff users
        staff_users = User.objects.filter(is_staff=True)
        for staff in staff_users:
            Notification.objects.create(
                recipient=staff,
                message=f"An appeal has been requested for dispute on task '{dispute.task.title}' by {user.username}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    @classmethod
    def resolve_dispute(cls, dispute, winner_side):
        if dispute.status in [Dispute.RESOLVED_POSTER, Dispute.RESOLVED_TAKER, Dispute.CANCELLED]:
            return dispute

        clean_winner = winner_side.lower().replace('resolved_', '')
        if clean_winner not in ['poster', 'taker']:
            raise ValidationError("Invalid winner side. Must be 'poster' or 'taker'.")

        with transaction.atomic():
            task = dispute.task
            if clean_winner == 'poster':
                cls.validate_transition(dispute.status, Dispute.RESOLVED_POSTER)
                dispute.status = Dispute.RESOLVED_POSTER
                dispute.winning_party = task.posted_by
                dispute.winning_side = 'poster'
                task.status = 'cancelled'

                poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
                poster_profile.rewards += task.reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_settlement_poster',
                    description=f"Dispute resolved in favor of poster for task: '{task.title}'"
                )
            else:
                cls.validate_transition(dispute.status, Dispute.RESOLVED_TAKER)
                dispute.status = Dispute.RESOLVED_TAKER
                dispute.winning_party = task.taken_by
                dispute.winning_side = 'taker'
                task.status = 'completed'

                if task.taken_by:
                    taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='dispute_settlement_taker',
                        description=f"Dispute resolved in favor of taker for task: '{task.title}'"
                    )

            dispute.resolved_at = timezone.now()
            if dispute.escrow_status == 'held' and dispute.deposit_amount > 0:
                if dispute.winning_party == dispute.raised_by:
                    dispute.refund_deposit(
                        reason_description=f"Security deposit bond refunded upon dispute win for task: '{task.title}'"
                    )
                else:
                    dispute.forfeit_deposit(
                        beneficiary=dispute.winning_party,
                        reason_description=f"Security deposit bond forfeited upon dispute loss for task: '{task.title}'"
                    )
            task.save()
            dispute.save()

            # Reward participating jurors if desired
            juror_ids = dispute.votes.values_list('voter_id', flat=True)
            for juror_id in juror_ids:
                juror_user = User.objects.filter(id=juror_id).first()
                if juror_user:
                    j_profile, _ = UserProfile.objects.get_or_create(user=juror_user)
                    j_profile.rewards += 10
                    j_profile.save()
                    RewardLedger.objects.create(
                        user=juror_user,
                        task=task,
                        amount=10,
                        transaction_type='juror_reward',
                        description=f"Participation reward for jury duty on task: '{task.title}'"
                    )

            # Notify parties
            for party in [task.posted_by, task.taken_by]:
                if party:
                    Notification.objects.create(
                        recipient=party,
                        message=f"Dispute for task '{task.title}' has been resolved in favor of {clean_winner.capitalize()}.",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )

        return dispute

    @classmethod
    def withdraw_dispute(cls, dispute, user):
        if dispute.raised_by != user:
            raise ValidationError("Only the user who raised the dispute can withdraw it.")

        if dispute.status in [Dispute.RESOLVED_POSTER, Dispute.RESOLVED_TAKER]:
            raise ValidationError("Cannot withdraw an already resolved dispute.")

        with transaction.atomic():
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{dispute.task.title}'"
            )
            cls.validate_transition(dispute.status, Dispute.CANCELLED)
            dispute.status = Dispute.CANCELLED
            task = dispute.task
            task.status = 'in_progress'
            task.save()
            dispute.save()

            counterparty = task.taken_by if user == task.posted_by else task.posted_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{user.username} has withdrawn the dispute for '{task.title}'. Task is now in progress.",
                    link=reverse('my_tasks')
                )

    @classmethod
    def check_and_process_deadlines(cls):
        now = timezone.now()
        # Process evidence deadline expirations
        expired_evidence_disputes = Dispute.objects.filter(
            status__in=[Dispute.EVIDENCE_COLLECTION, Dispute.OPEN],
            evidence_deadline__lt=now
        )
        for dispute in expired_evidence_disputes:
            cls.transition_to_jury_selection(dispute)

        # Process appeal deadline expirations
        expired_appeal_disputes = Dispute.objects.filter(
            status=Dispute.APPEAL,
            appeal_deadline__lt=now
        )
        for dispute in expired_appeal_disputes:
            winner = dispute.winning_side or 'poster'
            cls.resolve_dispute(dispute, winner)
