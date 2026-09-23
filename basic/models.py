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
    VOTING_PHASE_CHOICES = (
        ('commit', 'Commit Phase'),
        ('reveal', 'Reveal Phase'),
        ('finished', 'Finished'),
    )
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='raised_disputes')
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    voting_phase = models.CharField(max_length=20, choices=VOTING_PHASE_CHOICES, default='commit')
    deposit_amount = models.PositiveIntegerField(default=0)
    escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='held')
    created_at = models.DateTimeField(auto_now_add=True)
    commit_deadline = models.DateTimeField(null=True, blank=True)
    reveal_deadline = models.DateTimeField(null=True, blank=True)
    jurors = models.ManyToManyField(User, related_name='assigned_disputes', blank=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    def get_current_phase(self):
        now = timezone.now()
        if self.voting_phase == 'finished' or self.status == 'resolved':
            return 'finished'
        if self.commit_deadline and now >= self.commit_deadline and self.voting_phase == 'commit':
            self.voting_phase = 'reveal'
            self.save(update_fields=['voting_phase'])
        if self.reveal_deadline and now >= self.reveal_deadline and self.voting_phase == 'reveal':
            self.finalize_resolution()
            return 'finished'
        return self.voting_phase

    def can_user_vote(self, user):
        if not user or not user.is_authenticated:
            return False
        if user == self.task.posted_by or user == self.task.taken_by:
            return False
        if self.jurors.exists():
            return self.jurors.filter(id=user.id).exists()
        return True

    def tally_votes(self):
        revealed_commitments = self.commitments.filter(revealed=True)
        poster_votes = revealed_commitments.filter(vote_choice='poster').count()
        taker_votes = revealed_commitments.filter(vote_choice='taker').count()
        return {
            'poster': poster_votes,
            'taker': taker_votes,
            'total': poster_votes + taker_votes
        }

    def finalize_resolution(self):
        tallies = self.tally_votes()
        poster_votes = tallies['poster']
        taker_votes = tallies['taker']

        self.voting_phase = 'finished'
        self.status = 'resolved'
        self.save(update_fields=['voting_phase', 'status'])

        if taker_votes > poster_votes:
            self.refund_deposit()
            self.task.status = 'completed'
            self.task.save(update_fields=['status'])
        elif poster_votes > taker_votes:
            self.forfeit_deposit(beneficiary=self.task.posted_by)
            self.task.status = 'cancelled'
            self.task.save(update_fields=['status'])
        else:
            self.refund_deposit()

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


class DisputeCommitment(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='commitments')
    juror = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_commitments')
    commitment_hash = models.CharField(max_length=64)
    committed_at = models.DateTimeField(auto_now_add=True)
    vote_choice = models.CharField(max_length=50, blank=True, null=True)
    salt = models.CharField(max_length=128, blank=True, null=True)
    revealed = models.BooleanField(default=False)
    revealed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = ('dispute', 'juror')

    def __str__(self):
        return f"Commitment by {self.juror.username} for dispute {self.dispute.id}"

    @staticmethod
    def compute_hash(choice, salt, juror_id):
        data = f"{choice}:{salt}:{juror_id}".encode('utf-8')
        return hashlib.sha256(data).hexdigest()

    def verify_reveal(self, choice, salt):
        submitted_hash = self.commitment_hash.strip().lower()
        candidates = [
            hashlib.sha256(f"{choice}:{salt}:{self.juror.id}".encode('utf-8')).hexdigest(),
            hashlib.sha256(f"{choice}{salt}{self.juror.id}".encode('utf-8')).hexdigest(),
            hashlib.sha256(f"{choice}:{salt}".encode('utf-8')).hexdigest(),
            hashlib.sha256(f"{choice}{salt}".encode('utf-8')).hexdigest(),
        ]
        return submitted_hash in candidates

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
