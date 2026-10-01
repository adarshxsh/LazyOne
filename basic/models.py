import math
from django.db import models, transaction
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
        ('juror_stake', 'Juror Stake Locked'),
        ('juror_reward', 'Juror Reward Awarded'),
        ('juror_slash', 'Juror Stake Forfeited'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reward_transactions')
    task = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True)
    amount = models.IntegerField()
    transaction_type = models.CharField(max_length=50, choices=TRANSACTION_TYPES)
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
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='raised_disputes')
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    deposit_amount = models.PositiveIntegerField(default=0)
    escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='held')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    def refund_deposit(self, reason_description=None):
        if self.escrow_status == 'held' and self.deposit_amount > 0:
            desc = reason_description or f"Security deposit bond refunded for dispute on task: '{self.task.title}'"

            # Refund worker
            if self.task.taken_by:
                worker_profile = self.task.taken_by.userprofile
                worker_profile.rewards += self.deposit_amount
                worker_profile.save()
                RewardLedger.objects.create(
                    user=self.task.taken_by,
                    task=self.task,
                    amount=self.deposit_amount,
                    transaction_type='dispute_refund',
                    description=desc
                )

            # Refund poster
            poster_profile = self.task.posted_by.userprofile
            poster_profile.rewards += self.deposit_amount
            poster_profile.save()
            RewardLedger.objects.create(
                user=self.task.posted_by,
                task=self.task,
                amount=self.deposit_amount,
                transaction_type='dispute_refund',
                description=desc
            )

            self.escrow_status = 'refunded'
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

    def resolve(self, winner_choice):
        """
        Resolves the dispute for winner_choice in ['poster', 'worker'].
        """
        if self.status == 'resolved':
            return

        with transaction.atomic():
            task = self.task
            worker = task.taken_by
            poster = task.posted_by
            deposit = self.deposit_amount

            winner_user = poster if winner_choice == 'poster' else worker
            loser_user = worker if winner_choice == 'poster' else poster

            # 1. Refund winner's deposit bond
            if winner_user:
                winner_profile = winner_user.userprofile
                winner_profile.rewards += deposit
                winner_profile.save()
                RewardLedger.objects.create(
                    user=winner_user,
                    task=task,
                    amount=deposit,
                    transaction_type='dispute_refund',
                    description=f"Security deposit bond refunded for winning dispute on task: '{task.title}'"
                )

            # 2. Settle Task Reward
            if winner_choice == 'worker':
                if worker:
                    worker_profile = worker.userprofile
                    worker_profile.rewards += task.reward
                    worker_profile.save()
                    RewardLedger.objects.create(
                        user=worker,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Awarded task reward for winning dispute on task: '{task.title}'"
                    )
                task.status = 'completed'
            else:
                poster_profile = poster.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=poster,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Refunded task reward for winning dispute on task: '{task.title}'"
                )
                task.status = 'cancelled'
            task.save()

            # 3. Forfeit loser's deposit bond
            if loser_user:
                RewardLedger.objects.create(
                    user=loser_user,
                    task=task,
                    amount=0,
                    transaction_type='dispute_forfeit',
                    description=f"Security deposit bond forfeited for dispute on task: '{task.title}'"
                )

            # 4. Juror Rewards Distribution
            winning_votes = list(self.votes.filter(vote=winner_choice))
            losing_votes = list(self.votes.exclude(vote=winner_choice))
            num_winning_jurors = len(winning_votes)

            if num_winning_jurors > 0:
                share = math.floor(deposit / num_winning_jurors)
                for vote in winning_votes:
                    juror_profile = vote.voter.userprofile
                    total_payout = vote.stake_amount + share
                    juror_profile.rewards += total_payout
                    juror_profile.save()
                    RewardLedger.objects.create(
                        user=vote.voter,
                        task=task,
                        amount=total_payout,
                        transaction_type='juror_reward',
                        description=f"Juror stake refund ({vote.stake_amount} pts) plus reward share ({share} pts) for dispute on task: '{task.title}'"
                    )
            elif winner_user:
                # If no winning jurors, give forfeited bond to winning litigant
                winner_profile = winner_user.userprofile
                winner_profile.rewards += deposit
                winner_profile.save()
                RewardLedger.objects.create(
                    user=winner_user,
                    task=task,
                    amount=deposit,
                    transaction_type='dispute_refund',
                    description=f"Awarded forfeited loser deposit bond from dispute on task: '{task.title}'"
                )

            for vote in losing_votes:
                RewardLedger.objects.create(
                    user=vote.voter,
                    task=task,
                    amount=0,
                    transaction_type='juror_slash',
                    description=f"Juror stake forfeited for minority vote on dispute for task: '{task.title}'"
                )

            self.status = 'resolved'
            self.escrow_status = 'forfeited'
            self.save()

class DisputeVote(models.Model):
    VOTE_CHOICES = (
        ('poster', 'Poster'),
        ('worker', 'Worker'),
    )
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    voter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes')
    vote = models.CharField(max_length=10, choices=VOTE_CHOICES)
    voted_for = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes_received', null=True, blank=True)
    stake_amount = models.PositiveIntegerField(default=10)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'voter')

    def __str__(self):
        return f"Vote by {self.voter.username} on {self.dispute.task.title}: {self.vote}"

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
