from rest_framework import serializers

from basic.models import Dispute, Task
from basic.services import disputes, marketplace


class TaskSerializer(serializers.ModelSerializer):
    posted_by = serializers.CharField(source='posted_by.username', read_only=True)
    taken_by = serializers.CharField(source='taken_by.username', read_only=True, default=None)
    allowed_actions = serializers.SerializerMethodField()
    conversation_id = serializers.SerializerMethodField()
    active_dispute_id = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = ['public_id', 'title', 'description', 'reward', 'status', 'wave_policy', 'posted_by', 'taken_by',
                  'deadline', 'opened_at', 'submitted_at', 'submission_note', 'cancellation_requested', 'version',
                  'allowed_actions', 'conversation_id', 'active_dispute_id']

    def get_allowed_actions(self, task):
        user = self.context['request'].user
        hidden = {'expire', 'auto_approve', 'verdict_taker', 'verdict_poster', 'withdraw_dispute', 'dispute'}
        return [a for a in marketplace.allowed_actions(task, user) if a not in hidden]

    def get_conversation_id(self, task):
        user = self.context['request'].user
        conv = getattr(task, 'conversation', None) if hasattr(task, 'conversation') else None
        if conv and user.pk in (task.posted_by_id, task.taken_by_id):
            return conv.public_id
        return None

    def get_active_dispute_id(self, task):
        if task.status != Task.Status.DISPUTED:
            return None
        d = task.dispute
        return d.public_id if d else None


class TaskCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(allow_blank=True, required=False, default='')
    reward = serializers.IntegerField(min_value=1)
    deadline = serializers.DateTimeField(required=False, allow_null=True)
    wave_policy = serializers.ChoiceField(choices=Task.WavePolicy.choices, default=Task.WavePolicy.DEFAULT)


class DisputeSerializer(serializers.ModelSerializer):
    task = serializers.CharField(source='task.public_id', read_only=True)
    raised_by = serializers.CharField(source='raised_by.username', read_only=True, default=None)
    evidence = serializers.SerializerMethodField()
    my_vote = serializers.SerializerMethodField()
    is_juror = serializers.SerializerMethodField()
    tally = serializers.SerializerMethodField()

    class Meta:
        model = Dispute
        fields = ['public_id', 'task', 'raised_by', 'reason', 'dispute_type', 'status', 'outcome', 'bond_amount',
                  'evidence_deadline', 'voting_deadline', 'created_at', 'resolved_at',
                  'evidence', 'is_juror', 'my_vote', 'tally']

    def get_evidence(self, d):
        return [{'by': e.submitted_by.username if e.submitted_by else None, 'text': e.text, 'at': e.created_at}
                for e in d.evidence.select_related('submitted_by')]

    def _seat(self, d):
        user = self.context['request'].user
        return d.jurors.filter(juror=user).first()

    def get_is_juror(self, d):
        return self._seat(d) is not None

    def get_my_vote(self, d):
        seat = self._seat(d)
        return seat.vote or None if seat else None

    def get_tally(self, d):
        """Votes are revealed only after the dispute is closed."""
        if d.is_active:
            return {'jurors': d.jurors.count(), 'votes_cast': d.jurors.exclude(vote='').count()}
        return {'taker': d.jurors.filter(vote='taker').count(), 'poster': d.jurors.filter(vote='poster').count()}


__all__ = ['TaskSerializer', 'TaskCreateSerializer', 'DisputeSerializer', 'disputes']
