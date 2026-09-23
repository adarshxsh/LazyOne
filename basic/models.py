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
        ('evidence_submission', 'Evidence Submission'),
        ('under_review', 'Under Review'),
        ('voting', 'Voting'),
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
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    def is_participant(self, user):
        if not user or not user.is_authenticated:
            return False
        return user == self.task.posted_by or user == self.task.taken_by

    def is_eligible_voter(self, user):
        if not user or not user.is_authenticated:
            return False
        if self.is_participant(user):
            return False
        if self.status != 'voting':
            return False
        return not self.votes.filter(voter=user).exists()

    def can_transition_to(self, target_status):
        valid_transitions = {
            'open': ['evidence_submission', 'under_review', 'voting', 'resolved'],
            'evidence_submission': ['under_review', 'voting', 'resolved'],
            'under_review': ['voting', 'resolved'],
            'voting': ['resolved'],
            'resolved': [],
        }
        return target_status in valid_transitions.get(self.status, [])

    def transition_to(self, target_status):
        if self.can_transition_to(target_status):
            self.status = target_status
            self.save()
            return True
        return False

    def resolve_dispute(self, winner_user=None, reason_description=None):
        if self.status == 'resolved':
            return

        poster_votes = self.votes.filter(voted_for=self.task.posted_by).count()
        taker_votes = self.votes.filter(voted_for=self.task.taken_by).count()

        if not winner_user:
            if taker_votes > poster_votes:
                winner_user = self.task.taken_by
            elif poster_votes > taker_votes:
                winner_user = self.task.posted_by
            else:
                if self.raised_by == self.task.posted_by:
                    winner_user = self.task.posted_by
                else:
                    winner_user = self.task.taken_by

        if winner_user == self.task.taken_by:
            if self.task.taken_by:
                taker_profile = self.task.taken_by.userprofile
                taker_profile.rewards += self.task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=self.task.taken_by,
                    task=self.task,
                    amount=self.task.reward,
                    transaction_type='task_completion',
                    description=reason_description or f"Awarded task reward after dispute resolution for task: '{self.task.title}'"
                )
            self.task.status = 'completed'
            self.task.save()

            if self.escrow_status == 'held':
                self.refund_deposit(reason_description=reason_description or f"Security deposit bond refunded for resolved dispute on task: '{self.task.title}'")

        else:
            poster_profile = self.task.posted_by.userprofile
            poster_profile.rewards += self.task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=self.task.posted_by,
                task=self.task,
                amount=self.task.reward,
                transaction_type='task_cancellation',
                description=reason_description or f"Refund for task reward after dispute resolution on task: '{self.task.title}'"
            )

            self.task.status = 'cancelled'
            self.task.save()

            if self.escrow_status == 'held':
                if self.raised_by == self.task.taken_by:
                    self.forfeit_deposit(beneficiary=self.task.posted_by, reason_description=reason_description or f"Deposit bond forfeited to poster after dispute resolution on task: '{self.task.title}'")
                else:
                    self.refund_deposit(reason_description=reason_description or f"Security deposit bond refunded on resolved dispute for task '{self.task.title}'")

        self.status = 'resolved'
        self.save()

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


class DisputeEvidence(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='evidences')
    submitted_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_evidences')
    text_evidence = models.TextField(blank=True)
    file_evidence = models.FileField(upload_to='dispute_evidences/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Evidence by {self.submitted_by.username} for task {self.dispute.task.title}"


class DisputeVote(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    voter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes_cast')
    voted_for = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes_received')
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'voter')

    def __str__(self):
        return f"Vote by {self.voter.username} for {self.voted_for.username} in dispute {self.dispute.id}"

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
