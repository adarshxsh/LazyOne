import secrets
from django.db import models
from django.contrib.auth.models import User
import logging

logger = logging.getLogger(__name__)
from django.utils import timezone

def generate_public_id():
    return secrets.token_urlsafe(12)


class Community(models.Model):
    """A campus / hostel cluster. All marketplace data is scoped to one (ARCHITECTURE_V2 §4.6)."""
    DEFAULT_SLUG = 'public'

    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    email_domain = models.CharField(max_length=100, blank=True, null=True, unique=True,
                                    help_text="Users whose email ends with @<domain> join automatically.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = 'communities'

    def __str__(self):
        return self.name

    @classmethod
    def default(cls):
        return cls.objects.get_or_create(slug=cls.DEFAULT_SLUG, defaults={'name': 'Public'})[0]

    @classmethod
    def for_email(cls, email):
        domain = (email or '').rsplit('@', 1)[-1].lower() if email and '@' in email else None
        if domain:
            match = cls.objects.filter(email_domain__iexact=domain).first()
            if match:
                return match
        return cls.default()


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
    community = models.ForeignKey(Community, null=True, blank=True, on_delete=models.SET_NULL, related_name='members')

    def __str__(self):
        return self.user.username

    def get_community(self):
        if self.community_id is None:
            self.community = Community.for_email(self.user.email)
            UserProfile.objects.filter(pk=self.pk).update(community=self.community)
        return self.community


class Task(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = 'available', 'Open'
        IN_PROGRESS = 'in_progress', 'In Progress'
        SUBMITTED = 'submitted', 'Submitted for approval'
        COMPLETED = 'completed', 'Completed'
        DISPUTED = 'disputed', 'Disputed'
        CANCELLED = 'cancelled', 'Cancelled'
        EXPIRED = 'expired', 'Expired'

    class WavePolicy(models.TextChoices):
        DEFAULT = 'default', 'Closest friends first, then everyone'
        FRIENDS_ONLY = 'friends_only', 'Friends and friends-of-friends only'
        COMMUNITY_NOW = 'community_now', 'Everyone in my community right away'

    ACTIVE_STATUSES = ('available', 'in_progress', 'submitted', 'disputed')

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
    community = models.ForeignKey(Community, null=True, blank=True, on_delete=models.PROTECT, related_name='tasks')
    wave_policy = models.CharField(max_length=20, choices=WavePolicy.choices, default=WavePolicy.DEFAULT)
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submission_note = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(reward__gte=0), name='reward_non_negative')
        ]
        indexes = [models.Index(fields=['community', 'status', 'opened_at'])]

    def __str__(self):
        return self.title

    @property
    def dispute(self):
        """Most relevant dispute: the open/escalated one, else the latest."""
        disputes = list(self.disputes.order_by('-created_at'))
        for d in disputes:
            if d.status in (Dispute.Status.OPEN, Dispute.Status.ESCALATED):
                return d
        return disputes[0] if disputes else None

    @property
    def auto_approve_at(self):
        from django.conf import settings
        if self.submitted_at:
            return self.submitted_at + settings.LAZYONE['AUTO_APPROVE_AFTER']
        return None


class TaskEvent(models.Model):
    """Append-only audit trail of every task transition."""
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='events')
    action = models.CharField(max_length=30)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    from_status = models.CharField(max_length=20)
    to_status = models.CharField(max_length=20)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return f"{self.task_id}: {self.from_status} -{self.action}-> {self.to_status}"


class RewardLedger(models.Model):
    class TransactionType(models.TextChoices):
        TASK_CREATION = 'task_creation', 'Task Creation (Points Reserved)'
        TASK_COMPLETION = 'task_completion', 'Task Completion (Points Awarded)'
        TASK_CANCELLATION = 'task_cancellation', 'Task Cancellation (Points Refunded)'
        INITIAL_POINTS = 'initial_points', 'Initial Points'
        DISPUTE_BOND = 'dispute_bond', 'Dispute Bond Posted'
        DISPUTE_BOND_REFUND = 'dispute_bond_refund', 'Dispute Bond Refunded'
        DISPUTE_BOND_FORFEIT = 'dispute_bond_forfeit', 'Dispute Bond / Juror Reward'

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='reward_transactions')
    task = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True)
    amount = models.IntegerField()
    transaction_type = models.CharField(max_length=30, choices=TransactionType.choices)
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username}: {self.amount} points for {self.description}"


class Dispute(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        RESOLVED = 'resolved', 'Resolved'
        WITHDRAWN = 'withdrawn', 'Withdrawn'
        ESCALATED = 'escalated', 'Escalated to moderator'

    class Outcome(models.TextChoices):
        TAKER = 'taker', 'Decided for task taker'
        POSTER = 'poster', 'Decided for task creator'
        CONCEDED = 'conceded', 'Creator approved payment'
        WITHDRAWN = 'withdrawn', 'Withdrawn'

    class DisputeType(models.TextChoices):
        PAYMENT = 'payment', 'Payment Issue'
        QUALITY = 'quality', 'Quality of Work'
        COMMUNICATION = 'communication', 'Communication Breakdown'
        OTHER = 'other', 'Other'

    public_id = models.CharField(max_length=20, default=generate_public_id, unique=True, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='disputes')
    raised_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='raised_disputes')
    reason = models.TextField()
    dispute_type = models.CharField(max_length=20, choices=DisputeType.choices, default=DisputeType.OTHER)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    bond_amount = models.PositiveIntegerField(default=0)
    evidence_deadline = models.DateTimeField(null=True, blank=True)
    voting_deadline = models.DateTimeField(null=True, blank=True)
    jury_seed = models.CharField(max_length=32, blank=True)
    outcome = models.CharField(max_length=20, choices=Outcome.choices, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['task'], condition=models.Q(status__in=['open', 'escalated']),
                                    name='one_active_dispute_per_task'),
        ]

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    @property
    def is_active(self):
        return self.status in (self.Status.OPEN, self.Status.ESCALATED)


class DisputeEvidence(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='evidence')
    submitted_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    text = models.TextField(max_length=4000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


class JurorAssignment(models.Model):
    class Vote(models.TextChoices):
        POSTER = 'poster', 'Task creator'
        TAKER = 'taker', 'Task taker'

    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='jurors')
    juror = models.ForeignKey(User, on_delete=models.CASCADE, related_name='jury_duties')
    vote = models.CharField(max_length=10, choices=Vote.choices, blank=True)
    voted_at = models.DateTimeField(null=True, blank=True)
    reward = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['dispute', 'juror'], name='unique_juror_per_dispute')]


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


def realtime_uid(user_or_id):
    """Identity used for Firebase custom tokens and Firestore rules (ARCHITECTURE_V2 §4.4)."""
    return f"django:{getattr(user_or_id, 'pk', user_or_id)}"


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
