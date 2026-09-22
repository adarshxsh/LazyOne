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
        ('juror_reward', 'Juror Reward Points'),
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


class JuryPanel(models.Model):
    STATUS_CHOICES = (
        ('voting', 'Voting in Progress'),
        ('resolved', 'Resolved'),
        ('cancelled', 'Cancelled'),
    )
    dispute = models.OneToOneField('Dispute', on_delete=models.CASCADE, related_name='jury_panel')
    jurors = models.ManyToManyField(User, related_name='assigned_jury_panels')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='voting')
    voting_deadline = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Jury Panel for Dispute #{self.dispute.id} ({self.status})"

    @classmethod
    def create_panel_for_dispute(cls, dispute, panel_size=3):
        from datetime import timedelta
        import random
        task = dispute.task

        # Identify counterparties
        counterparties = set()
        if task.posted_by:
            counterparties.add(task.posted_by.id)
        if task.taken_by:
            counterparties.add(task.taken_by.id)
        if dispute.raised_by:
            counterparties.add(dispute.raised_by.id)

        # Identify friends of counterparties
        excluded_user_ids = set(counterparties)
        for user_id in counterparties:
            try:
                user = User.objects.get(id=user_id)
                if hasattr(user, 'userprofile'):
                    profile = user.userprofile
                    friend_ids = profile.friends.all().values_list('user__id', flat=True)
                    excluded_user_ids.update(friend_ids)

                    fs1 = Friendship.objects.filter(from_user=profile).values_list('to_user__user__id', flat=True)
                    fs2 = Friendship.objects.filter(to_user=profile).values_list('from_user__user__id', flat=True)
                    excluded_user_ids.update(fs1)
                    excluded_user_ids.update(fs2)
            except Exception:
                pass

        # Candidate neutral jurors
        candidate_users = list(User.objects.filter(is_active=True).exclude(id__in=excluded_user_ids).order_by('id'))

        if len(candidate_users) >= panel_size:
            selected_jurors = random.sample(candidate_users, panel_size)
        else:
            selected_jurors = candidate_users

        deadline = timezone.now() + timedelta(days=3)
        jury_panel, created = cls.objects.get_or_create(
            dispute=dispute,
            defaults={
                'status': 'voting',
                'voting_deadline': deadline
            }
        )
        if not created:
            jury_panel.status = 'voting'
            jury_panel.voting_deadline = deadline
            jury_panel.save()

        jury_panel.jurors.set(selected_jurors)

        # Send notifications
        from django.urls import reverse
        dispute_link = reverse('dispute_detail', args=[dispute.id])
        for juror in selected_jurors:
            Notification.objects.create(
                recipient=juror,
                message=f"You have been selected as an impartial juror for dispute on task: '{task.title}'.",
                link=dispute_link
            )
        return jury_panel

    def is_juror(self, user):
        if not user or not user.is_authenticated:
            return False
        return self.jurors.filter(id=user.id).exists()

    def has_voted(self, user):
        if not user or not user.is_authenticated:
            return False
        return self.votes.filter(juror=user).exists()

    def get_vote(self, user):
        if not user or not user.is_authenticated:
            return None
        return self.votes.filter(juror=user).first()

    def tally_and_settle(self):
        if self.status == 'resolved':
            return self.dispute.status

        votes = self.votes.all()
        poster_votes = votes.filter(vote='poster').count()
        taker_votes = votes.filter(vote='taker').count()

        task = self.dispute.task
        dispute = self.dispute

        from django.db import transaction
        with transaction.atomic():
            if taker_votes > poster_votes:
                winner = 'taker'
            elif poster_votes > taker_votes:
                winner = 'poster'
            else:
                winner = 'poster' if dispute.raised_by == task.taken_by else 'taker'

            if winner == 'poster':
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                task.status = 'cancelled'
                task.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Reward points refunded via Jury Consensus on task: '{task.title}'"
                )

                if dispute.raised_by == task.posted_by:
                    dispute.refund_deposit(reason_description=f"Deposit bond refunded via Jury Consensus for task '{task.title}'")
                else:
                    dispute.forfeit_deposit(
                        beneficiary=task.posted_by,
                        reason_description=f"Deposit bond forfeited via Jury Consensus for task '{task.title}'"
                    )
            else:
                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Task reward awarded via Jury Consensus on task: '{task.title}'"
                    )

                task.status = 'completed'
                task.save()

                if dispute.raised_by == task.taken_by:
                    dispute.refund_deposit(reason_description=f"Deposit bond refunded via Jury Consensus for task '{task.title}'")
                else:
                    dispute.forfeit_deposit(
                        beneficiary=task.taken_by,
                        reason_description=f"Deposit bond forfeited via Jury Consensus for task '{task.title}'"
                    )

            # Award juror reward points to participating jurors
            JUROR_REWARD_AMOUNT = 10
            for vote_obj in votes:
                juror = vote_obj.juror
                juror_profile = juror.userprofile
                juror_profile.rewards += JUROR_REWARD_AMOUNT
                juror_profile.save()

                RewardLedger.objects.create(
                    user=juror,
                    task=task,
                    amount=JUROR_REWARD_AMOUNT,
                    transaction_type='juror_reward',
                    description=f"Reward points earned for jury duty service on task: '{task.title}'"
                )

            self.status = 'resolved'
            self.save()

            dispute.status = 'resolved'
            dispute.save()

            from django.urls import reverse
            dispute_link = reverse('dispute_detail', args=[dispute.id])
            winner_username = task.posted_by.username if winner == 'poster' else (task.taken_by.username if task.taken_by else 'Taker')

            notification_msg = f"Dispute for task '{task.title}' has been settled by Community Jury in favor of {winner_username}."

            participants = set([task.posted_by])
            if task.taken_by:
                participants.add(task.taken_by)
            for juror in self.jurors.all():
                participants.add(juror)

            for participant in participants:
                Notification.objects.create(
                    recipient=participant,
                    message=notification_msg,
                    link=dispute_link
                )

        return dispute.status


class JurorVote(models.Model):
    VOTE_CHOICES = (
        ('poster', 'In favor of Task Poster'),
        ('taker', 'In favor of Task Taker'),
    )
    panel = models.ForeignKey(JuryPanel, on_delete=models.CASCADE, related_name='votes')
    juror = models.ForeignKey(User, on_delete=models.CASCADE, related_name='juror_votes')
    vote = models.CharField(max_length=20, choices=VOTE_CHOICES)
    comments = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('panel', 'juror')

    def __str__(self):
        return f"Vote by {self.juror.username} on Dispute #{self.panel.dispute.id}: {self.vote}"
