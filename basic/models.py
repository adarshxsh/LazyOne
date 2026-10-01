import math
from datetime import timedelta
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
        ('dispute_counter_deposit', 'Dispute Counter Deposit Bond Held'),
        ('dispute_refund', 'Dispute Deposit Bond Refunded'),
        ('dispute_forfeit', 'Dispute Deposit Bond Forfeited'),
        ('juror_stake', 'Juror Stake Locked'),
        ('juror_reward', 'Juror Reward Awarded'),
        ('juror_slash', 'Juror Stake Slashed'),
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
        ('pending', 'Pending Counter-Deposit'),
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

    worker_deposit_amount = models.PositiveIntegerField(default=0)
    worker_escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='pending')
    poster_deposit_amount = models.PositiveIntegerField(default=0)
    poster_escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='pending')
    counter_bond_deadline = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    @property
    def has_counter_deposit(self):
        return (self.worker_escrow_status == 'held') and (self.poster_escrow_status == 'held')

    @property
    def counter_party(self):
        if self.raised_by == self.task.posted_by:
            return self.task.taken_by
        return self.task.posted_by

    @property
    def is_counter_bond_expired(self):
        if self.has_counter_deposit:
            return False
        deadline = self.counter_bond_deadline or (self.created_at + timedelta(hours=48))
        return timezone.now() >= deadline

    def can_vote(self, user):
        if not user.is_authenticated:
            return False
        if self.status != 'open' or not self.has_counter_deposit:
            return False
        if user == self.task.posted_by or user == self.task.taken_by:
            return False
        if DisputeVote.objects.filter(dispute=self, voter=user).exists():
            return False
        if self.votes.count() >= 11:
            return False
        if not hasattr(user, 'userprofile') or user.userprofile.rewards < 20:
            return False
        return True

    def refund_worker_deposit(self, reason_description=None):
        amt = self.worker_deposit_amount if self.worker_deposit_amount > 0 else (self.deposit_amount if self.raised_by != self.task.posted_by else 0)
        esc = self.worker_escrow_status if self.worker_deposit_amount > 0 else self.escrow_status
        if esc == 'held' and amt > 0:
            if self.task.taken_by:
                taker_profile = self.task.taken_by.userprofile
                taker_profile.rewards += amt
                taker_profile.save()

                desc = reason_description or f"Security deposit bond refunded for dispute on task: '{self.task.title}'"
                RewardLedger.objects.create(
                    user=self.task.taken_by,
                    task=self.task,
                    amount=amt,
                    transaction_type='dispute_refund',
                    description=desc
                )
            self.worker_escrow_status = 'refunded'
            self.escrow_status = 'refunded'
            self.save()

    def refund_poster_deposit(self, reason_description=None):
        amt = self.poster_deposit_amount if self.poster_deposit_amount > 0 else (self.deposit_amount if self.raised_by == self.task.posted_by else 0)
        esc = self.poster_escrow_status if self.poster_deposit_amount > 0 else self.escrow_status
        if esc == 'held' and amt > 0:
            poster_profile = self.task.posted_by.userprofile
            poster_profile.rewards += amt
            poster_profile.save()

            desc = reason_description or f"Security deposit bond refunded for dispute on task: '{self.task.title}'"
            RewardLedger.objects.create(
                user=self.task.posted_by,
                task=self.task,
                amount=amt,
                transaction_type='dispute_refund',
                description=desc
            )
            self.poster_escrow_status = 'refunded'
            self.escrow_status = 'refunded'
            self.save()

    def forfeit_worker_deposit(self, beneficiary=None, reason_description=None):
        amt = self.worker_deposit_amount if self.worker_deposit_amount > 0 else (self.deposit_amount if self.raised_by != self.task.posted_by else 0)
        esc = self.worker_escrow_status if self.worker_deposit_amount > 0 else self.escrow_status
        if esc == 'held' and amt > 0:
            if beneficiary:
                beneficiary_profile = beneficiary.userprofile
                beneficiary_profile.rewards += amt
                beneficiary_profile.save()
                RewardLedger.objects.create(
                    user=beneficiary,
                    task=self.task,
                    amount=amt,
                    transaction_type='dispute_forfeit',
                    description=reason_description or f"Forfeited worker deposit bond awarded from task: '{self.task.title}'"
                )

            if self.task.taken_by:
                desc = reason_description or f"Worker security deposit bond forfeited for dispute on task: '{self.task.title}'"
                RewardLedger.objects.create(
                    user=self.task.taken_by,
                    task=self.task,
                    amount=0,
                    transaction_type='dispute_forfeit',
                    description=desc
                )
            self.worker_escrow_status = 'forfeited'
            self.escrow_status = 'forfeited'
            self.save()

    def forfeit_poster_deposit(self, beneficiary=None, reason_description=None):
        amt = self.poster_deposit_amount if self.poster_deposit_amount > 0 else (self.deposit_amount if self.raised_by == self.task.posted_by else 0)
        esc = self.poster_escrow_status if self.poster_deposit_amount > 0 else self.escrow_status
        if esc == 'held' and amt > 0:
            if beneficiary:
                beneficiary_profile = beneficiary.userprofile
                beneficiary_profile.rewards += amt
                beneficiary_profile.save()
                RewardLedger.objects.create(
                    user=beneficiary,
                    task=self.task,
                    amount=amt,
                    transaction_type='dispute_forfeit',
                    description=reason_description or f"Forfeited poster deposit bond awarded from task: '{self.task.title}'"
                )

            desc = reason_description or f"Poster security deposit bond forfeited for dispute on task: '{self.task.title}'"
            RewardLedger.objects.create(
                user=self.task.posted_by,
                task=self.task,
                amount=0,
                transaction_type='dispute_forfeit',
                description=desc
            )
            self.poster_escrow_status = 'forfeited'
            self.escrow_status = 'forfeited'
            self.save()

    def refund_deposit(self, reason_description=None):
        if self.raised_by == self.task.posted_by:
            self.refund_poster_deposit(reason_description)
        else:
            self.refund_worker_deposit(reason_description)

    def forfeit_deposit(self, beneficiary=None, reason_description=None):
        if self.raised_by == self.task.posted_by:
            self.forfeit_poster_deposit(beneficiary, reason_description)
        else:
            self.forfeit_worker_deposit(beneficiary, reason_description)

class DisputeVote(models.Model):
    CHOICE_CHOICES = (
        ('poster', 'Poster'),
        ('taker', 'Taker'),
    )
    STATUS_CHOICES = (
        ('staked', 'Staked'),
        ('rewarded', 'Rewarded'),
        ('slashed', 'Slashed'),
    )
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    voter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes')
    choice = models.CharField(max_length=10, choices=CHOICE_CHOICES)
    stake = models.PositiveIntegerField(default=20)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='staked')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'voter')

    def __str__(self):
        return f"Vote by {self.voter.username} on Dispute #{self.dispute.id}: {self.choice}"

    @property
    def juror(self):
        return self.voter

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
