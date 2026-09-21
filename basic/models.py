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
        ('juror_reward', 'Juror Reward Payout'),
        ('dispute_penalty', 'Dispute Bad-Actor Penalty'),
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

    def forfeit_deposit(self, beneficiary=None, losing_user=None, jurors=None, reason_description=None):
        from django.db import transaction
        if self.escrow_status == 'held' and self.deposit_amount > 0:
            with transaction.atomic():
                losing = losing_user or self.raised_by
                if losing == self.raised_by:
                    RewardLedger.objects.create(
                        user=self.raised_by,
                        task=self.task,
                        amount=self.deposit_amount,
                        transaction_type='dispute_refund',
                        description=f"Deposit escrow released for dispute resolution on task: '{self.task.title}'"
                    )
                    r_profile = self.raised_by.userprofile
                    r_profile.rewards += self.deposit_amount
                    r_profile.save()

                    r_profile.rewards -= self.deposit_amount
                    r_profile.save()
                    desc = reason_description or f"Security deposit bond forfeited as penalty for dispute on task: '{self.task.title}'"
                    RewardLedger.objects.create(
                        user=self.raised_by,
                        task=self.task,
                        amount=-self.deposit_amount,
                        transaction_type='dispute_penalty',
                        description=desc
                    )
                else:
                    self.refund_deposit(reason_description=f"Security deposit bond refunded for dispute on task: '{self.task.title}'")

                    l_profile = losing.userprofile
                    l_profile.rewards -= self.deposit_amount
                    l_profile.save()
                    desc = reason_description or f"Security deposit bond forfeited as penalty for dispute on task: '{self.task.title}'"
                    RewardLedger.objects.create(
                        user=losing,
                        task=self.task,
                        amount=-self.deposit_amount,
                        transaction_type='dispute_penalty',
                        description=desc
                    )

                if jurors:
                    jurors_list = list(jurors)
                    num_jurors = len(jurors_list)
                    if num_jurors > 0:
                        reward_per_juror = self.deposit_amount // num_jurors
                        for juror in jurors_list:
                            j_profile = juror.userprofile
                            j_profile.rewards += reward_per_juror
                            j_profile.save()
                            RewardLedger.objects.create(
                                user=juror,
                                task=self.task,
                                amount=reward_per_juror,
                                transaction_type='juror_reward',
                                description=f"Juror reward for dispute on task: '{self.task.title}'"
                            )
                elif beneficiary:
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

                self.escrow_status = 'forfeited'
                self.save()

    def resolve_dispute(self, winner=None, losing_user=None, jurors=None, reason_description=None):
        from django.db import transaction
        with transaction.atomic():
            self.status = 'resolved'
            task = self.task
            if winner == task.taken_by:
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
                    description=f"Completed task: '{task.title}'"
                )
                if self.raised_by == task.posted_by:
                    self.forfeit_deposit(losing_user=task.posted_by, jurors=jurors, reason_description=reason_description)
                else:
                    self.refund_deposit(reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'")
                    if jurors:
                        pass
            elif winner == task.posted_by:
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
                    description=f"Refund for cancelled task: '{task.title}'"
                )
                if self.raised_by == task.taken_by:
                    self.forfeit_deposit(losing_user=task.taken_by, jurors=jurors, reason_description=reason_description)
                else:
                    self.refund_deposit(reason_description=f"Security deposit bond refunded upon winning dispute for task: '{task.title}'")
            else:
                if jurors:
                    losing = losing_user or self.raised_by
                    self.forfeit_deposit(losing_user=losing, jurors=jurors, reason_description=reason_description)
            self.save()

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
