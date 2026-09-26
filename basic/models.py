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
        ('dispute_poster_deposit', 'Poster Dispute Deposit Bond Held'),
        ('juror_stake', 'Juror Stake Held'),
        ('juror_stake_refund', 'Juror Stake Refunded'),
        ('juror_slash', 'Juror Stake Slashed'),
        ('juror_reward', 'Consensus Jury Reward Distributed'),
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
        ('pending', 'Pending Counter-Bond'),
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
    
    # Symmetrical Dispute Deposit Bond Fields
    worker_deposit_amount = models.PositiveIntegerField(default=0)
    worker_escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='held')
    poster_deposit_amount = models.PositiveIntegerField(default=0)
    poster_escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='pending')
    
    counter_bond_deadline = models.DateTimeField(null=True, blank=True)
    voting_deadline = models.DateTimeField(null=True, blank=True)
    consensus_outcome = models.CharField(max_length=20, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    def is_counter_bond_overdue(self):
        if self.status == 'open' and self.poster_escrow_status == 'pending' and self.counter_bond_deadline:
            return timezone.now() > self.counter_bond_deadline
        return False

    def check_counter_bond_sla(self):
        from django.db import transaction
        from django.urls import reverse
        if self.is_counter_bond_overdue():
            with transaction.atomic():
                task = self.task
                # Default resolution in favor of worker (taker)
                if self.worker_escrow_status == 'held' and self.worker_deposit_amount > 0 and task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    # Refund worker deposit
                    taker_profile.rewards += self.worker_deposit_amount
                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=self.worker_deposit_amount,
                        transaction_type='dispute_refund',
                        description=f"Security deposit bond refunded on default win for task: '{task.title}'"
                    )
                    self.worker_escrow_status = 'refunded'
                    self.escrow_status = 'refunded'

                    # Pay task reward to worker
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Task reward awarded on default dispute win (poster counter-bond expired) for task: '{task.title}'"
                    )

                task.status = 'completed'
                task.save()

                self.status = 'resolved'
                self.consensus_outcome = 'taker'
                self.save()

                # Notify participants
                dispute_link = reverse('dispute_detail', args=[self.id])
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Dispute for task '{task.title}' defaulted in favor of worker due to expired counter-bond SLA.",
                    link=dispute_link
                )
                if task.taken_by:
                    Notification.objects.create(
                        recipient=task.taken_by,
                        message=f"Dispute for task '{task.title}' defaulted in your favor because poster failed to match deposit bond.",
                        link=dispute_link
                    )
            return True
        return False

    def refund_deposit(self, reason_description=None):
        if self.worker_escrow_status == 'held' and self.worker_deposit_amount > 0:
            user_profile = self.raised_by.userprofile
            user_profile.rewards += self.worker_deposit_amount
            user_profile.save()

            desc = reason_description or f"Security deposit bond refunded for dispute on task: '{self.task.title}'"
            RewardLedger.objects.create(
                user=self.raised_by,
                task=self.task,
                amount=self.worker_deposit_amount,
                transaction_type='dispute_refund',
                description=desc
            )
            self.worker_escrow_status = 'refunded'
            self.escrow_status = 'refunded'
            self.save()
        elif self.escrow_status == 'held' and self.deposit_amount > 0:
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
            self.save()

    def forfeit_deposit(self, beneficiary=None, reason_description=None):
        amt = self.worker_deposit_amount if self.worker_deposit_amount > 0 else self.deposit_amount
        if (self.worker_escrow_status == 'held' or self.escrow_status == 'held') and amt > 0:
            if beneficiary:
                beneficiary_profile = beneficiary.userprofile
                beneficiary_profile.rewards += amt
                beneficiary_profile.save()
                RewardLedger.objects.create(
                    user=beneficiary,
                    task=self.task,
                    amount=amt,
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
            self.worker_escrow_status = 'forfeited'
            self.escrow_status = 'forfeited'
            self.save()

class JurorAssignment(models.Model):
    STAKE_STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('held', 'Held in Escrow'),
        ('refunded', 'Refunded'),
        ('slashed', 'Slashed'),
    )
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='juror_assignments')
    juror = models.ForeignKey(User, on_delete=models.CASCADE, related_name='juror_assignments')
    assigned_at = models.DateTimeField(auto_now_add=True)
    has_voted = models.BooleanField(default=False)
    vote_choice = models.CharField(max_length=20, blank=True, null=True)
    stake_amount = models.PositiveIntegerField(default=25)
    stake_status = models.CharField(max_length=20, choices=STAKE_STATUS_CHOICES, default='pending')
    voted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('dispute', 'juror')

    def __str__(self):
        return f"Juror {self.juror.username} for Dispute {self.dispute.id}"

    @property
    def user(self):
        return self.juror

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
