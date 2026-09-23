import math
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.models import User
from ..models import Dispute, JurorAssignment, DisputeAppeal, RewardLedger, Notification, UserProfile

class DisputeService:

    @staticmethod
    def assemble_juror_panel(dispute, tier=1, count=3):
        task = dispute.task
        excluded_user_ids = {task.posted_by.id}
        if task.taken_by:
            excluded_user_ids.add(task.taken_by.id)

        # Exclude jurors who served in Tier 1 if assembling Tier 2
        if tier == 2:
            tier1_juror_ids = JurorAssignment.objects.filter(
                dispute=dispute, tier=1
            ).values_list('juror_id', flat=True)
            excluded_user_ids.update(tier1_juror_ids)

        # Exclude already assigned jurors in this tier
        assigned_in_tier = JurorAssignment.objects.filter(
            dispute=dispute, tier=tier
        ).values_list('juror_id', flat=True)
        excluded_user_ids.update(assigned_in_tier)

        # Exclude friends of poster and taker if possible
        poster_profile = getattr(task.posted_by, 'userprofile', None)
        taker_profile = getattr(task.taken_by, 'userprofile', None) if task.taken_by else None

        friend_ids = set()
        if poster_profile:
            friend_ids.update(poster_profile.friends.values_list('user_id', flat=True))
        if taker_profile:
            friend_ids.update(taker_profile.friends.values_list('user_id', flat=True))

        candidate_qs = User.objects.exclude(id__in=excluded_user_ids)
        filtered_qs = candidate_qs.exclude(id__in=friend_ids)

        if filtered_qs.count() >= count:
            candidates = list(filtered_qs.order_by('?')[:count])
        else:
            candidates = list(candidate_qs.order_by('?')[:count])

        created_assignments = []
        with transaction.atomic():
            for juror in candidates:
                assignment, _ = JurorAssignment.objects.get_or_create(
                    dispute=dispute,
                    juror=juror,
                    tier=tier
                )
                created_assignments.append(assignment)

            if dispute.status != 'open' and dispute.status != 'tier1_resolved' and dispute.status != 'appealed':
                dispute.status = 'voting'
            dispute.save()

        return created_assignments

    @staticmethod
    def submit_juror_vote(dispute, juror, vote, tier=1):
        if vote not in ['poster', 'taker']:
            raise ValueError("Invalid vote option. Must be 'poster' or 'taker'.")

        with transaction.atomic():
            assignment = JurorAssignment.objects.get(
                dispute=dispute,
                juror=juror,
                tier=tier
            )
            assignment.vote = vote
            assignment.voted_at = timezone.now()
            assignment.save()

            assignments = JurorAssignment.objects.filter(dispute=dispute, tier=tier)
            total_assigned = assignments.count()
            voted_assignments = assignments.filter(vote__isnull=False)
            total_voted = voted_assignments.count()

            if total_voted > 0:
                poster_votes = voted_assignments.filter(vote='poster').count()
                taker_votes = voted_assignments.filter(vote='taker').count()

                poster_pct = poster_votes / total_voted
                taker_pct = taker_votes / total_voted

                winner = None
                if poster_pct >= 0.66:
                    winner = 'poster'
                elif taker_pct >= 0.66:
                    winner = 'taker'

                threshold = math.ceil(total_assigned * 0.66) if total_assigned > 0 else 1
                if winner and (poster_votes >= threshold or taker_votes >= threshold or total_voted == total_assigned):
                    if tier == 1:
                        dispute.primary_verdict = winner
                        dispute.verdict_published_at = timezone.now()
                        dispute.status = 'tier1_resolved'
                        dispute.save()

                        # Notify participants
                        Notification.objects.create(
                            recipient=dispute.task.posted_by,
                            message=f"Primary ruling published for dispute on '{dispute.task.title}': {winner.capitalize()} favored. 48-hour appeal window open."
                        )
                        if dispute.task.taken_by:
                            Notification.objects.create(
                                recipient=dispute.task.taken_by,
                                message=f"Primary ruling published for dispute on '{dispute.task.title}': {winner.capitalize()} favored. 48-hour appeal window open."
                            )
                        return True, winner
                    elif tier == 2:
                        DisputeService.resolve_secondary_appeal(dispute)
                        return True, dispute.final_verdict

        return False, None

    @staticmethod
    def file_appeal(dispute, appellant, reason, stake_amount=None):
        if not dispute.can_appeal(appellant):
            raise ValueError("This dispute cannot be appealed by the specified user at this time.")

        stake_amount = stake_amount or dispute.task.deposit_bond_amount
        appellant_profile = appellant.userprofile

        if appellant_profile.rewards < stake_amount:
            raise ValueError(f"Insufficient reward points balance. Need {stake_amount} points, but have {appellant_profile.rewards}.")

        with transaction.atomic():
            appellant_profile.rewards -= stake_amount
            appellant_profile.save()

            RewardLedger.objects.create(
                user=appellant,
                task=dispute.task,
                amount=-stake_amount,
                transaction_type='appeal_deposit',
                description=f"Appeal stake bond held for dispute on task: '{dispute.task.title}'"
            )

            appeal = DisputeAppeal.objects.create(
                dispute=dispute,
                appellant=appellant,
                reason=reason,
                stake_amount=stake_amount,
                status='pending'
            )

            dispute.status = 'appealed'
            dispute.save()

            # Assemble Tier-2 secondary juror panel
            DisputeService.assemble_juror_panel(dispute, tier=2, count=3)

            # Notify counterparties
            other_party = dispute.task.posted_by if appellant == dispute.task.taken_by else dispute.task.taken_by
            if other_party:
                Notification.objects.create(
                    recipient=other_party,
                    message=f"{appellant.username} filed an appeal for dispute on task: '{dispute.task.title}'."
                )

        return appeal

    @staticmethod
    def resolve_secondary_appeal(dispute):
        with transaction.atomic():
            appeal = getattr(dispute, 'appeal', None)
            tier2_voted = JurorAssignment.objects.filter(
                dispute=dispute, tier=2, vote__isnull=False
            )
            total_voted = tier2_voted.count()
            if total_voted == 0:
                return

            poster_votes = tier2_voted.filter(vote='poster').count()
            taker_votes = tier2_voted.filter(vote='taker').count()

            tier2_winner = None
            if total_voted > 0 and poster_votes / total_voted >= 0.66:
                tier2_winner = 'poster'
            elif total_voted > 0 and taker_votes / total_voted >= 0.66:
                tier2_winner = 'taker'
            else:
                tier2_winner = 'poster' if poster_votes >= taker_votes else 'taker'

            # Did tier 2 uphold tier 1 verdict?
            is_upheld = (tier2_winner == dispute.primary_verdict)

            if is_upheld:
                # Litigant LOST appeal (frivolous appeal loss)
                if appeal:
                    appeal.status = 'upheld'
                    appeal.resolved_at = timezone.now()
                    appeal.save()

                    RewardLedger.objects.create(
                        user=appeal.appellant,
                        task=dispute.task,
                        amount=0,
                        transaction_type='litigant_slashing',
                        description=f"Appeal stake bond forfeited and slashed for frivolous appeal loss on task: '{dispute.task.title}'"
                    )
                final_verdict = dispute.primary_verdict
            else:
                # Litigant WON appeal (verdict reversed)
                if appeal:
                    appeal.status = 'reversed'
                    appeal.resolved_at = timezone.now()
                    appeal.save()

                    appellant_profile = appeal.appellant.userprofile
                    appellant_profile.rewards += appeal.stake_amount
                    appellant_profile.save()

                    RewardLedger.objects.create(
                        user=appeal.appellant,
                        task=dispute.task,
                        amount=appeal.stake_amount,
                        transaction_type='appeal_refund',
                        description=f"Appeal stake bond refunded for successful appeal on task: '{dispute.task.title}'"
                    )
                final_verdict = tier2_winner

            dispute.final_verdict = final_verdict
            dispute.status = 'resolved'
            dispute.save()

            DisputeService._settle_task_escrow(dispute, final_verdict)
            DisputeService._slash_nonconforming_jurors(dispute, final_verdict)

    @staticmethod
    def finalize_uncontested_dispute(dispute):
        with transaction.atomic():
            final_verdict = dispute.primary_verdict or 'poster'
            dispute.final_verdict = final_verdict
            dispute.status = 'resolved'
            dispute.save()

            DisputeService._settle_task_escrow(dispute, final_verdict)
            DisputeService._slash_nonconforming_jurors(dispute, final_verdict)

    @staticmethod
    def _settle_task_escrow(dispute, final_verdict):
        task = dispute.task
        if final_verdict == 'poster':
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            task.status = 'cancelled'
            task.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for dispute resolution on task: '{task.title}'"
            )

            if dispute.escrow_status == 'held':
                dispute.forfeit_deposit(
                    beneficiary=task.posted_by,
                    reason_description=f"Deposit bond forfeited to poster on dispute resolution for task '{task.title}'"
                )
        else:
            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Task reward awarded on dispute resolution for task: '{task.title}'"
                )

            task.status = 'completed'
            task.save()

            if dispute.escrow_status == 'held':
                dispute.refund_deposit(
                    reason_description=f"Deposit bond refunded on dispute resolution for task '{task.title}'"
                )

    @staticmethod
    def _slash_nonconforming_jurors(dispute, final_verdict):
        nonconforming_assignments = JurorAssignment.objects.filter(
            dispute=dispute,
            vote__isnull=False
        ).exclude(vote=final_verdict)

        for assignment in nonconforming_assignments:
            juror = assignment.juror
            juror_profile = juror.userprofile

            SLASH_PENALTY = 20
            actual_slash = min(SLASH_PENALTY, juror_profile.rewards)

            if actual_slash > 0:
                juror_profile.rewards -= actual_slash
                juror_profile.save()

            RewardLedger.objects.create(
                user=juror,
                task=dispute.task,
                amount=-actual_slash,
                transaction_type='juror_slashing',
                description=f"Point slashing penalty for non-conforming juror vote on task: '{dispute.task.title}'"
            )
