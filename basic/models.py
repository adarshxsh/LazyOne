import math
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

# Create your models here.
class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    bio = models.CharField(max_length=300,blank=True)
    first_name = models.CharField(max_length=50, blank=True)
    last_name = models.CharField(max_length=50, blank=True)
    college = models.CharField(max_length=100, blank=True)
    room_no = models.CharField(max_length=20, blank=True)
    major = models.CharField(max_length=100, blank=True)
    hostel = models.CharField(max_length=100, blank=True)
    roll_no = models.CharField(max_length=100, blank=True)
    batch = models.IntegerField(default=2029)
    friends = models.ManyToManyField('self', blank=True)
    rewards = models.IntegerField(default=1500)
    phone_number = models.CharField(max_length=20, blank=True)
    is_phone_verified = models.BooleanField(default=False)
    instagram_username = models.CharField(max_length=100, blank=True)
    is_instagram_verified = models.BooleanField(default=False)
    
    # Fields for Email OTP Verification
    email_otp = models.CharField(max_length=6, blank=True, null=True)
    email_otp_created_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return self.user.username

class Task(models.Model):
    STATUS_CHOICES = (
        ('available', 'Available'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('disputed', 'Disputed'),
        ('cancelled', 'Cancelled'),
    )

    title = models.CharField(max_length=200)
    description = models.TextField()
    reward = models.PositiveIntegerField()
    posted_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='posted_tasks')
    created_at = models.DateTimeField(auto_now_add=True)
    taken_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='taken_tasks')
    deadline = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='available')
    cancellation_requested = models.BooleanField(default=False)

    def __str__(self):
        return self.title

    @property
    def main_chat(self):
        return self.conversations.first()

    @property
    def deposit_bond_amount(self):
        return max(50, math.ceil(self.reward * 0.20))

class RewardLedger(models.Model):
    TRANSACTION_TYPES = (
        ('task_creation', 'Task Creation (Points Reserved)'),
        ('task_completion', 'Task Completion (Points Awarded)'),
        ('task_cancellation', 'Task Cancellation (Points Refunded)'),
        ('initial_points', 'Initial Points'),
        ('dispute_deposit', 'Dispute Deposit Bond Held'),
        ('dispute_refund', 'Dispute Deposit Bond Refunded'),
        ('dispute_forfeit', 'Dispute Deposit Bond Forfeited'),
        ('dispute_payout', 'Dispute Compensation Payout'),
        ('juror_stake', 'Juror Stake Bond Held'),
        ('juror_refund', 'Juror Stake Refunded'),
        ('juror_slash', 'Juror Stake Slashed'),
        ('juror_reward', 'Juror Reward Payout'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reward_transactions')
    task = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True)
    amount = models.IntegerField()
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username}: {self.amount} points for {self.description}"

class Dispute(models.Model):
    STATUS_CHOICES = (
        ('open', 'Open'),
        ('resolved', 'Resolved'),
    )
    ESCROW_STATUS_CHOICES = (
        ('held', 'Held in Escrow'),
        ('refunded', 'Refunded'),
        ('forfeited', 'Forfeited'),
    )
    POSTER_ESCROW_STATUS_CHOICES = (
        ('pending', 'Pending Counter-Stake'),
        ('held', 'Held in Escrow'),
        ('refunded', 'Refunded'),
        ('forfeited', 'Forfeited'),
    )
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='raised_disputes')
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    deposit_amount = models.PositiveIntegerField(default=0)
    escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='held')
    poster_deposit_amount = models.PositiveIntegerField(default=0)
    poster_escrow_status = models.CharField(max_length=20, choices=POSTER_ESCROW_STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    @property
    def juror_stake_amount(self):
        return 50

    @property
    def is_jury_review_active(self):
        return self.escrow_status == 'held' and self.poster_escrow_status == 'held'

    def refund_deposit(self, reason_description=None):
        if self.escrow_status == 'held' and self.deposit_amount > 0:
            user_profile = self.raised_by.userprofile
            user_profile.rewards += self.deposit_amount
            user_profile.save()

            desc = reason_description or f"Security deposit bond refunded for dispute on task: '{self.task.title}'"
            RewardLedger.objects.create(
                user=self.raised_by,
                task=self.task,
                amount=self.deposit_amount,
                transaction_type='dispute_refund',
                description=desc
            )
            self.escrow_status = 'refunded'

        if self.poster_escrow_status == 'held' and self.poster_deposit_amount > 0:
            poster_profile = self.task.posted_by.userprofile
            poster_profile.rewards += self.poster_deposit_amount
            poster_profile.save()

            desc = reason_description or f"Counter-stake deposit bond refunded for dispute on task: '{self.task.title}'"
            RewardLedger.objects.create(
                user=self.task.posted_by,
                task=self.task,
                amount=self.poster_deposit_amount,
                transaction_type='dispute_refund',
                description=desc
            )
            self.poster_escrow_status = 'refunded'

        for vote in self.votes.filter(status='held'):
            vote.status = 'refunded'
            vote.save()
            juror_profile = vote.voter.userprofile
            juror_profile.rewards += vote.stake_amount
            juror_profile.save()
            RewardLedger.objects.create(
                user=vote.voter,
                task=self.task,
                amount=vote.stake_amount,
                transaction_type='juror_refund',
                description=f"Juror stake bond refunded for cancelled dispute on task: '{self.task.title}'"
            )

        self.save()

    def forfeit_deposit(self, beneficiary=None, reason_description=None):
        if self.escrow_status == 'held' and self.deposit_amount > 0:
            if beneficiary:
                beneficiary_profile = beneficiary.userprofile
                beneficiary_profile.rewards += self.deposit_amount
                beneficiary_profile.save()
                RewardLedger.objects.create(
                    user=beneficiary,
                    task=self.task,
                    amount=self.deposit_amount,
                    transaction_type='dispute_refund',
                    description=f"Forfeited dispute deposit bond awarded from task: '{self.task.title}'"
                )

            desc = reason_description or f"Security deposit bond forfeited for dispute on task: '{self.task.title}'"
            RewardLedger.objects.create(
                user=self.raised_by,
                task=self.task,
                amount=0,
                transaction_type='dispute_forfeit',
                description=desc
            )
            self.escrow_status = 'forfeited'
            self.save()

    def settle(self, winner=None):
        if self.status == 'resolved':
            return

        from django.db import transaction
        with transaction.atomic():
            task = self.task
            worker = task.taken_by
            poster = task.posted_by

            if winner is None:
                worker_votes = self.votes.filter(voted_for=worker).count() if worker else 0
                poster_votes = self.votes.filter(voted_for=poster).count()
                if worker_votes > poster_votes:
                    winner = worker
                elif poster_votes > worker_votes:
                    winner = poster
                else:
                    self.refund_deposit(reason_description=f"Dispute resolved with tie/no majority for task: '{task.title}'")
                    self.status = 'resolved'
                    self.save()
                    return

            loser = poster if winner == worker else worker

            winner_deposit = self.deposit_amount if winner == worker else self.poster_deposit_amount
            loser_deposit = self.poster_deposit_amount if winner == worker else self.deposit_amount

            winner_profile = winner.userprofile
            total_payout_to_winner = 0

            if (winner == worker and self.escrow_status == 'held') or (winner == poster and self.poster_escrow_status == 'held'):
                total_payout_to_winner += winner_deposit
                RewardLedger.objects.create(
                    user=winner,
                    task=task,
                    amount=winner_deposit,
                    transaction_type='dispute_refund',
                    description=f"Security deposit bond refunded for winning dispute on task: '{task.title}'"
                )

            if (loser == worker and self.escrow_status == 'held') or (loser == poster and self.poster_escrow_status == 'held'):
                total_payout_to_winner += loser_deposit
                RewardLedger.objects.create(
                    user=winner,
                    task=task,
                    amount=loser_deposit,
                    transaction_type='dispute_payout',
                    description=f"Compensation payout awarded from losing party deposit bond on task: '{task.title}'"
                )
                if loser:
                    RewardLedger.objects.create(
                        user=loser,
                        task=task,
                        amount=0,
                        transaction_type='dispute_forfeit',
                        description=f"Security deposit bond forfeited for losing dispute on task: '{task.title}'"
                    )

            if total_payout_to_winner > 0:
                winner_profile.rewards += total_payout_to_winner
                winner_profile.save()

            if winner == worker:
                if self.escrow_status == 'held':
                    self.escrow_status = 'refunded'
                if self.poster_escrow_status == 'held':
                    self.poster_escrow_status = 'forfeited'
            else:
                if self.poster_escrow_status == 'held':
                    self.poster_escrow_status = 'refunded'
                if self.escrow_status == 'held':
                    self.escrow_status = 'forfeited'

            if winner == worker and worker:
                task.status = 'completed'
                task.save()
                worker_profile = worker.userprofile
                worker_profile.rewards += task.reward
                worker_profile.save()
                RewardLedger.objects.create(
                    user=worker,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Completed task upon dispute resolution: '{task.title}'"
                )
            elif winner == poster:
                task.status = 'cancelled'
                task.save()
                poster_profile = poster.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=poster,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Refund for cancelled task upon dispute resolution: '{task.title}'"
                )

            majority_votes = list(self.votes.filter(voted_for=winner))
            minority_votes = list(self.votes.exclude(voted_for=winner))

            slashed_pool = 0
            for vote in minority_votes:
                if vote.status == 'held':
                    vote.status = 'slashed'
                    vote.save()
                    slashed_pool += vote.stake_amount
                    RewardLedger.objects.create(
                        user=vote.voter,
                        task=task,
                        amount=0,
                        transaction_type='juror_slash',
                        description=f"Juror stake bond slashed for minority vote on dispute for task: '{task.title}'"
                    )

            maj_count = len(majority_votes)
            bonus_per_juror = (slashed_pool // maj_count) if maj_count > 0 else 0

            for vote in majority_votes:
                if vote.status == 'held':
                    vote.status = 'rewarded'
                    vote.save()
                    juror_profile = vote.voter.userprofile
                    juror_profile.rewards += (vote.stake_amount + bonus_per_juror)
                    juror_profile.save()

                    RewardLedger.objects.create(
                        user=vote.voter,
                        task=task,
                        amount=vote.stake_amount,
                        transaction_type='juror_refund',
                        description=f"Juror stake bond refunded for majority vote on dispute for task: '{task.title}'"
                    )
                    if bonus_per_juror > 0:
                        RewardLedger.objects.create(
                            user=vote.voter,
                            task=task,
                            amount=bonus_per_juror,
                            transaction_type='juror_reward',
                            description=f"Juror reward payout share from slashed stake pool on task: '{task.title}'"
                        )

            self.status = 'resolved'
            self.save()

class DisputeVote(models.Model):
    STATUS_CHOICES = (
        ('held', 'Held in Escrow'),
        ('refunded', 'Refunded'),
        ('slashed', 'Slashed'),
        ('rewarded', 'Rewarded'),
    )
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    voter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes')
    voted_for = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes_received')
    stake_amount = models.PositiveIntegerField(default=50)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='held')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'voter')

    def __str__(self):
        return f"Vote by {self.voter.username} for {self.voted_for.username} on {self.dispute}"

class FriendRequest(models.Model):
    from_user = models.ForeignKey(User, related_name='from_user', on_delete=models.CASCADE)
    to_user = models.ForeignKey(User, related_name='to_user', on_delete=models.CASCADE)
    is_accepted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"From {self.from_user} to {self.to_user}"

class Friendship(models.Model):
    from_user = models.ForeignKey(UserProfile, related_name='friendship_from_user', on_delete=models.CASCADE)
    to_user = models.ForeignKey(UserProfile, related_name='friendship_to_user', on_delete=models.CASCADE)
    closeness = models.IntegerField(default=50)

class Conversation(models.Model):
    task = models.OneToOneField(Task, on_delete=models.CASCADE, null=True, blank=True, related_name='conversation')
    participants = models.ManyToManyField(User, related_name='conversations')
    last_message_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        if self.task:
            return f"Chat for task: {self.task.title}"
        participant_names = [user.username for user in self.participants.all()]
        return f"Chat between {' and '.join(participant_names)}"

class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages')
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f"Message from {self.sender.username} in {self.conversation}"

class Notification(models.Model):
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    message = models.CharField(max_length=255)
    link = models.URLField(blank=True, null=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Notification for {self.recipient.username}: {self.message}"

    class Meta:
        ordering = ['-created_at']
