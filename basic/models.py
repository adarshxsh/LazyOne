import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import os
import secrets
from django.db import models, transaction
from django.contrib.auth.models import User
import logging

logger = logging.getLogger(__name__)
from django.utils import timezone

# Initialize Firebase Admin SDK if not already initialized
if not firebase_admin._apps:
    cred_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../serviceAccountKey.json')
    try:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK initialized successfully.")
    except Exception as e:
        print(f"Error initializing Firebase Admin SDK: {e}")

db = firestore.client() if firebase_admin._apps else None


def generate_public_id():
    return secrets.token_urlsafe(12)


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    bio = models.CharField(max_length=300, blank=True)
    first_name = models.CharField(max_length=50, blank=True)
    last_name = models.CharField(max_length=50, blank=True)
    college = models.CharField(max_length=100, blank=True)
    room_no = models.CharField(max_length=20, blank=True)
    major = models.CharField(max_length=100, blank=True)
    hostel = models.CharField(max_length=100, blank=True)
    roll_no = models.CharField(max_length=100, blank=True)
    batch = models.IntegerField(default=2029)
    rewards = models.IntegerField(default=1500)
    phone_number = models.CharField(max_length=20, blank=True)
    is_phone_verified = models.BooleanField(default=False)
    instagram_username = models.CharField(max_length=100, blank=True)
    is_instagram_verified = models.BooleanField(default=False)
    firebase_uid = models.CharField(max_length=128, blank=True, null=True, unique=True)
    friends = models.ManyToManyField('self', through='Friendship', symmetrical=False, blank=True)

    def __str__(self):
        return self.user.username


class Task(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = 'available', 'Available'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETED = 'completed', 'Completed'
        DISPUTED = 'disputed', 'Disputed'
        CANCELLED = 'cancelled', 'Cancelled'

    public_id = models.CharField(max_length=20, default=generate_public_id, unique=True, editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField()
    reward = models.PositiveIntegerField()
    posted_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='posted_tasks')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    taken_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='taken_tasks')
    deadline = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AVAILABLE, db_index=True)
    cancellation_requested = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(reward__gte=0), name='reward_non_negative')
        ]

    def __str__(self):
        return self.title


class RewardLedger(models.Model):
    class TransactionType(models.TextChoices):
        TASK_CREATION = 'task_creation', 'Task Creation (Points Reserved)'
        TASK_COMPLETION = 'task_completion', 'Task Completion (Points Awarded)'
        TASK_CANCELLATION = 'task_cancellation', 'Task Cancellation (Points Refunded)'
        INITIAL_POINTS = 'initial_points', 'Initial Points'

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='reward_transactions')
    task = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True)
    amount = models.IntegerField()
    transaction_type = models.CharField(max_length=30, choices=TransactionType.choices)
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username}: {self.amount} points for {self.description}"


def sync_dispute_to_firestore(dispute_id):
    if not db:
        return
    try:
        doc_ref = db.collection('disputes').document(str(dispute_id))
        dispute = Dispute.objects.get(id=dispute_id)
        dispute_data = {
            'public_id': dispute.public_id,
            'task_id': dispute.task.id,
            'raised_by_user_id': dispute.raised_by.id if dispute.raised_by else None,
            'raised_by_username': dispute.raised_by.username if dispute.raised_by else "Unknown",
            'reason': dispute.reason,
            'dispute_type': dispute.dispute_type,
            'status': dispute.status,
            'created_at': dispute.created_at.isoformat(),
            'django_id': dispute.id,
        }
        doc_ref.set(dispute_data)
        logger.info(f"Dispute {dispute_id} synced to Firestore.")
    except Exception as e:
        logger.exception(f"Error syncing dispute {dispute_id} to Firestore")


class Dispute(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        RESOLVED = 'resolved', 'Resolved'
        WITHDRAWN = 'withdrawn', 'Withdrawn'

    class DisputeType(models.TextChoices):
        PAYMENT = 'payment', 'Payment Issue'
        QUALITY = 'quality', 'Quality of Work'
        COMMUNICATION = 'communication', 'Communication Breakdown'
        OTHER = 'other', 'Other'

    public_id = models.CharField(max_length=20, default=generate_public_id, unique=True, editable=False)
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='raised_disputes')
    reason = models.TextField()
    dispute_type = models.CharField(max_length=20, choices=DisputeType.choices, default=DisputeType.OTHER)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        transaction.on_commit(lambda: sync_dispute_to_firestore(self.id))


class FriendRequest(models.Model):
    from_user = models.ForeignKey(User, related_name='from_user', on_delete=models.CASCADE)
    to_user = models.ForeignKey(User, related_name='to_user', on_delete=models.CASCADE)
    closeness = models.IntegerField(default=50)
    is_accepted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['from_user', 'to_user'], name='unique_friend_request')
        ]

    def __str__(self):
        return f"From {self.from_user} to {self.to_user}"


class Friendship(models.Model):
    from_user = models.ForeignKey(UserProfile, related_name='friendship_from_user', on_delete=models.CASCADE)
    to_user = models.ForeignKey(UserProfile, related_name='friendship_to_user', on_delete=models.CASCADE)
    closeness = models.IntegerField(default=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['from_user', 'to_user'], name='unique_friendship')
        ]


class Conversation(models.Model):
    public_id = models.CharField(max_length=20, default=generate_public_id, unique=True, editable=False)
    task = models.OneToOneField(Task, on_delete=models.CASCADE, null=True, blank=True)
    participants = models.ManyToManyField(User, related_name='conversations')
    last_message_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        if self.task:
            return f"Chat for task: {self.task.title}"
        participant_names = [user.username for user in self.participants.all()]
        return f"Chat between {' and '.join(participant_names)}"


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_messages')
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    is_read = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        sender_name = self.sender.username if self.sender else "Deleted User"
        return f"Message from {sender_name} in {self.conversation}"


class Notification(models.Model):
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    message = models.CharField(max_length=255)
    link = models.URLField(blank=True, null=True)
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"Notification for {self.recipient.username}: {self.message}"

    class Meta:
        ordering = ['-created_at']
