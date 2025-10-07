import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import os
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

# Initialize Firebase Admin SDK if not already initialized
# IMPORTANT: In a production environment, load the credential path from Django settings
# or environment variables, and initialize Firebase in a more central place (e.g., apps.py's ready method)
if not firebase_admin._apps:
    # Adjust this path to where your serviceAccountKey.json is located
    # This example assumes it's in the project root, two levels up from models.py
    cred_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../serviceAccountKey.json')
    try:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK initialized successfully.")
    except Exception as e:
        print(f"Error initializing Firebase Admin SDK: {e}")
        # Handle the error appropriately, e.g., log it and prevent Firebase operations

db = firestore.client() if firebase_admin._apps else None


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
    firebase_uid = models.CharField(max_length=128, blank=True, null=True, unique=True)

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
    cancellation_requested = models.BooleanField(default=False) # New field to track cancellation requests

    def __str__(self):
        return self.title

class RewardLedger(models.Model):
    TRANSACTION_TYPES = (
        ('task_creation', 'Task Creation (Points Reserved)'),
        ('task_completion', 'Task Completion (Points Awarded)'),
        ('task_cancellation', 'Task Cancellation (Points Refunded)'),
        ('initial_points', 'Initial Points'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reward_transactions')
    task = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True)
    amount = models.IntegerField() # Can be positive (earned) or negative (spent)
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
    DISPUTE_TYPE_CHOICES = (
        ('payment', 'Payment Issue'),
        ('quality', 'Quality of Work'),
        ('communication', 'Communication Breakdown'),
        ('other', 'Other'),
    )
    # vote1 = models.
    # vote2 = models.IntegerField(default=0)
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='raised_disputes')
    reason = models.TextField()
    dispute_type = models.CharField(max_length=20, choices=DISPUTE_TYPE_CHOICES, default='other')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    def save(self, *args, **kwargs):
        old_instance = None
        if self.pk:
            try:
                old_instance = Dispute.objects.get(pk=self.pk)
            except Dispute.DoesNotExist:
                pass

        super().save(*args, **kwargs) # Call the original save method

        if db: # Only attempt to save to Firestore if Firebase was initialized
            doc_ref = db.collection('disputes').document(str(self.id))
            if self.status == 'resolved' and (old_instance and old_instance.status != 'resolved'):
                # If status changed to resolved, delete from Firestore
                try:
                    doc_ref.delete()
                    print(f"Dispute {self.id} deleted from Firestore successfully due to resolution.")
                except Exception as e:
                    print(f"Error deleting dispute {self.id} from Firestore: {e}")
            else:
                # Prepare data for Firestore
                dispute_data = {
                    'task_id': self.task.id,
                    'raised_by_user_id': self.raised_by.id,
                    'raised_by_username': self.raised_by.username,
                    'reason': self.reason,
                    'dispute_type': self.dispute_type,
                    'status': self.status,
                    'created_at': self.created_at.isoformat(), # Convert datetime to ISO format string
                    'django_id': self.id, # Store Django's primary key
                }

                # Save to Firestore
                try:
                    doc_ref.set(dispute_data)
                    print(f"Dispute {self.id} saved/updated to Firestore successfully.")
                except Exception as e:
                    print(f"Error saving dispute {self.id} to Firestore: {e}")
        else:
            print("Firebase not initialized, skipping Firestore save/delete for dispute.")


class FriendRequest(models.Model):
    from_user = models.ForeignKey(User, related_name='from_user', on_delete=models.CASCADE)
    to_user = models.ForeignKey(User, related_name='to_user', on_delete=models.CASCADE)
    closeness = models.IntegerField(default=50)
    is_accepted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"From {self.from_user} to {self.to_user}"

class Friendship(models.Model):
    from_user = models.ForeignKey(UserProfile, related_name='friendship_from_user', on_delete=models.CASCADE)
    to_user = models.ForeignKey(UserProfile, related_name='friendship_to_user', on_delete=models.CASCADE)
    closeness = models.IntegerField(default=50)

class Conversation(models.Model):
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