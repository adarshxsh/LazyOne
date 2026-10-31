from django.contrib.auth.models import User
from basic.models import Dispute, Task, UserProfile, RewardLedger
from django.db import transaction
from django.db.models import F

class DisputeService:
    @staticmethod
    def can_view(user, dispute):
        """
        Anyone who is authenticated can view a dispute.
        """
        return user.is_authenticated

    @classmethod
    def can_withdraw(cls, user: User, dispute: Dispute) -> bool:
        """
        Only the user who raised the dispute can withdraw it.
        """
        return user == dispute.raised_by

    @classmethod
    def resolve_dispute(cls, dispute: Dispute, winner: User):
        """
        Forcefully resolves a dispute, transferring rewards to the winner.
        Currently meant to be called by admins/moderators, or future voting mechanism.
        """
        with transaction.atomic():
            dispute = Dispute.objects.select_for_update().get(pk=dispute.pk)
            task = Task.objects.select_for_update().get(pk=dispute.task_id)
            if dispute.status != Dispute.Status.OPEN or task.status != Task.Status.DISPUTED:
                raise ValueError("Can only resolve an open dispute on a disputed task.")
            if winner.id not in (task.posted_by_id, task.taken_by_id):
                raise ValueError("Winner must be the task creator or the assignee.")

            if winner.id == task.taken_by_id:
                # Assignee wins, gets the reward
                UserProfile.objects.filter(user_id=task.taken_by_id).update(rewards=F('rewards') + task.reward)
                RewardLedger.objects.create(
                    user_id=task.taken_by_id, task=task, amount=task.reward,
                    transaction_type='task_completion', description=f"Won dispute for task: '{task.title}'"
                )
                task.status = Task.Status.COMPLETED
            else:
                # Creator wins, gets refund; the task ends.
                UserProfile.objects.filter(user_id=task.posted_by_id).update(rewards=F('rewards') + task.reward)
                RewardLedger.objects.create(
                    user_id=task.posted_by_id, task=task, amount=task.reward,
                    transaction_type='task_cancellation', description=f"Won dispute refund for task: '{task.title}'"
                )
                task.status = Task.Status.CANCELLED

            task.save()
            dispute.status = Dispute.Status.RESOLVED
            dispute.save()
        return dispute

    @staticmethod
    def can_vote(user, dispute):
        """
        Anyone who is authenticated can vote, except the task creator and assignee.
        """
        if not user.is_authenticated:
            return False
        return user not in [dispute.task.posted_by, dispute.task.taken_by]


class ChatService:
    @staticmethod
    def can_read(user, conversation):
        """
        A user can read the chat if they are a participant.
        If the task is disputed, any authenticated user can read it.
        Staff/Moderators can always read chats.
        """
        if not user.is_authenticated:
            return False
            
        if user in conversation.participants.all():
            return True
            
        if conversation.task and conversation.task.status == 'disputed':
            return True
            
        return user.is_staff

    @staticmethod
    def can_send(user, conversation):
        """
        A user can send a message if they are a participant.
        If the task is disputed, staff/moderators can also send messages.
        """
        if not user.is_authenticated:
            return False
            
        if user in conversation.participants.all():
            return True
            
        if conversation.task and conversation.task.status == 'disputed':
            return user.is_staff
            
        return False
