"""
Authorization policies, shared by web views, the API and the Firestore member list.

Dispute privacy (ARCHITECTURE_V2 §4.5): a disputed task's chat and evidence are visible
to the two parties, the drawn jurors and staff - not to every logged-in user.
"""
from django.contrib.auth.models import User

from ..models import Dispute, JurorAssignment, Task
from . import disputes


class DisputeService:
    """Backwards-compatible facade over services.disputes."""

    can_view = staticmethod(disputes.can_view)
    can_withdraw = staticmethod(disputes.can_withdraw)

    @staticmethod
    def can_vote(user, dispute):
        return disputes.is_juror(user, dispute) and dispute.status == Dispute.Status.OPEN

    @staticmethod
    def resolve_dispute(dispute: Dispute, winner: User, moderator: User = None):
        return disputes.moderator_resolve(dispute, winner, moderator)


class ChatService:
    @staticmethod
    def _active_jurors(conversation):
        if not conversation.task_id:
            return JurorAssignment.objects.none()
        return JurorAssignment.objects.filter(
            dispute__task_id=conversation.task_id,
            dispute__status__in=[Dispute.Status.OPEN, Dispute.Status.ESCALATED],
        )

    @staticmethod
    def can_read(user, conversation):
        if not user.is_authenticated:
            return False
        if conversation.participants.filter(pk=user.pk).exists():
            return True
        if user.is_staff:
            return True
        return ChatService._active_jurors(conversation).filter(juror=user).exists()

    @staticmethod
    def can_send(user, conversation):
        if not user.is_authenticated:
            return False
        if conversation.participants.filter(pk=user.pk).exists():
            return True
        # Moderators may step into a disputed task's chat.
        return bool(user.is_staff and conversation.task_id and conversation.task.status == Task.Status.DISPUTED)

    @staticmethod
    def reader_ids(conversation):
        """Everyone allowed to read: participants + active jurors + (staff use the web UI)."""
        ids = set(conversation.participants.values_list('pk', flat=True))
        ids.update(ChatService._active_jurors(conversation).values_list('juror_id', flat=True))
        return ids
