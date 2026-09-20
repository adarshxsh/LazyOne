from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification

class Command(BaseCommand):
    help = 'Resolves expired open disputes, refunds/forfeits escrowed bonds, and settles task points.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days after dispute creation before considering it expired (default: 7)'
        )

    def handle(self, *args, **options):
        days = options['days']
        now = timezone.now()
        expiry_threshold = now - timedelta(days=days)

        # Find open disputes created before the expiration window
        expired_disputes = Dispute.objects.filter(status='open', created_at__lte=expiry_threshold)

        count = 0
        for dispute in expired_disputes:
            task = dispute.task
            tally = dispute.get_weighted_tally()
            w_poster = tally['poster_weight']
            w_taker = tally['taker_weight']

            with transaction.atomic():
                if w_poster > w_taker:
                    # Leading vote for Poster
                    if dispute.raised_by == task.posted_by:
                        if dispute.escrow_status == 'held':
                            dispute.refund_deposit(reason_description=f"Deposit bond refunded on expired dispute for task '{task.title}'")
                    else:
                        if dispute.escrow_status == 'held':
                            dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Deposit bond forfeited on expired dispute for task '{task.title}'")

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
                        description=f"Refund for expired dispute on task: '{task.title}'"
                    )

                    # Micro-rewards for poster jurors
                    winning_votes = dispute.votes.filter(vote_choice='poster')
                    for vote in winning_votes:
                        juror_profile = vote.voter.userprofile
                        juror_profile.rewards += 10
                        juror_profile.save()
                        RewardLedger.objects.create(
                            user=vote.voter,
                            task=task,
                            amount=10,
                            transaction_type='juror_reward',
                            description=f"Jury voting reward for expired dispute resolution on task: '{task.title}'"
                        )

                elif w_taker > w_poster:
                    # Leading vote for Taker
                    if dispute.raised_by == task.taken_by:
                        if dispute.escrow_status == 'held':
                            dispute.refund_deposit(reason_description=f"Deposit bond refunded on expired dispute for task '{task.title}'")
                    else:
                        if dispute.escrow_status == 'held':
                            dispute.forfeit_deposit(beneficiary=task.taken_by, reason_description=f"Deposit bond forfeited on expired dispute for task '{task.title}'")

                    if task.taken_by:
                        taker_profile = task.taken_by.userprofile
                        taker_profile.rewards += task.reward
                        taker_profile.save()

                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_completion',
                            description=f"Awarded reward for expired dispute on task: '{task.title}'"
                        )

                    task.status = 'completed'
                    task.save()

                    # Micro-rewards for taker jurors
                    winning_votes = dispute.votes.filter(vote_choice='taker')
                    for vote in winning_votes:
                        juror_profile = vote.voter.userprofile
                        juror_profile.rewards += 10
                        juror_profile.save()
                        RewardLedger.objects.create(
                            user=vote.voter,
                            task=task,
                            amount=10,
                            transaction_type='juror_reward',
                            description=f"Jury voting reward for expired dispute resolution on task: '{task.title}'"
                        )

                else:
                    # Fallback to default resolution
                    if dispute.raised_by == task.posted_by:
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
                            description=f"Refund for expired dispute on task: '{task.title}'"
                        )

                        if dispute.escrow_status == 'held':
                            dispute.refund_deposit(reason_description=f"Deposit bond refunded on auto-resolved dispute for task '{task.title}'")
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
                                description=f"Awarded reward for auto-resolved expired dispute on task: '{task.title}'"
                            )
                        task.status = 'completed'
                        task.save()

                        if dispute.escrow_status == 'held':
                            dispute.refund_deposit(reason_description=f"Deposit bond refunded on auto-resolved dispute for task '{task.title}'")

                dispute.status = 'resolved'
                dispute.save()

                # Notify participants
                participants = [task.posted_by]
                if task.taken_by and task.taken_by not in participants:
                    participants.append(task.taken_by)

                dispute_link = reverse('dispute_detail', args=[dispute.id])
                for participant in participants:
                    Notification.objects.create(
                        recipient=participant,
                        message=f"Dispute for task '{task.title}' has expired ({days}d SLA) and was automatically resolved.",
                        link=dispute_link
                    )

                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))
