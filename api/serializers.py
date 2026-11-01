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
    offers = serializers.SerializerMethodField()
    my_vote = serializers.SerializerMethodField()
    is_juror = serializers.SerializerMethodField()
    tally = serializers.SerializerMethodField()
    can_appeal = serializers.SerializerMethodField()

    class Meta:
        model = Dispute
        fields = ['public_id', 'task', 'raised_by', 'reason', 'dispute_type', 'status', 'stage', 'round',
                  'outcome', 'outcome_pct', 'bond_amount', 'appeal_bond', 'settlement_deadline',
                  'evidence_deadline', 'voting_deadline', 'appeal_deadline', 'created_at', 'resolved_at',
                  'offers', 'evidence', 'is_juror', 'my_vote', 'tally', 'can_appeal']

    def get_evidence(self, d):
        return [{'by': e.submitted_by.username if e.submitted_by else None, 'text': e.text, 'at': e.created_at}
                for e in d.evidence.select_related('submitted_by')]

    def get_offers(self, d):
        return [{'by': o.proposed_by.username if o.proposed_by else None, 'taker_pct': o.taker_pct,
                 'message': o.message, 'status': o.status, 'at': o.created_at}
                for o in d.offers.select_related('proposed_by')]

    def _seat(self, d):
        return d.jurors.filter(juror=self.context['request'].user).order_by('-round').first()

    def get_is_juror(self, d):
        return self._seat(d) is not None

    def get_my_vote(self, d):
        seat = self._seat(d)
        return {'taker_pct': seat.taker_pct, 'reason': seat.reason, 'round': seat.round} if seat and seat.has_voted else None

    def get_tally(self, d):
        """Votes of the current round are revealed only after that round closes."""
        seats = d.jurors.filter(round=d.round)
        if d.is_active and d.stage in ('jury', 'appeal'):
            return {'jurors': seats.count(), 'votes_cast': seats.filter(taker_pct__isnull=False).count()}
        return {'votes': [{'round': s.round, 'taker_pct': s.taker_pct, 'reason': s.reason}
                          for s in d.jurors.filter(taker_pct__isnull=False).order_by('round', 'id')]}

    def get_can_appeal(self, d):
        return disputes.can_appeal(self.context['request'].user, d)


__all__ = ['TaskSerializer', 'TaskCreateSerializer', 'DisputeSerializer', 'disputes']
