from django.db import transaction

class ReputationService:
    BASE_SCORE = 100

    @classmethod
    def calculate_reputation_score(cls, profile):
        """
        Calculates the user's reputation score.
        Formula:
        score = BASE_SCORE (100) + (tasks_completed * 10) - (tasks_defaulted * 20) + (disputes_won * 10) - (disputes_lost * 20)
        Floor at 0.
        """
        score = (
            cls.BASE_SCORE
            + (profile.tasks_completed_count * 10)
            - (profile.tasks_defaulted_count * 20)
            + (profile.disputes_won_count * 10)
            - (profile.disputes_lost_count * 20)
        )
        return max(0, score)

    @classmethod
    def calculate_risk_level(cls, score, profile=None):
        """
        Determines the user's risk level ('LOW', 'MEDIUM', 'HIGH') based on score and history.
        - HIGH: score < 60 or defaults_and_losses >= 3 or loss ratio >= 40%
        - MEDIUM: 60 <= score < 85 or defaults_and_losses >= 1
        - LOW: score >= 85 and no defaults/losses
        """
        if profile is not None:
            defaults_and_losses = profile.tasks_defaulted_count + profile.disputes_lost_count
            total_activity = (
                profile.tasks_completed_count
                + profile.tasks_defaulted_count
                + profile.disputes_won_count
                + profile.disputes_lost_count
            )
            if score < 60 or defaults_and_losses >= 3 or (total_activity >= 3 and (defaults_and_losses / total_activity) >= 0.4):
                return 'HIGH'
            elif score < 85 or defaults_and_losses >= 1:
                return 'MEDIUM'
            else:
                return 'LOW'
        else:
            if score < 60:
                return 'HIGH'
            elif score < 85:
                return 'MEDIUM'
            else:
                return 'LOW'

    @classmethod
    def update_reputation(cls, profile):
        """
        Recalculates score and risk level for a UserProfile instance and saves it.
        """
        score = cls.calculate_reputation_score(profile)
        risk = cls.calculate_risk_level(score, profile)
        profile.reputation_score = score
        profile.risk_level = risk
        profile.save(update_fields=[
            'reputation_score',
            'risk_level',
            'tasks_completed_count',
            'tasks_defaulted_count',
            'disputes_won_count',
            'disputes_lost_count',
        ])
        return profile

    @classmethod
    def record_task_completion(cls, profile):
        """
        Increments tasks_completed_count and recalculates reputation.
        """
        profile.tasks_completed_count += 1
        return cls.update_reputation(profile)

    @classmethod
    def record_task_default(cls, profile):
        """
        Increments tasks_defaulted_count and recalculates reputation.
        """
        profile.tasks_defaulted_count += 1
        return cls.update_reputation(profile)

    @classmethod
    def record_dispute_win(cls, profile):
        """
        Increments disputes_won_count and recalculates reputation.
        """
        profile.disputes_won_count += 1
        return cls.update_reputation(profile)

    @classmethod
    def record_dispute_loss(cls, profile):
        """
        Increments disputes_lost_count and recalculates reputation.
        """
        profile.disputes_lost_count += 1
        return cls.update_reputation(profile)

    @classmethod
    def record_dispute_resolution(cls, winner_profile, loser_profile):
        """
        Atomically updates win/loss stats for both parties in a dispute resolution.
        """
        cls.record_dispute_win(winner_profile)
        cls.record_dispute_loss(loser_profile)
