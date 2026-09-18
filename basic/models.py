import math
from datetime import timedelta
from django.apps import apps
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

    @property
    def poster_votes_count(self):
        return self.votes.filter(voted_for=self.task.posted_by).count()

    @property
    def taker_votes_count(self):
        return self.votes.filter(voted_for=self.task.taken_by).count()

    @property
    def total_votes_count(self):
        return self.votes.count()

    def check_and_resolve_consensus(self, threshold=None, expiration_hours=None):
        if self.status != 'open':
            return False

        from django.conf import settings
        if threshold is None:
            threshold = getattr(settings, 'JURY_VOTE_THRESHOLD', 3)
        if expiration_hours is None:
            expiration_hours = getattr(settings, 'DISPUTE_EXPIRATION_HOURS', 72)

        total_votes = self.votes.count()
        is_expired = timezone.now() >= (self.created_at + timedelta(hours=expiration_hours))

        if total_votes >= threshold or is_expired:
            poster_votes = self.votes.filter(voted_for=self.task.posted_by).count()
            taker_votes = self.votes.filter(voted_for=self.task.taken_by).count()

            if taker_votes > poster_votes:
                winning_party = 'taker'
            elif poster_votes > taker_votes:
                winning_party = 'poster'
            else:
                winning_party = 'poster'

            self.resolve_with_winner(winning_party)
            return True

        return False

    def resolve_with_winner(self, winning_party):
        from django.db import transaction
        from django.urls import reverse

        with transaction.atomic():
            task = self.task
            Notification = apps.get_model('basic', 'Notification')

            if winning_party == 'taker':
                task.status = 'completed'
                task.save()

                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Completed task: '{task.title}' via community jury consensus"
                )

                if self.raised_by == task.taken_by:
                    self.refund_deposit(
                        reason_description=f"Security deposit bond refunded upon winning dispute resolution for task: '{task.title}'"
                    )
                else:
                    self.forfeit_deposit(
                        beneficiary=task.taken_by,
                        reason_description=f"Security deposit bond forfeited upon losing dispute resolution for task: '{task.title}'"
                    )

                self.status = 'resolved'
                self.save()

                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Community jury consensus resolved dispute for '{task.title}' in favor of task taker ({task.taken_by.username}).",
                    link=reverse('my_tasks')
                )
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Community jury consensus resolved dispute for '{task.title}' in your favor. {task.reward} points awarded.",
                    link=reverse('my_tasks')
                )

            else:  # winning_party == 'poster'
                task.status = 'cancelled'
                task.save()

                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Refund for dispute resolution on task: '{task.title}'"
                )

                if self.raised_by == task.posted_by:
                    self.refund_deposit(
                        reason_description=f"Security deposit bond refunded upon winning dispute resolution for task: '{task.title}'"
                    )
                else:
                    self.forfeit_deposit(
                        beneficiary=task.posted_by,
                        reason_description=f"Security deposit bond forfeited upon losing dispute resolution for task: '{task.title}'"
                    )

                self.status = 'resolved'
                self.save()

                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Community jury consensus resolved dispute for '{task.title}' in your favor. Task reward refunded.",
                    link=reverse('my_tasks')
                )
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Community jury consensus resolved dispute for '{task.title}' in favor of task poster ({task.posted_by.username}).",
                    link=reverse('my_tasks')
                )


class DisputeVote(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    voter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes')
    voted_for = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes_received')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'voter')

    def __str__(self):
        return f"{self.voter.username} voted for {self.voted_for.username} on dispute {self.dispute.id}"

    @property
    def vote(self):
        if self.voted_for_id == self.dispute.task.posted_by_id:
            return 'poster'
        elif self.voted_for_id == self.dispute.task.taken_by_id:
            return 'taker'
        return None

    @property
    def choice(self):
        return self.vote

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
