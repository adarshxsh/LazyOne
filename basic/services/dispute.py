from django.db import transaction
from django.utils import timezone
from django.urls import reverse
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from ..models import Dispute, Task, RewardLedger, Notification, JurorAssignment, DisputeAuditEvent
from ..jury import select_jurors_for_dispute


class DisputeService:
    @staticmethod
    def raise_dispute(task, user, reason):
        if task.status != 'in_progress':
            raise ValidationError("Disputes can only be raised for tasks currently in progress.")
        if user not in [task.posted_by, task.taken_by]:
            raise ValidationError("Only task participants can raise a dispute.")

        deposit_amount = task.deposit_bond_amount
        user_profile = user.userprofile

        if user_profile.rewards < deposit_amount:
            raise ValidationError(
                f"Insufficient reward points. You need at least {deposit_amount} points as a deposit bond."
            )

        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = user
                dispute.reason = reason
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.status = 'voting'
                dispute.tier = 1
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    status='voting',
                    tier=1
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

            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=user,
                event_type='dispute_raised',
                details_json={'reason': reason, 'deposit_amount': deposit_amount}
            )

            # Assign Tier 1 Jurors
            select_jurors_for_dispute(dispute, panel_size=3, tier=1)

            recipient = task.posted_by if user == task.taken_by else task.taken_by
            if recipient:
                Notification.objects.create(
                    recipient=recipient,
                    message=f"{user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        return dispute

    @staticmethod
    def withdraw_dispute(dispute, user):
        if dispute.raised_by != user:
            raise ValidationError("Only the user who raised the dispute can withdraw it.")
        if not dispute.can_withdraw():
            raise ValidationError("This dispute cannot be withdrawn in its current state.")

        task = dispute.task
        with transaction.atomic():
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
            )

            # Refund any pending jurors
            for assignment in dispute.juror_assignments.filter(voting_status='pending'):
                juror_profile = assignment.juror.userprofile
                juror_profile.rewards += assignment.stake_amount
                juror_profile.save()

                RewardLedger.objects.create(
                    user=assignment.juror,
                    task=task,
                    amount=assignment.stake_amount,
                    transaction_type='juror_stake_refunded',
                    description=f"Juror stake refunded due to dispute withdrawal on task: '{task.title}'"
                )
                assignment.voting_status = 'refunded'
                assignment.save()

            dispute.status = 'withdrawn'
            dispute.save()

            task.status = 'in_progress'
            task.save()

            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=user,
                event_type='dispute_withdrawn',
                details_json={}
            )

            recipient = task.posted_by if user == task.taken_by else task.taken_by
            if recipient:
                Notification.objects.create(
                    recipient=recipient,
                    message=f"{user.username} has withdrawn the dispute for '{task.title}'.",
                    link=reverse('my_tasks')
                )

        return dispute

    @staticmethod
    def cast_juror_vote(dispute, juror_user, vote_choice):
        if vote_choice not in ['poster', 'taker']:
            raise ValidationError("Invalid vote choice. Must be 'poster' or 'taker'.")

        assignment = dispute.juror_assignments.filter(
            dispute=dispute,
            juror=juror_user,
            tier=dispute.tier,
            has_voted=False
        ).first()

        if not assignment:
            raise ValidationError("You are not assigned as an active voter for this dispute tier or have already voted.")

        with transaction.atomic():
            assignment.has_voted = True
            assignment.vote = vote_choice
            assignment.voting_status = 'voted'
            assignment.save()

            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=juror_user,
                event_type='vote_cast',
                details_json={'tier': dispute.tier, 'vote': vote_choice}
            )

            tier_assignments = dispute.juror_assignments.filter(tier=dispute.tier)
            voted_assignments = tier_assignments.filter(has_voted=True)

            poster_votes = voted_assignments.filter(vote='poster').count()
            taker_votes = voted_assignments.filter(vote='taker').count()

            # Trigger resolution if majority reached (e.g. 2 votes out of 3) or all voted
            if poster_votes >= 2 or taker_votes >= 2 or voted_assignments.count() == tier_assignments.count():
                DisputeService.resolve_tier_voting(dispute, tier=dispute.tier)

        return assignment

    @staticmethod
    def resolve_tier_voting(dispute, tier):
        if tier == 1 and dispute.status not in ['open', 'voting']:
            return
        if tier == 2 and dispute.status not in ['appealed', 'under_appeal']:
            return

        task = dispute.task
        tier_assignments = dispute.juror_assignments.filter(tier=tier, has_voted=True)

        poster_votes = tier_assignments.filter(vote='poster').count()
        taker_votes = tier_assignments.filter(vote='taker').count()
        majority_choice = 'poster' if poster_votes >= taker_votes else 'taker'

        with transaction.atomic():
            if tier == 1:
                dispute.consensus_outcome = majority_choice
                if majority_choice == 'poster':
                    winner = task.posted_by
                    loser = task.taken_by
                    task.status = 'cancelled'
                    task.save()

                    # Refund poster task reward
                    poster_profile = winner.userprofile
                    poster_profile.rewards += task.reward
                    poster_profile.save()
                    RewardLedger.objects.create(
                        user=winner,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_cancellation',
                        description=f"Task reward refunded after dispute resolution on task: '{task.title}'"
                    )

                    # Forfeit dispute deposit if raised by taker
                    if dispute.raised_by == loser:
                        dispute.forfeit_deposit(beneficiary=winner)
                    elif dispute.raised_by == winner:
                        dispute.refund_deposit()
                else: # taker
                    winner = task.taken_by
                    loser = task.posted_by
                    task.status = 'completed'
                    task.save()

                    # Award taker task reward
                    taker_profile = winner.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()
                    RewardLedger.objects.create(
                        user=winner,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Task reward awarded after dispute resolution on task: '{task.title}'"
                    )

                    # Deposit handling
                    if dispute.raised_by == winner:
                        dispute.refund_deposit()
                    elif dispute.raised_by == loser:
                        dispute.forfeit_deposit(beneficiary=winner)

                dispute.winner = winner
                dispute.status = 'resolved'
                dispute.save()

                # Update stats
                if hasattr(winner, 'userprofile'):
                    winner.userprofile.disputes_won += 1
                    winner.userprofile.save()
                if loser and hasattr(loser, 'userprofile'):
                    loser.userprofile.disputes_lost += 1
                    loser.userprofile.save()

                # Reward majority Tier 1 jurors, slash minority Tier 1 jurors
                for assignment in dispute.juror_assignments.filter(tier=1):
                    if assignment.has_voted and assignment.vote == majority_choice:
                        juror_profile = assignment.juror.userprofile
                        juror_profile.rewards += assignment.stake_amount + 10 # refund + micro reward
                        juror_profile.save()

                        RewardLedger.objects.create(
                            user=assignment.juror,
                            task=task,
                            amount=assignment.stake_amount,
                            transaction_type='juror_stake_refunded',
                            description=f"Juror stake refunded for correct vote on task: '{task.title}'"
                        )
                        RewardLedger.objects.create(
                            user=assignment.juror,
                            task=task,
                            amount=10,
                            transaction_type='juror_reward_payout',
                            description=f"Juror micro-reward awarded for correct vote on task: '{task.title}'"
                        )
                        assignment.voting_status = 'rewarded'
                        assignment.save()
                    else:
                        RewardLedger.objects.create(
                            user=assignment.juror,
                            task=task,
                            amount=0,
                            transaction_type='juror_stake_slash',
                            description=f"Juror stake slashed for dissenting vote on task: '{task.title}'"
                        )
                        assignment.voting_status = 'slashed'
                        assignment.save()

                DisputeAuditEvent.objects.create(
                    dispute=dispute,
                    actor=None,
                    event_type='primary_voting_resolved',
                    details_json={'winner': winner.username if winner else '', 'outcome': majority_choice}
                )

            elif tier == 2:
                initial_outcome = dispute.consensus_outcome
                if majority_choice != initial_outcome:
                    # APPEAL OVERTURNED / REVERSED TIER 1 DECISION
                    appellant = dispute.appealed_by

                    # Refund appeal bond
                    if dispute.appeal_escrow_status == 'held' and dispute.appeal_bond_amount > 0:
                        appellant_profile = appellant.userprofile
                        appellant_profile.rewards += dispute.appeal_bond_amount
                        appellant_profile.save()

                        RewardLedger.objects.create(
                            user=appellant,
                            task=task,
                            amount=dispute.appeal_bond_amount,
                            transaction_type='dispute_appeal_refund',
                            description=f"Appeal stake bond refunded after successful appeal on task: '{task.title}'"
                        )
                        dispute.appeal_escrow_status = 'refunded'

                    # Reverse winner & task status
                    if majority_choice == 'poster':
                        new_winner = task.posted_by
                        new_loser = task.taken_by
                        task.status = 'cancelled'
                        task.save()

                        poster_profile = new_winner.userprofile
                        poster_profile.rewards += task.reward
                        poster_profile.save()
                        RewardLedger.objects.create(
                            user=new_winner,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_cancellation',
                            description=f"Task reward refunded after appeal overturn on task: '{task.title}'"
                        )
                    else: # taker
                        new_winner = task.taken_by
                        new_loser = task.posted_by
                        task.status = 'completed'
                        task.save()

                        taker_profile = new_winner.userprofile
                        taker_profile.rewards += task.reward
                        taker_profile.save()
                        RewardLedger.objects.create(
                            user=new_winner,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_completion',
                            description=f"Task reward awarded after appeal overturn on task: '{task.title}'"
                        )

                    dispute.winner = new_winner
                    dispute.status = 'appeal_reversed'
                    dispute.save()

                    # UPDATE STATS
                    if hasattr(new_winner, 'userprofile'):
                        new_winner.userprofile.disputes_won += 1
                        new_winner.userprofile.save()
                    if new_loser and hasattr(new_loser, 'userprofile'):
                        new_loser.userprofile.disputes_lost += 1
                        new_loser.userprofile.save()

                    # DISHONEST JUROR SLASHING FOR TIER 1 JURORS WHO VOTED FOR OVERTURNED OUTCOME
                    for t1_assignment in dispute.juror_assignments.filter(tier=1):
                        if t1_assignment.vote == initial_outcome:
                            # Dishonest/Colluding juror!
                            t1_juror = t1_assignment.juror
                            t1_profile = t1_juror.userprofile
                            t1_profile.reputation_score = max(0, t1_profile.reputation_score - 15)
                            t1_profile.save()

                            t1_assignment.voting_status = 'slashed'
                            t1_assignment.save()

                            RewardLedger.objects.create(
                                user=t1_juror,
                                task=task,
                                amount=0,
                                transaction_type='juror_slashing',
                                description=f"Dishonest juror slashing penalty applied for overturned decision on task: '{task.title}'"
                            )

                            Notification.objects.create(
                                recipient=t1_juror,
                                message=f"You have been penalized and slashed for participating in a dishonest/overturned decision on task '{task.title}'.",
                                link=reverse('dispute_detail', args=[dispute.id])
                            )
                        elif t1_assignment.vote == majority_choice:
                            # Correct Tier 1 juror
                            t1_assignment.voting_status = 'rewarded'
                            t1_assignment.save()

                    # Reward majority Tier 2 jurors
                    for t2_assignment in dispute.juror_assignments.filter(tier=2):
                        if t2_assignment.has_voted and t2_assignment.vote == majority_choice:
                            juror_profile = t2_assignment.juror.userprofile
                            juror_profile.rewards += t2_assignment.stake_amount + 20
                            juror_profile.save()

                            RewardLedger.objects.create(
                                user=t2_assignment.juror,
                                task=task,
                                amount=t2_assignment.stake_amount,
                                transaction_type='juror_stake_refunded',
                                description=f"Appeal juror stake refunded for correct vote on task: '{task.title}'"
                            )
                            RewardLedger.objects.create(
                                user=t2_assignment.juror,
                                task=task,
                                amount=20,
                                transaction_type='juror_reward_payout',
                                description=f"Appeal juror reward payout for correct vote on task: '{task.title}'"
                            )
                            t2_assignment.voting_status = 'rewarded'
                            t2_assignment.save()
                        else:
                            RewardLedger.objects.create(
                                user=t2_assignment.juror,
                                task=task,
                                amount=0,
                                transaction_type='juror_stake_slash',
                                description=f"Appeal juror stake slashed for dissenting vote on task: '{task.title}'"
                            )
                            t2_assignment.voting_status = 'slashed'
                            t2_assignment.save()

                    DisputeAuditEvent.objects.create(
                        dispute=dispute,
                        actor=None,
                        event_type='appeal_reversed',
                        details_json={'new_winner': new_winner.username if new_winner else '', 'outcome': majority_choice}
                    )

                else:
                    # APPEAL UPHELD TIER 1 DECISION
                    appellant = dispute.appealed_by

                    # Forfeit appeal bond
                    if dispute.appeal_escrow_status == 'held':
                        RewardLedger.objects.create(
                            user=appellant,
                            task=task,
                            amount=0,
                            transaction_type='dispute_appeal_forfeit',
                            description=f"Appeal stake bond forfeited after unsuccessful appeal on task: '{task.title}'"
                        )
                        dispute.appeal_escrow_status = 'forfeited'

                    dispute.status = 'appeal_upheld'
                    dispute.save()

                    # Reward majority Tier 2 jurors
                    for t2_assignment in dispute.juror_assignments.filter(tier=2):
                        if t2_assignment.has_voted and t2_assignment.vote == majority_choice:
                            juror_profile = t2_assignment.juror.userprofile
                            juror_profile.rewards += t2_assignment.stake_amount + 20
                            juror_profile.save()

                            RewardLedger.objects.create(
                                user=t2_assignment.juror,
                                task=task,
                                amount=t2_assignment.stake_amount,
                                transaction_type='juror_stake_refunded',
                                description=f"Appeal juror stake refunded for correct vote on task: '{task.title}'"
                            )
                            RewardLedger.objects.create(
                                user=t2_assignment.juror,
                                task=task,
                                amount=20,
                                transaction_type='juror_reward_payout',
                                description=f"Appeal juror reward payout for correct vote on task: '{task.title}'"
                            )
                            t2_assignment.voting_status = 'rewarded'
                            t2_assignment.save()
                        else:
                            RewardLedger.objects.create(
                                user=t2_assignment.juror,
                                task=task,
                                amount=0,
                                transaction_type='juror_stake_slash',
                                description=f"Appeal juror stake slashed for dissenting vote on task: '{task.title}'"
                            )
                            t2_assignment.voting_status = 'slashed'
                            t2_assignment.save()

                    DisputeAuditEvent.objects.create(
                        dispute=dispute,
                        actor=None,
                        event_type='appeal_upheld',
                        details_json={'winner': dispute.winner.username if dispute.winner else '', 'outcome': majority_choice}
                    )

    @staticmethod
    def file_appeal(dispute, user, appeal_reason):
        task = dispute.task
        if user not in [task.posted_by, task.taken_by]:
            raise ValidationError("Only task participants can file an appeal.")
        if not dispute.is_appealable():
            raise ValidationError("This dispute is not currently eligible for appeal.")

        appeal_bond_amount = max(100, dispute.deposit_amount * 2)
        user_profile = user.userprofile

        if user_profile.rewards < appeal_bond_amount:
            raise ValidationError(
                f"Insufficient reward points balance. You need at least {appeal_bond_amount} points as an appeal stake bond."
            )

        with transaction.atomic():
            user_profile.rewards -= appeal_bond_amount
            user_profile.save()

            RewardLedger.objects.create(
                user=user,
                task=task,
                amount=-appeal_bond_amount,
                transaction_type='dispute_appeal_bond',
                description=f"Appeal stake bond held for dispute on task: '{task.title}'"
            )

            dispute.appealed_by = user
            dispute.appeal_reason = appeal_reason
            dispute.appealed_at = timezone.now()
            dispute.appeal_bond_amount = appeal_bond_amount
            dispute.appeal_escrow_status = 'held'
            dispute.tier = 2
            dispute.status = 'appealed'
            dispute.save()

            task.status = 'disputed'
            task.save()

            DisputeAuditEvent.objects.create(
                dispute=dispute,
                actor=user,
                event_type='appeal_filed',
                details_json={'appeal_reason': appeal_reason, 'appeal_bond_amount': appeal_bond_amount}
            )

            # Trigger Tier 2 Juror Selection (excluding Tier 1 jurors)
            select_jurors_for_dispute(dispute, panel_size=3, tier=2)

            recipient = task.posted_by if user == task.taken_by else task.taken_by
            if recipient:
                Notification.objects.create(
                    recipient=recipient,
                    message=f"{user.username} has filed an appeal for dispute on task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        return dispute
