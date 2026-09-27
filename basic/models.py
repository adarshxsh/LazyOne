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
        ('juror_stake', 'Juror Stake Held'),
        ('juror_slash', 'Juror Stake Slashed'),
        ('juror_reward', 'Juror Reward & Stake Refunded'),
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
    COUNTER_BOND_STATUS_CHOICES = (
        ('pending', 'Pending Counter-Bond'),
        ('posted', 'Counter-Bond Posted'),
        ('expired', 'Counter-Bond Expired'),
    )
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name='dispute')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='raised_disputes')
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    deposit_amount = models.PositiveIntegerField(default=0)
    poster_deposit_amount = models.PositiveIntegerField(default=0)
    counter_bond_status = models.CharField(max_length=20, choices=COUNTER_BOND_STATUS_CHOICES, default='pending')
    counter_bond_deadline = models.DateTimeField(null=True, blank=True)
    escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='held')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    def check_counter_bond_timeout(self):
        """
        If counter-bond is pending and counter_bond_deadline has passed, auto-resolves for the dispute creator.
        """
        if self.status == 'open' and self.counter_bond_status == 'pending':
            if self.counter_bond_deadline and timezone.now() >= self.counter_bond_deadline:
                from django.db import transaction
                with transaction.atomic():
                    self.counter_bond_status = 'expired'
                    self.status = 'resolved'
                    self.save()

                    # Refund creator deposit bond
                    self.refund_deposit(
                        reason_description=f"Security deposit bond refunded on auto-resolved dispute for task: '{self.task.title}'"
                    )

                    # Award task reward to creator
                    creator = self.raised_by
                    creator_profile = creator.userprofile
                    creator_profile.rewards += self.task.reward
                    creator_profile.save()

                    RewardLedger.objects.create(
                        user=creator,
                        task=self.task,
                        amount=self.task.reward,
                        transaction_type='task_completion',
                        description=f"Awarded reward for auto-resolved dispute (poster failed to counter-bond) on task: '{self.task.title}'"
                    )

                    self.task.status = 'completed'
                    self.task.save()

                    # Create notifications
                    Notification.objects.create(
                        recipient=self.raised_by,
                        message=f"Dispute for task '{self.task.title}' auto-resolved in your favor because task poster failed to post counter-bond."
                    )
                    Notification.objects.create(
                        recipient=self.task.posted_by,
                        message=f"Dispute for task '{self.task.title}' auto-resolved against you because counter-bond window expired."
                    )
                return True
        return False

    def resolve_dispute_with_jury(self):
        """
        Tallies juror votes and settles dispute atomically:
        - Refunds winning principal deposit + awards losing principal deposit + awards task reward/cancellation refund.
        - Slashes minority juror stakes and distributes them equally among majority jurors along with their stake refunds.
        """
        from django.db import transaction
        if self.status != 'open':
            return

        with transaction.atomic():
            worker_votes = self.votes.filter(vote='worker')
            poster_votes = self.votes.filter(vote='poster')

            w_count = worker_votes.count()
            p_count = poster_votes.count()

            if w_count > p_count:
                winning_side = 'worker'
            elif p_count > w_count:
                winning_side = 'poster'
            else:
                winning_side = 'tie'

            if winning_side == 'worker':
                winner = self.task.taken_by
                loser = self.task.posted_by

                # Worker gets deposit back
                self.refund_deposit(
                    reason_description=f"Security deposit bond refunded for winning dispute on task: '{self.task.title}'"
                )

                # Worker gets task reward
                if winner:
                    winner_profile = winner.userprofile
                    winner_profile.rewards += self.task.reward
                    winner_profile.save()
                    RewardLedger.objects.create(
                        user=winner,
                        task=self.task,
                        amount=self.task.reward,
                        transaction_type='task_completion',
                        description=f"Task reward awarded for winning dispute on task: '{self.task.title}'"
                    )

                # Worker gets poster's forfeited deposit bond
                if winner and self.poster_deposit_amount > 0:
                    winner_profile = winner.userprofile
                    winner_profile.rewards += self.poster_deposit_amount
                    winner_profile.save()
                    RewardLedger.objects.create(
                        user=winner,
                        task=self.task,
                        amount=self.poster_deposit_amount,
                        transaction_type='dispute_refund',
                        description=f"Forfeited poster counter-bond awarded for winning dispute on task: '{self.task.title}'"
                    )

                if loser and self.poster_deposit_amount > 0:
                    RewardLedger.objects.create(
                        user=loser,
                        task=self.task,
                        amount=0,
                        transaction_type='dispute_forfeit',
                        description=f"Security counter-bond forfeited for losing dispute on task: '{self.task.title}'"
                    )

                self.task.status = 'completed'
                self.task.save()

                majority_votes = worker_votes
                minority_votes = poster_votes

            elif winning_side == 'poster':
                winner = self.task.posted_by
                loser = self.task.taken_by

                # Poster gets counter-bond back
                if winner and self.poster_deposit_amount > 0:
                    winner_profile = winner.userprofile
                    winner_profile.rewards += self.poster_deposit_amount
                    winner_profile.save()
                    RewardLedger.objects.create(
                        user=winner,
                        task=self.task,
                        amount=self.poster_deposit_amount,
                        transaction_type='dispute_refund',
                        description=f"Security counter-bond refunded for winning dispute on task: '{self.task.title}'"
                    )

                # Poster gets task reward points refunded (task cancellation)
                if winner:
                    winner_profile = winner.userprofile
                    winner_profile.rewards += self.task.reward
                    winner_profile.save()
                    RewardLedger.objects.create(
                        user=winner,
                        task=self.task,
                        amount=self.task.reward,
                        transaction_type='task_cancellation',
                        description=f"Task reward points refunded for winning dispute on task: '{self.task.title}'"
                    )

                # Poster gets worker's forfeited deposit bond
                if winner and self.deposit_amount > 0:
                    winner_profile = winner.userprofile
                    winner_profile.rewards += self.deposit_amount
                    winner_profile.save()
                    RewardLedger.objects.create(
                        user=winner,
                        task=self.task,
                        amount=self.deposit_amount,
                        transaction_type='dispute_refund',
                        description=f"Forfeited worker deposit bond awarded for winning dispute on task: '{self.task.title}'"
                    )

                if self.escrow_status == 'held' and self.deposit_amount > 0:
                    RewardLedger.objects.create(
                        user=self.raised_by,
                        task=self.task,
                        amount=0,
                        transaction_type='dispute_forfeit',
                        description=f"Security deposit bond forfeited for losing dispute on task: '{self.task.title}'"
                    )
                    self.escrow_status = 'forfeited'

                self.task.status = 'cancelled'
                self.task.save()

                majority_votes = poster_votes
                minority_votes = worker_votes

            else:  # tie or no votes
                # Refund both principal deposit bonds
                self.refund_deposit(
                    reason_description=f"Security deposit bond refunded on tie dispute resolution for task: '{self.task.title}'"
                )
                if self.poster_deposit_amount > 0 and self.task.posted_by:
                    poster_profile = self.task.posted_by.userprofile
                    poster_profile.rewards += self.poster_deposit_amount
                    poster_profile.save()
                    RewardLedger.objects.create(
                        user=self.task.posted_by,
                        task=self.task,
                        amount=self.poster_deposit_amount,
                        transaction_type='dispute_refund',
                        description=f"Security counter-bond refunded on tie dispute resolution for task: '{self.task.title}'"
                    )

                # Refund task reward to poster and cancel task
                poster_profile = self.task.posted_by.userprofile
                poster_profile.rewards += self.task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=self.task.posted_by,
                    task=self.task,
                    amount=self.task.reward,
                    transaction_type='task_cancellation',
                    description=f"Task reward refunded on tie dispute resolution for task: '{self.task.title}'"
                )
                self.task.status = 'cancelled'
                self.task.save()

                majority_votes = self.votes.all()
                minority_votes = self.votes.none()

            # Process Juror Rewards & Slashes
            if winning_side != 'tie':
                total_slashed = sum(v.stake_amount for v in minority_votes)
                for min_vote in minority_votes:
                    RewardLedger.objects.create(
                        user=min_vote.voter,
                        task=self.task,
                        amount=0,
                        transaction_type='juror_slash',
                        description=f"Juror stake ({min_vote.stake_amount} pts) slashed for minority vote on dispute: '{self.task.title}'"
                    )

                maj_count = majority_votes.count()
                if maj_count > 0:
                    reward_share = total_slashed // maj_count
                    for maj_vote in majority_votes:
                        total_payout = maj_vote.stake_amount + reward_share
                        juror_profile = maj_vote.voter.userprofile
                        juror_profile.rewards += total_payout
                        juror_profile.save()

                        RewardLedger.objects.create(
                            user=maj_vote.voter,
                            task=self.task,
                            amount=total_payout,
                            transaction_type='juror_reward',
                            description=f"Juror stake returned ({maj_vote.stake_amount} pts) plus reward share ({reward_share} pts) for majority vote on task: '{self.task.title}'"
                        )
            else:
                # Tie: refund all jurors their stakes
                for v in majority_votes:
                    juror_profile = v.voter.userprofile
                    juror_profile.rewards += v.stake_amount
                    juror_profile.save()
                    RewardLedger.objects.create(
                        user=v.voter,
                        task=self.task,
                        amount=v.stake_amount,
                        transaction_type='juror_reward',
                        description=f"Juror stake returned ({v.stake_amount} pts) on tie dispute for task: '{self.task.title}'"
                    )

            self.status = 'resolved'
            self.save()

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

class DisputeVote(models.Model):
    VOTE_CHOICES = (
        ('worker', 'In Favor of Worker'),
        ('poster', 'In Favor of Task Poster'),
    )
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    voter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes')
    vote = models.CharField(max_length=10, choices=VOTE_CHOICES)
    stake_amount = models.PositiveIntegerField(default=25)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'voter')

    def __str__(self):
        return f"Vote by {self.voter.username} on dispute '{self.dispute.task.title}': {self.vote}"

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
