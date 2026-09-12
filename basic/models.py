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
    STATUS_CHOICES = (
        ('open', 'Open'),
        ('resolved', 'Resolved'),
    )
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='raised_disputes')
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    created_at = models.DateTimeField(auto_now_add=True)

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

class JuryPool(models.Model):
    dispute = models.OneToOneField(Dispute, on_delete=models.CASCADE, related_name='jury_pool')
    jurors = models.ManyToManyField(User, related_name='jury_pools')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Jury Pool for {self.dispute}"

    @classmethod
    def create_for_dispute(cls, dispute, pool_size=5):
        import random
        from django.urls import reverse

        excluded_ids = set()

        # Exclude task participants and their friends
        if dispute.task.posted_by:
            excluded_ids.add(dispute.task.posted_by.id)
            if hasattr(dispute.task.posted_by, 'userprofile'):
                poster_p = dispute.task.posted_by.userprofile
                excluded_ids.update(poster_p.friends.values_list('user__id', flat=True))
                excluded_ids.update(Friendship.objects.filter(from_user=poster_p).values_list('to_user__user__id', flat=True))
                excluded_ids.update(Friendship.objects.filter(to_user=poster_p).values_list('from_user__user__id', flat=True))

        if dispute.task.taken_by:
            excluded_ids.add(dispute.task.taken_by.id)
            if hasattr(dispute.task.taken_by, 'userprofile'):
                taker_p = dispute.task.taken_by.userprofile
                excluded_ids.update(taker_p.friends.values_list('user__id', flat=True))
                excluded_ids.update(Friendship.objects.filter(from_user=taker_p).values_list('to_user__user__id', flat=True))
                excluded_ids.update(Friendship.objects.filter(to_user=taker_p).values_list('from_user__user__id', flat=True))

        candidate_users = User.objects.exclude(id__in=excluded_ids).filter(is_active=True)
        candidate_ids = list(candidate_users.values_list('id', flat=True))

        selected_count = min(pool_size, len(candidate_ids))
        if selected_count > 0:
            selected_ids = random.sample(candidate_ids, selected_count)
            selected_users = list(User.objects.filter(id__in=selected_ids))
        else:
            selected_users = []

        jury_pool = cls.objects.create(dispute=dispute)
        jury_pool.jurors.set(selected_users)

        for juror in selected_users:
            Notification.objects.create(
                recipient=juror,
                message=f"You have been selected as a juror for dispute on task: '{dispute.task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        return jury_pool


class DisputeVote(models.Model):
    VOTE_CHOICES = (
        ('poster', 'In favor of Poster'),
        ('taker', 'In favor of Taker'),
    )

    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    juror = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes')
    vote = models.CharField(max_length=10, choices=VOTE_CHOICES)
    rationale = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'juror')

    def __str__(self):
        return f"Vote by {self.juror.username} on {self.dispute}: {self.vote}"


RewardTransaction = RewardLedger
