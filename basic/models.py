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
        return getattr(self, 'conversation', None)

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
        ('jury_reward', 'Jury Participation Bonus'),
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
        ('staff_review', 'Under Staff Review'),
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

    @property
    def voting_deadline(self):
        from datetime import timedelta
        return self.created_at + timedelta(hours=48)

    @property
    def is_voting_expired(self):
        return timezone.now() > self.voting_deadline

    def check_consensus_and_settle(self):
        if self.status != 'open':
            return False

        poster_votes = self.votes.filter(vote_for=self.task.posted_by).count()
        taker_votes = self.votes.filter(vote_for=self.task.taken_by).count() if self.task.taken_by else 0

        if taker_votes >= 2:
            self._settle_for_winner(winner=self.task.taken_by, loser=self.task.posted_by)
            return True
        elif poster_votes >= 2:
            self._settle_for_winner(winner=self.task.posted_by, loser=self.task.taken_by)
            return True
        return False

    def _settle_for_winner(self, winner, loser):
        from django.urls import reverse
        task = self.task

        self.status = 'resolved'
        self.save()

        if winner == task.taken_by:
            task.status = 'completed'
            task.save()

            winner_profile = winner.userprofile
            winner_profile.rewards += task.reward
            winner_profile.save()

            RewardLedger.objects.create(
                user=winner,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Awarded task reward from jury consensus on dispute: '{task.title}'"
            )

            if self.raised_by == task.taken_by:
                self.refund_deposit(reason_description=f"Deposit bond refunded following jury consensus win for task '{task.title}'")
            else:
                self.forfeit_deposit(beneficiary=winner, reason_description=f"Deposit bond forfeited to winner following jury consensus for task '{task.title}'")

        else:
            task.status = 'cancelled'
            task.save()

            winner_profile = winner.userprofile
            winner_profile.rewards += task.reward
            winner_profile.save()

            RewardLedger.objects.create(
                user=winner,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refunded task reward from jury consensus on dispute: '{task.title}'"
            )

            if self.raised_by == task.posted_by:
                self.refund_deposit(reason_description=f"Deposit bond refunded following jury consensus win for task '{task.title}'")
            else:
                self.forfeit_deposit(beneficiary=winner, reason_description=f"Deposit bond forfeited to winner following jury consensus for task '{task.title}'")

        dispute_link = reverse('dispute_detail', args=[self.id])
        Notification.objects.create(
            recipient=winner,
            message=f"Jury consensus reached! You won the dispute for task '{task.title}'.",
            link=dispute_link
        )
        if loser:
            Notification.objects.create(
                recipient=loser,
                message=f"Jury consensus reached! Dispute for task '{task.title}' was resolved in favor of {winner.username}.",
                link=dispute_link
            )

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

class JuryAssignment(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='jury_assignments')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='jury_assignments')
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'user')

    def __str__(self):
        return f"Juror {self.user.username} for dispute {self.dispute.id}"

class DisputeVote(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    juror = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes')
    vote_for = models.ForeignKey(User, on_delete=models.CASCADE, related_name='votes_received')
    voted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'juror')

    def __str__(self):
        return f"Vote by {self.juror.username} on dispute {self.dispute.id}"

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
