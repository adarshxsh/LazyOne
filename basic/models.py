import math
import hashlib
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
        ('resolved', 'Resolved'),
    )
    ESCROW_STATUS_CHOICES = (
        ('held', 'Held in Escrow'),
        ('refunded', 'Refunded'),
        ('forfeited', 'Forfeited'),
    )
    PHASE_CHOICES = (
        ('commit', 'Commit Phase'),
        ('reveal', 'Reveal Phase'),
        ('concluded', 'Concluded Phase'),
    )
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='raised_disputes')
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    phase = models.CharField(max_length=20, choices=PHASE_CHOICES, default='commit')
    commit_deadline = models.DateTimeField(null=True, blank=True)
    reveal_deadline = models.DateTimeField(null=True, blank=True)
    winning_choice = models.CharField(max_length=20, null=True, blank=True)
    deposit_amount = models.PositiveIntegerField(default=0)
    escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='held')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

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

    def calculate_consensus(self):
        """
        Calculates final dispute consensus from verified revealed votes.
        Excludes unrevealed commitments or invalid reveal hashes, applying appropriate penalties.
        """
        # Penalize unrevealed commitments or unverified votes
        for commitment in self.commitments.all():
            has_verified_vote = self.votes.filter(juror=commitment.juror, is_verified=True).exists()
            if not has_verified_vote:
                RewardLedger.objects.create(
                    user=commitment.juror,
                    task=self.task,
                    amount=0,
                    transaction_type='juror_slash',
                    description=f"Penalty for failing to reveal valid vote commitment in dispute on task: '{self.task.title}'"
                )

        verified_votes = self.votes.filter(is_verified=True)
        poster_votes = verified_votes.filter(choice='poster').count()
        taker_votes = verified_votes.filter(choice='taker').count()

        if poster_votes > taker_votes:
            self.winning_choice = 'poster'
            self.forfeit_deposit(beneficiary=self.task.posted_by, reason_description=f"Dispute resolved in favor of task poster for '{self.task.title}'")
            self.task.status = 'cancelled'
            self.task.save()
        elif taker_votes > poster_votes:
            self.winning_choice = 'taker'
            if self.raised_by == self.task.taken_by:
                self.refund_deposit(reason_description=f"Dispute resolved in favor of task taker for '{self.task.title}'")
            self.task.status = 'completed'
            self.task.save()
        else:
            self.winning_choice = 'tie'
            if self.escrow_status == 'held':
                self.refund_deposit(reason_description=f"Dispute resolved with tie/no majority for '{self.task.title}'")

        self.phase = 'concluded'
        self.status = 'resolved'
        self.save()
        return self.winning_choice


class JurorCommitment(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='commitments')
    juror = models.ForeignKey(User, on_delete=models.CASCADE, related_name='juror_commitments')
    commitment_hash = models.CharField(max_length=64)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'juror')

    def __str__(self):
        return f"Commitment by {self.juror.username} for dispute {self.dispute.id}"

    @staticmethod
    def compute_hash(choice, salt, juror_id):
        return hashlib.sha256(f"{choice}{salt}{juror_id}".encode('utf-8')).hexdigest()

    def verify_commitment(self, choice, salt):
        expected = self.compute_hash(choice, salt, self.juror.id)
        return expected == self.commitment_hash


class JurorVote(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    juror = models.ForeignKey(User, on_delete=models.CASCADE, related_name='juror_votes')
    commitment = models.OneToOneField(JurorCommitment, on_delete=models.CASCADE, related_name='vote', null=True, blank=True)
    choice = models.CharField(max_length=20)
    salt = models.CharField(max_length=255)
    is_verified = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'juror')

    def __str__(self):
        return f"Vote by {self.juror.username} ({self.choice}, verified={self.is_verified})"

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
