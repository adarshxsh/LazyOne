from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.exceptions import ValidationError

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
        ('dispute_resolution', 'Dispute Resolution'),
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
        ('evidence_submission', 'Evidence Submission'),
        ('voting', 'Voting'),
        ('resolved', 'Resolved'),
        ('withdrawn', 'Withdrawn'),
    )
    RESOLUTION_CHOICES = (
        ('resolved_poster_wins', 'Poster Wins'),
        ('resolved_taker_wins', 'Taker Wins'),
        ('split_settlement', 'Split Settlement'),
    )
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='raised_disputes')
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    resolution_outcome = models.CharField(max_length=30, choices=RESOLUTION_CHOICES, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._initial_status = self.status

    def clean(self):
        super().clean()
        if self.pk and hasattr(self, '_initial_status') and self.status != self._initial_status:
            valid_transitions = {
                'open': {'evidence_submission', 'withdrawn'},
                'evidence_submission': {'voting', 'withdrawn'},
                'voting': {'resolved', 'withdrawn'},
                'resolved': set(),
                'withdrawn': set(),
            }
            allowed = valid_transitions.get(self._initial_status, set())
            if self.status not in allowed:
                raise ValidationError(f"Invalid dispute status transition from '{self._initial_status}' to '{self.status}'.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
        self._initial_status = self.status

    def submit_evidence(self, user, evidence_text, evidence_url=None):
        from django.urls import reverse
        if self.status not in ['open', 'evidence_submission']:
            raise ValidationError("Evidence can only be submitted during the open or evidence submission phase.")
        if user != self.task.posted_by and user != self.task.taken_by:
            raise ValidationError("Only task participants (poster and taker) can submit evidence.")
        
        if self.status == 'open':
            self.status = 'evidence_submission'
            self.save()

        evidence = DisputeEvidence.objects.create(
            dispute=self,
            submitted_by=user,
            evidence_text=evidence_text,
            evidence_url=evidence_url
        )
        recipient = self.task.posted_by if user == self.task.taken_by else self.task.taken_by
        if recipient:
            Notification.objects.create(
                recipient=recipient,
                message=f"New evidence submitted by {user.username} for dispute on '{self.task.title}'.",
                link=reverse('dispute_detail', args=[self.id])
            )
        return evidence

    def start_voting(self):
        from django.urls import reverse
        if self.status != 'evidence_submission':
            raise ValidationError("Dispute must be in evidence submission phase to start voting.")
        self.status = 'voting'
        self.save()
        for participant in [self.task.posted_by, self.task.taken_by]:
            if participant:
                Notification.objects.create(
                    recipient=participant,
                    message=f"Dispute for task '{self.task.title}' has entered the voting phase.",
                    link=reverse('dispute_detail', args=[self.id])
                )

    def cast_vote(self, voter, voted_for):
        if self.status != 'voting':
            raise ValidationError("Voting is only allowed during the voting phase.")
        if voter == self.task.posted_by or voter == self.task.taken_by:
            raise ValidationError("Task poster and taker cannot vote on their own dispute.")
        if voted_for != self.task.posted_by and voted_for != self.task.taken_by:
            raise ValidationError("Votes must be cast for either the task poster or task taker.")
        if self.votes.filter(voter=voter).exists():
            raise ValidationError("You have already cast a vote for this dispute.")
        vote = DisputeVote.objects.create(
            dispute=self,
            voter=voter,
            voted_for=voted_for
        )
        return vote

    def finalize_resolution(self, outcome=None):
        from django.urls import reverse
        from django.db import transaction
        if self.status != 'voting':
            raise ValidationError("Dispute must be in voting phase to finalize resolution.")
        
        if not outcome:
            poster_votes = self.votes.filter(voted_for=self.task.posted_by).count()
            taker_votes = self.votes.filter(voted_for=self.task.taken_by).count()
            if poster_votes > taker_votes:
                outcome = 'resolved_poster_wins'
            elif taker_votes > poster_votes:
                outcome = 'resolved_taker_wins'
            else:
                outcome = 'split_settlement'

        if outcome not in ['resolved_poster_wins', 'resolved_taker_wins', 'split_settlement']:
            raise ValidationError(f"Invalid resolution outcome: {outcome}")

        with transaction.atomic():
            self.status = 'resolved'
            self.resolution_outcome = outcome
            self.save()

            task = self.task
            task.status = 'completed'
            task.save()

            if outcome == 'resolved_poster_wins':
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_resolution',
                    description=f"Dispute resolved in favor of poster for task: '{task.title}'"
                )
            elif outcome == 'resolved_taker_wins':
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='dispute_resolution',
                    description=f"Dispute resolved in favor of taker for task: '{task.title}'"
                )
            elif outcome == 'split_settlement':
                poster_share = task.reward // 2
                taker_share = task.reward - poster_share

                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += poster_share
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=poster_share,
                    transaction_type='dispute_resolution',
                    description=f"Split settlement (poster share) for dispute on task: '{task.title}'"
                )

                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += taker_share
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=taker_share,
                    transaction_type='dispute_resolution',
                    description=f"Split settlement (taker share) for dispute on task: '{task.title}'"
                )

            for participant in [task.posted_by, task.taken_by]:
                if participant:
                    Notification.objects.create(
                        recipient=participant,
                        message=f"Dispute for task '{task.title}' resolved: {self.get_resolution_outcome_display()}.",
                        link=reverse('dispute_detail', args=[self.id])
                    )

    def withdraw(self, by_user=None):
        from django.urls import reverse
        from django.db import transaction
        if self.status in ['resolved', 'withdrawn']:
            raise ValidationError("Cannot withdraw a dispute that is already resolved or withdrawn.")
        if by_user and by_user != self.raised_by:
            raise ValidationError("Only the user who raised the dispute can withdraw it.")
        
        with transaction.atomic():
            self.status = 'withdrawn'
            self.save()

            task = self.task
            task.status = 'in_progress'
            task.save()

            recipient = task.posted_by if self.raised_by == task.taken_by else task.taken_by
            if recipient:
                Notification.objects.create(
                    recipient=recipient,
                    message=f"{self.raised_by.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                    link=reverse('dispute_detail', args=[self.id])
                )

    def __str__(self):
        return f"Dispute for task: {self.task.title}"


class DisputeEvidence(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='evidence')
    submitted_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='submitted_evidence')
    evidence_text = models.TextField()
    evidence_url = models.URLField(max_length=500, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Evidence by {self.submitted_by.username} for dispute {self.dispute.id}"


class DisputeVote(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    voter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes')
    voted_for = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes_received')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (('dispute', 'voter'),)

    def __str__(self):
        return f"Vote by {self.voter.username} for {self.voted_for.username} on dispute {self.dispute.id}"

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
