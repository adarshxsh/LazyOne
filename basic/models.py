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
        ('juror_stake', 'Juror Voting Stake Held'),
        ('juror_slash', 'Juror Voting Stake Slashed'),
        ('juror_refund', 'Juror Stake Refunded'),
        ('juror_reward', 'Juror Consensus Reward Payout'),
        ('dispute_reward', 'Counterparty Forfeited Bond Payout'),
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
    JUROR_STAKE_AMOUNT = 20

    STATUS_CHOICES = (
        ('open', 'Open'),
        ('resolved', 'Resolved'),
    )
    ESCROW_STATUS_CHOICES = (
        ('none', 'None'),
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
    
    poster_deposit_amount = models.PositiveIntegerField(default=0)
    worker_deposit_amount = models.PositiveIntegerField(default=0)
    poster_escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='none')
    worker_escrow_status = models.CharField(max_length=20, choices=ESCROW_STATUS_CHOICES, default='none')
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Dispute for task: {self.task.title}"

    def is_contested(self):
        return self.poster_escrow_status == 'held' and self.worker_escrow_status == 'held'

    def refund_deposit(self, reason_description=None):
        from django.db import transaction
        with transaction.atomic():
            task = self.task
            for user, amount, status_attr in [
                (task.posted_by, self.poster_deposit_amount, 'poster_escrow_status'),
                (task.taken_by, self.worker_deposit_amount, 'worker_escrow_status'),
            ]:
                if getattr(self, status_attr) == 'held' and amount > 0 and user:
                    user_profile = user.userprofile
                    user_profile.rewards += amount
                    user_profile.save()

                    desc = reason_description or f"Security deposit bond refunded for dispute on task: '{task.title}'"
                    RewardLedger.objects.create(
                        user=user,
                        task=task,
                        amount=amount,
                        transaction_type='dispute_refund',
                        description=desc
                    )
                    setattr(self, status_attr, 'refunded')

            if self.escrow_status == 'held' and self.deposit_amount > 0 and self.raised_by and self.poster_escrow_status == 'none' and self.worker_escrow_status == 'none':
                user_profile = self.raised_by.userprofile
                user_profile.rewards += self.deposit_amount
                user_profile.save()
                desc = reason_description or f"Security deposit bond refunded for dispute on task: '{task.title}'"
                RewardLedger.objects.create(
                    user=self.raised_by,
                    task=task,
                    amount=self.deposit_amount,
                    transaction_type='dispute_refund',
                    description=desc
                )
            self.escrow_status = 'refunded'
            self.save()

    def forfeit_deposit(self, beneficiary=None, reason_description=None):
        if beneficiary:
            self.settle_dispute(winner=beneficiary)
        else:
            self.refund_deposit(reason_description=reason_description)

    def settle_dispute(self, winner, resolved_by_admin=False):
        if self.status == 'resolved':
            return

        from django.db import transaction
        with transaction.atomic():
            task = self.task
            poster = task.posted_by
            worker = task.taken_by
            loser = poster if winner == worker else worker

            # 1. Handle winner deposit bond refund
            if winner == poster and self.poster_escrow_status == 'held' and self.poster_deposit_amount > 0:
                winner_profile = winner.userprofile
                winner_profile.rewards += self.poster_deposit_amount
                winner_profile.save()
                RewardLedger.objects.create(
                    user=winner,
                    task=task,
                    amount=self.poster_deposit_amount,
                    transaction_type='dispute_refund',
                    description=f"Security deposit bond refunded for winning dispute on task: '{task.title}'"
                )
                self.poster_escrow_status = 'refunded'
            elif winner == worker and self.worker_escrow_status == 'held' and self.worker_deposit_amount > 0:
                winner_profile = winner.userprofile
                winner_profile.rewards += self.worker_deposit_amount
                winner_profile.save()
                RewardLedger.objects.create(
                    user=winner,
                    task=task,
                    amount=self.worker_deposit_amount,
                    transaction_type='dispute_refund',
                    description=f"Security deposit bond refunded for winning dispute on task: '{task.title}'"
                )
                self.worker_escrow_status = 'refunded'
            elif self.escrow_status == 'held' and self.raised_by == winner and self.deposit_amount > 0:
                winner_profile = winner.userprofile
                winner_profile.rewards += self.deposit_amount
                winner_profile.save()
                RewardLedger.objects.create(
                    user=winner,
                    task=task,
                    amount=self.deposit_amount,
                    transaction_type='dispute_refund',
                    description=f"Security deposit bond refunded for winning dispute on task: '{task.title}'"
                )

            # 2. Handle loser deposit bond forfeit
            loser_bond = 0
            if loser == poster and self.poster_escrow_status == 'held':
                loser_bond = self.poster_deposit_amount
                self.poster_escrow_status = 'forfeited'
                RewardLedger.objects.create(
                    user=loser,
                    task=task,
                    amount=0,
                    transaction_type='dispute_forfeit',
                    description=f"Security deposit bond forfeited for losing dispute on task: '{task.title}'"
                )
            elif loser == worker and self.worker_escrow_status == 'held':
                loser_bond = self.worker_deposit_amount
                self.worker_escrow_status = 'forfeited'
                RewardLedger.objects.create(
                    user=loser,
                    task=task,
                    amount=0,
                    transaction_type='dispute_forfeit',
                    description=f"Security deposit bond forfeited for losing dispute on task: '{task.title}'"
                )
            elif self.escrow_status == 'held' and self.raised_by == loser and self.deposit_amount > 0:
                loser_bond = self.deposit_amount
                RewardLedger.objects.create(
                    user=loser,
                    task=task,
                    amount=0,
                    transaction_type='dispute_forfeit',
                    description=f"Security deposit bond forfeited for losing dispute on task: '{task.title}'"
                )

            # 3. Juror votes evaluation
            votes = list(self.votes.all())
            majority_votes = [v for v in votes if v.voted_for == winner]
            minority_votes = [v for v in votes if v.voted_for != winner]

            # Slash minority jurors
            for mv in minority_votes:
                RewardLedger.objects.create(
                    user=mv.voter,
                    task=task,
                    amount=0,
                    transaction_type='juror_slash',
                    description=f"Juror stake slashed for non-consensus vote on task: '{task.title}'"
                )

            minority_slashed_total = sum(mv.stake_amount for mv in minority_votes)

            # Award winner percentage of counterparty bond
            winner_bond_share = 0
            if loser_bond > 0:
                if len(majority_votes) > 0:
                    winner_bond_share = math.floor(loser_bond * 0.50)
                else:
                    winner_bond_share = loser_bond

                if winner_bond_share > 0:
                    winner_profile = winner.userprofile
                    winner_profile.rewards += winner_bond_share
                    winner_profile.save()
                    RewardLedger.objects.create(
                        user=winner,
                        task=task,
                        amount=winner_bond_share,
                        transaction_type='dispute_reward',
                        description=f"Forfeited counterparty deposit bond share awarded for winning dispute on task: '{task.title}'"
                    )

            # Pool remaining slashed funds for majority consensus jurors
            remaining_loser_bond = loser_bond - winner_bond_share
            slashed_pool = remaining_loser_bond + minority_slashed_total

            if len(majority_votes) > 0:
                reward_per_juror = math.floor(slashed_pool / len(majority_votes))
                remainder = slashed_pool - (reward_per_juror * len(majority_votes))

                for idx, mv in enumerate(majority_votes):
                    bonus = reward_per_juror + (remainder if idx == 0 else 0)
                    total_payout = mv.stake_amount + bonus
                    juror_profile = mv.voter.userprofile
                    juror_profile.rewards += total_payout
                    juror_profile.save()

                    RewardLedger.objects.create(
                        user=mv.voter,
                        task=task,
                        amount=mv.stake_amount,
                        transaction_type='juror_refund',
                        description=f"Juror stake returned for consensus vote on task: '{task.title}'"
                    )
                    if bonus > 0:
                        RewardLedger.objects.create(
                            user=mv.voter,
                            task=task,
                            amount=bonus,
                            transaction_type='juror_reward',
                            description=f"Consensus juror reward share awarded for dispute on task: '{task.title}'"
                        )
            else:
                # If no majority jurors and there is leftover slashed pool (e.g. from minority slashed stakes), award to winner
                if slashed_pool > 0:
                    winner_profile = winner.userprofile
                    winner_profile.rewards += slashed_pool
                    winner_profile.save()
                    RewardLedger.objects.create(
                        user=winner,
                        task=task,
                        amount=slashed_pool,
                        transaction_type='dispute_reward',
                        description=f"Slashed funds pool awarded for winning dispute on task: '{task.title}'"
                    )

            # 4. Finalize Task Status
            if winner == worker and worker is not None:
                worker_profile = worker.userprofile
                worker_profile.rewards += task.reward
                worker_profile.save()
                RewardLedger.objects.create(
                    user=worker,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Completed task reward awarded upon dispute settlement: '{task.title}'"
                )
                task.status = 'completed'
            elif winner == poster:
                poster_profile = poster.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=poster,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Task reward refunded upon dispute settlement: '{task.title}'"
                )
                task.status = 'cancelled'

            task.save()
            self.status = 'resolved'
            self.escrow_status = 'refunded'
            self.save()

class DisputeVote(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name='votes')
    voter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes')
    voted_for = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dispute_votes_received')
    stake_amount = models.PositiveIntegerField(default=20)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('dispute', 'voter')

    def __str__(self):
        return f"Vote by {self.voter.username} for {self.voted_for.username} on {self.dispute}"

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
