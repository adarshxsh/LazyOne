from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from basic.models import Dispute, RewardLedger, Notification

class Command(BaseCommand):
    help = 'Resolves expired open disputes based on juror vote quorum or fallback rules, refunds/forfeits escrowed bonds, and settles task points.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Fallback number of days for legacy disputes without explicit voting deadline (default: 7)'
        )

    def handle(self, *args, **options):
        days = options['days']
        now = timezone.now()
        fallback_threshold = now - timedelta(days=days)

        # Find open disputes where voting_deadline has passed or legacy created_at passed threshold
        expired_disputes = Dispute.objects.filter(
            status='open'
        ).filter(
            Q(voting_deadline__lte=now) | Q(voting_deadline__isnull=True, created_at__lte=fallback_threshold)
        )

        count = 0
        for dispute in expired_disputes:
            task = dispute.task
            with transaction.atomic():
                vote_summary = dispute.vote_summary
                poster_votes = vote_summary['poster_votes']
                taker_votes = vote_summary['taker_votes']

                if poster_votes > taker_votes:
                    # Poster wins by majority
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
                        description=f"Task reward refunded via juror majority vote for dispute on: '{task.title}'"
                    )

                    if dispute.raised_by == task.taken_by:
                        dispute.forfeit_deposit(
                            beneficiary=task.posted_by,
                            reason_description=f"Deposit bond forfeited to poster after losing juror vote on: '{task.title}'"
                        )
                    else:
                        dispute.refund_deposit(
                            reason_description=f"Deposit bond refunded to poster after winning juror vote on: '{task.title}'"
                        )

                    resolution_msg = f"Dispute for task '{task.title}' was resolved in favor of Poster by juror majority vote ({poster_votes}-{taker_votes})."

                elif taker_votes > poster_votes:
                    # Taker wins by majority
                    if task.taken_by:
                        taker_profile = task.taken_by.userprofile
                        taker_profile.rewards += task.reward
                        taker_profile.save()

                        RewardLedger.objects.create(
                            user=task.taken_by,
                            task=task,
                            amount=task.reward,
                            transaction_type='task_completion',
                            description=f"Awarded task reward via juror majority vote for dispute on: '{task.title}'"
                        )

                    task.status = 'completed'
                    task.save()

                    if dispute.raised_by == task.posted_by:
                        dispute.forfeit_deposit(
                            beneficiary=task.taken_by,
                            reason_description=f"Deposit bond forfeited to taker after losing juror vote on: '{task.title}'"
                        )
                    else:
                        dispute.refund_deposit(
                            reason_description=f"Deposit bond refunded to taker after winning juror vote on: '{task.title}'"
                        )

                    resolution_msg = f"Dispute for task '{task.title}' was resolved in favor of Taker by juror majority vote ({taker_votes}-{poster_votes})."

                else:
                    # Fallback when votes are tied or no votes cast (quorum unmet)
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
                            description=f"Refund for expired dispute (quorum timeout fallback) on task: '{task.title}'"
                        )

                        dispute.refund_deposit(
                            reason_description=f"Deposit bond refunded on timeout fallback for task: '{task.title}'"
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
                                description=f"Awarded reward on expired dispute (quorum timeout fallback) for task: '{task.title}'"
                            )

                        task.status = 'completed'
                        task.save()

                        dispute.refund_deposit(
                            reason_description=f"Deposit bond refunded on timeout fallback for task: '{task.title}'"
                        )

                    resolution_msg = f"Dispute voting deadline for task '{task.title}' expired without a clear quorum. Fallback resolution was applied."

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
                        message=resolution_msg,
                        link=dispute_link
                    )

                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} expired dispute(s)."))

