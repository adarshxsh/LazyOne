import math
from datetime import timedelta
from django.db import models
from django.db.models import Sum
from django.contrib.auth.models import User
from django.utils import timezone
from django.urls import reverse

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
        ('juror_reward', 'Juror Reward Points Awarded'),
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

    def get_vote_weight(self, user):
        if not hasattr(user, 'userprofile'):
            return 1
        rewards = user.userprofile.rewards
        if rewards >= 3000:
            return 5
        elif rewards >= 1500:
            return 3
        elif rewards >= 500:
            return 2
        else:
            return 1

    def can_user_vote(self, user):
        if not user or not user.is_authenticated:
            return False
        if self.status != 'open':
            return False
        if user == self.task.posted_by or user == self.task.taken_by:
            return False
        if not hasattr(user, 'userprofile'):
            return False
        if user.userprofile.rewards < 100:
            return False
        if user.date_joined and (timezone.now() - user.date_joined < timedelta(hours=24)):
            return False
        if self.votes.filter(voter=user).exists():
            return False
        return True

    def get_weighted_tally(self):
        poster_votes = self.votes.filter(vote_choice='poster')
        taker_votes = self.votes.filter(vote_choice='taker')
        w_poster = poster_votes.aggregate(Sum('weight'))['weight__sum'] or 0
        w_taker = taker_votes.aggregate(Sum('weight'))['weight__sum'] or 0
        w_total = w_poster + w_taker
        poster_count = poster_votes.count()
        taker_count = taker_votes.count()
        total_voters = self.votes.count()
        return {
            'poster_weight': w_poster,
            'taker_weight': w_taker,
            'total_weight': w_total,
            'poster_count': poster_count,
            'taker_count': taker_count,
            'total_voters': total_voters,
        }

    def has_quorum(self):
        tally = self.get_weighted_tally()
        return tally['total_weight'] >= 10 and tally['total_voters'] >= 3

    def check_and_execute_consensus(self):
        if self.status != 'open':
            return False
        if not self.has_quorum():
            return False

        tally = self.get_weighted_tally()
        w_total = tally['total_weight']
        if w_total == 0:
            return False

        w_poster = tally['poster_weight']
        w_taker = tally['taker_weight']

        poster_ratio = w_poster / w_total
        taker_ratio = w_taker / w_total

        task = self.task

        if poster_ratio >= 0.60:
            if self.raised_by == task.posted_by:
                self.refund_deposit(reason_description=f"Security deposit bond refunded for dispute on task: '{task.title}'")
            else:
                self.forfeit_deposit(beneficiary=task.posted_by)

            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task reward refunded for resolved dispute on task: '{task.title}'"
            )

            task.status = 'cancelled'
            task.save()
            self.status = 'resolved'
            self.save()

            winning_votes = self.votes.filter(vote_choice='poster')
            for vote in winning_votes:
                juror_profile = vote.voter.userprofile
                juror_profile.rewards += 10
                juror_profile.save()
                RewardLedger.objects.create(
                    user=vote.voter,
                    task=task,
                    amount=10,
                    transaction_type='juror_reward',
                    description=f"Jury voting reward for dispute resolution on task: '{task.title}'"
                )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' was resolved in your favor by community jury voting.",
                link=reverse('dispute_detail', args=[self.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' was resolved in favor of the poster by community jury voting.",
                    link=reverse('dispute_detail', args=[self.id])
                )
            return True

        elif taker_ratio >= 0.60:
            if self.raised_by == task.taken_by:
                self.refund_deposit(reason_description=f"Security deposit bond refunded for dispute on task: '{task.title}'")
            else:
                self.forfeit_deposit(beneficiary=task.taken_by)

            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Awarded reward for resolved dispute on task: '{task.title}'"
                )

            task.status = 'completed'
            task.save()
            self.status = 'resolved'
            self.save()

            winning_votes = self.votes.filter(vote_choice='taker')
            for vote in winning_votes:
                juror_profile = vote.voter.userprofile
                juror_profile.rewards += 10
                juror_profile.save()
                RewardLedger.objects.create(
                    user=vote.voter,
                    task=task,
                    amount=10,
                    transaction_type='juror_reward',
                    description=f"Jury voting reward for dispute resolution on task: '{task.title}'"
                )

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Dispute for task '{task.title}' was resolved in favor of the taker by community jury voting.",
                link=reverse('dispute_detail', args=[self.id])
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Dispute for task '{task.title}' was resolved in your favor by community jury voting.",
                    link=reverse('dispute_detail', args=[self.id])
                )
            return True

        return False

class DisputeVote(models.Model):
    VOTE_CHOICES = (
        ('poster', 'In Favor of Poster'),
        ('taker', 'In Favor of Taker'),
    )
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    voter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes')
    vote_choice = models.CharField(max_length=10, choices=VOTE_CHOICES)
    weight = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'voter')

    def __str__(self):
        return f"Vote by {self.voter.username} on dispute {self.dispute.id}: {self.vote_choice} (weight: {self.weight})"

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
