from django.db import models, transaction
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.exceptions import ValidationError
from datetime import timedelta

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

class RewardLedger(models.Model):
    TRANSACTION_TYPES = (
        ('task_creation', 'Task Creation (Points Reserved)'),
        ('task_completion', 'Task Completion (Points Awarded)'),
        ('task_cancellation', 'Task Cancellation (Points Refunded)'),
        ('initial_points', 'Initial Points'),
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
    STATUS_INITIATED = 'initiated'
    STATUS_EVIDENCE_SUBMISSION = 'evidence_submission'
    STATUS_VOTING = 'voting'
    STATUS_APPEALED = 'appealed'
    STATUS_RESOLVED = 'resolved'
    STATUS_CANCELLED = 'cancelled'

    STATUS_CHOICES = (
        (STATUS_INITIATED, 'Initiated'),
        (STATUS_EVIDENCE_SUBMISSION, 'Evidence Submission'),
        (STATUS_VOTING, 'Voting'),
        (STATUS_APPEALED, 'Appealed'),
        (STATUS_RESOLVED, 'Resolved'),
        (STATUS_CANCELLED, 'Cancelled'),
    )

    VALID_TRANSITIONS = {
        STATUS_INITIATED: {STATUS_EVIDENCE_SUBMISSION, STATUS_CANCELLED},
        STATUS_EVIDENCE_SUBMISSION: {STATUS_VOTING, STATUS_RESOLVED, STATUS_CANCELLED},
        STATUS_VOTING: {STATUS_APPEALED, STATUS_RESOLVED, STATUS_CANCELLED},
        STATUS_APPEALED: {STATUS_VOTING, STATUS_EVIDENCE_SUBMISSION, STATUS_RESOLVED, STATUS_CANCELLED},
        STATUS_RESOLVED: {STATUS_APPEALED},
        STATUS_CANCELLED: set(),
    }

    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='raised_disputes')
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_INITIATED)
    created_at = models.DateTimeField(auto_now_add=True)

    evidence_deadline = models.DateTimeField(null=True, blank=True)
    voting_deadline = models.DateTimeField(null=True, blank=True)
    appeal_deadline = models.DateTimeField(null=True, blank=True)

    def can_transition_to(self, target_status):
        allowed = self.VALID_TRANSITIONS.get(self.status, set())
        return target_status in allowed

    def validate_transition(self, target_status):
        if not self.can_transition_to(target_status):
            raise ValidationError(
                f"Cannot transition dispute from '{self.status}' to '{target_status}'."
            )

    def transition_to(self, target_status, save=True, deadline=None):
        self.validate_transition(target_status)
        now = timezone.now()
        with transaction.atomic():
            self.status = target_status
            if target_status == self.STATUS_EVIDENCE_SUBMISSION:
                self.evidence_deadline = deadline or (now + timedelta(days=1))
            elif target_status == self.STATUS_VOTING:
                self.voting_deadline = deadline or (now + timedelta(days=2))
            elif target_status == self.STATUS_APPEALED:
                self.appeal_deadline = deadline or (now + timedelta(days=1))
            elif target_status == self.STATUS_RESOLVED:
                if not self.appeal_deadline:
                    self.appeal_deadline = deadline or (now + timedelta(days=1))

            if save:
                super().save()

    def save(self, *args, **kwargs):
        if self.pk:
            orig = Dispute.objects.filter(pk=self.pk).values('status').first()
            if orig and orig['status'] != self.status:
                allowed = self.VALID_TRANSITIONS.get(orig['status'], set())
                if self.status not in allowed:
                    raise ValidationError(
                        f"Cannot transition dispute from '{orig['status']}' to '{self.status}'."
                    )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

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
