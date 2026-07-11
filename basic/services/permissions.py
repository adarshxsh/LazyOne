class DisputeService:
    @staticmethod
    def can_view(user, dispute):
        """
        Anyone who is authenticated can view a dispute.
        """
        return user.is_authenticated

    @staticmethod
    def can_withdraw(user, dispute):
        """
        Only the person who raised the dispute can withdraw it.
        """
        return user == dispute.raised_by

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
