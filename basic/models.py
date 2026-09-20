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
        ('dispute_refund', 'Dispute Deposit Bond Refunded'),
        ('dispute_forfeit', 'Dispute Deposit Bond Forfeited'),
        ('appeal_deposit', 'Appeal Deposit Bond Held'),
        ('appeal_refund', 'Appeal Deposit Bond Refunded'),
        ('juror_reward', 'Juror Reward Distribution'),
        ('juror_slashing', 'Juror Stake Slashing Penalty'),
        ('litigant_slashing', 'Dishonest Litigant Slashing Penalty'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reward_transactions')
    task = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True)
    amount = models.IntegerField()
    transaction_type = models.CharField(max_length=30, choices=TRANSACTION_TYPES)
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username}: {self.amount} points for {self.description}"

class Dispute(models.Model):
    STATUS_CHOICES = (
        ('open', 'Open'),
        ('peer_review', 'Peer Review'),
        ('appealed', 'Appealed'),
        ('grand_jury_review', 'Grand Jury Review'),
        ('resolved', 'Resolved'),
        ('slashed', 'Slashed'),
    )
    ESCROW_STATUS_CHOICES = (
        ('held', 'Held in Escrow'),
        ('refunded', 'Refunded'),
        ('forfeited', 'Forfeited'),
    )
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='raised_disputes')
    reason = models.TextField()
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='open')
    deposit_amount = models.PositiveIntegerField(default=0)
    escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='held')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    @property
    def can_be_appealed(self):
        if self.status in ['appealed', 'grand_jury_review', 'slashed']:
            return False
        tier1_panel = self.jury_panels.filter(tier=1, status='resolved').order_by('-resolved_at').first()
        if not tier1_panel or not tier1_panel.resolved_at:
            return False
        if self.appeals.exists():
            return False
        expiry = tier1_panel.resolved_at + timedelta(hours=48)
        return timezone.now() <= expiry

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

class DisputeAppeal(models.Model):
    STAGE_CHOICES = (
        ('tier2', 'Tier-2 Grand Jury'),
    )
    OUTCOME_CHOICES = (
        ('pending', 'Pending'),
        ('upheld', 'Upheld'),
        ('overturned', 'Overturned'),
        ('dismissed', 'Dismissed'),
    )
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='appeals')
    stage = models.CharField(max_length=20, choices=STAGE_CHOICES, default='tier2')
    appellant = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_appeals')
    deposit_amount = models.PositiveIntegerField(default=0)
    justification = models.TextField()
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Appeal for dispute {self.dispute.id} by {self.appellant.username}"

class JuryPanel(models.Model):
    STATUS_CHOICES = (
        ('active', 'Active'),
        ('resolved', 'Resolved'),
        ('escalated', 'Escalated'),
        ('expired', 'Expired'),
    )
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='jury_panels')
    tier = models.PositiveIntegerField(default=1)
    quorum_size = models.PositiveIntegerField(default=3)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    jurors = models.ManyToManyField(User, related_name='jury_panels')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Tier-{self.tier} JuryPanel for Dispute {self.dispute.id} ({self.status})"

    def assign_eligible_jurors(self):
        task = self.dispute.task
        excluded_ids = {task.posted_by.id}
        if task.taken_by:
            excluded_ids.add(task.taken_by.id)

        parties_profiles = []
        if hasattr(task.posted_by, 'userprofile'):
            parties_profiles.append(task.posted_by.userprofile)
        if task.taken_by and hasattr(task.taken_by, 'userprofile'):
            parties_profiles.append(task.taken_by.userprofile)

        for profile in parties_profiles:
            friendships = Friendship.objects.filter(
                models.Q(from_user=profile) | models.Q(to_user=profile)
            )
            for f in friendships:
                excluded_ids.add(f.from_user.user.id)
                excluded_ids.add(f.to_user.user.id)

        existing_juror_ids = User.objects.filter(jury_panels__dispute=self.dispute).values_list('id', flat=True)
        excluded_ids.update(existing_juror_ids)

        eligible_users = User.objects.filter(is_active=True).exclude(id__in=excluded_ids).order_by('?')
        selected = list(eligible_users[:self.quorum_size])
        self.jurors.set(selected)
        return selected

    def evaluate_consensus(self):
        votes = self.votes.all()
        total_cast = votes.count()
        if total_cast < self.quorum_size:
            return None

        counts = {}
        for v in votes:
            counts[v.voted_for] = counts.get(v.voted_for, 0) + 1

        required_supermajority = math.ceil(self.quorum_size * 0.66)

        for party, count in counts.items():
            if count >= required_supermajority:
                return party

        if counts:
            top_party = max(counts, key=counts.get)
            return top_party

        return None

class JurorVote(models.Model):
    panel = models.ForeignKey(JuryPanel, on_delete=models.CASCADE, related_name='votes')
    juror = models.ForeignKey(User, on_delete=models.CASCADE, related_name='juror_votes')
    voted_for = models.ForeignKey(User, on_delete=models.CASCADE, related_name='juror_votes_received')
    justification = models.TextField(blank=True, default='')
    voted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('panel', 'juror')

    def __str__(self):
        return f"Vote by {self.juror.username} on Panel {self.panel.id} for {self.voted_for.username}"


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
