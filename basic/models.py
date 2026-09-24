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
    reputation_score = models.IntegerField(default=100)
    disputes_won = models.IntegerField(default=0)
    disputes_lost = models.IntegerField(default=0)
    tasks_completed = models.IntegerField(default=0)
    tasks_cancelled = models.IntegerField(default=0)
    tasks_abandoned = models.IntegerField(default=0)
    locked_collateral = models.IntegerField(default=0)
    phone_number = models.CharField(max_length=20, blank=True)
    is_phone_verified = models.BooleanField(default=False)
    instagram_username = models.CharField(max_length=100, blank=True)
    is_instagram_verified = models.BooleanField(default=False)
    
    # Fields for Email OTP Verification
    email_otp = models.CharField(max_length=6, blank=True, null=True)
    email_otp_created_at = models.DateTimeField(blank=True, null=True)

    def calculate_risk_tier(self):
        if self.reputation_score < 50 or self.disputes_lost >= 2 or (self.tasks_cancelled >= 2 and self.tasks_cancelled > self.tasks_completed):
            return 'HIGH'
        elif self.reputation_score < 80 or self.disputes_lost == 1 or self.tasks_cancelled > 0:
            return 'MEDIUM'
        return 'LOW'

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
        ('juror_stake_lock', 'Juror Stake Locked'),
        ('juror_stake_refunded', 'Juror Stake Refunded'),
        ('juror_stake_slash', 'Juror Stake Slashed'),
        ('juror_reward_payout', 'Juror Reward Payout'),
        ('dispute_appeal_bond', 'Dispute Appeal Stake Bond Held'),
        ('dispute_appeal_refund', 'Dispute Appeal Stake Bond Refunded'),
        ('dispute_appeal_forfeit', 'Dispute Appeal Stake Bond Forfeited'),
        ('juror_slashing', 'Dishonest Juror Slashing Penalty'),
        ('slash_penalty', 'Slash Penalty'),
        ('appeal_fee', 'Appeal Fee'),
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
        ('evidence_submission', 'Evidence Submission'),
        ('voting', 'Voting'),
        ('under_review', 'Under Review'),
        ('appealed', 'Appealed'),
        ('under_appeal', 'Under Appeal'),
        ('resolved', 'Resolved'),
        ('appeal_upheld', 'Appeal Upheld'),
        ('appeal_reversed', 'Appeal Reversed'),
        ('withdrawn', 'Withdrawn'),
        ('slashed', 'Slashed'),
        ('staff_review', 'Staff Review'),
    )
    ESCROW_STATUS_CHOICES = (
        ('held', 'Held in Escrow'),
        ('refunded', 'Refunded'),
        ('forfeited', 'Forfeited'),
    )
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='raised_disputes')
    reason = models.TextField()
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='open')
    deposit_amount = models.PositiveIntegerField(default=0)
    escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='held')
    
    tier = models.IntegerField(default=1)
    appealed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='appeals_raised')
    appeal_reason = models.TextField(blank=True, default='')
    appealed_at = models.DateTimeField(null=True, blank=True)
    appeal_bond_amount = models.PositiveIntegerField(default=0)
    appeal_escrow_status = models.CharField(
        max_length=20,
        choices=[('none', 'None'), ('held', 'Held in Escrow'), ('refunded', 'Refunded'), ('forfeited', 'Forfeited')],
        default='none'
    )
    winner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='won_disputes')
    consensus_outcome = models.CharField(max_length=20, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    def is_appealable(self):
        return self.status in ['resolved', 'appealable'] and self.tier == 1 and self.appealed_by is None

    def can_withdraw(self):
        return self.status in ['open', 'voting'] and self.task.status == 'disputed'

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

class JurorAssignment(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending Vote'),
        ('voted', 'Voted'),
        ('slashed', 'Slashed'),
        ('rewarded', 'Rewarded'),
        ('refunded', 'Refunded'),
    )
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='juror_assignments')
    juror = models.ForeignKey(User, on_delete=models.CASCADE, related_name='juror_assignments')
    tier = models.IntegerField(default=1)
    stake_amount = models.PositiveIntegerField(default=50)
    has_voted = models.BooleanField(default=False)
    vote = models.CharField(max_length=20, blank=True, default='')
    voting_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'juror')

    def __str__(self):
        return f"Juror {self.juror.username} for dispute #{self.dispute.id} (Tier {self.tier})"

    @property
    def user(self):
        return self.juror

class DisputeAuditEvent(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='audit_events')
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    event_type = models.CharField(max_length=50)
    details_json = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f"AuditEvent {self.event_type} on dispute #{self.dispute.id}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("DisputeAuditEvent records are immutable and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("DisputeAuditEvent records are immutable and cannot be deleted.")

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
