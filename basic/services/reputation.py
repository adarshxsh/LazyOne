from django.db import transaction
from ..models import UserProfile, ReputationLog, RewardLedger

class ReputationService:
    @staticmethod
    def calculate_risk_tier(reputation_score: int) -> str:
        if reputation_score >= 80:
            return 'low'
        elif reputation_score >= 60:
            return 'medium'
        elif reputation_score >= 40:
            return 'high'
        else:
            return 'critical'

    @staticmethod
    def get_poster_collateral_multiplier(risk_tier: str) -> float:
        multipliers = {
            'low': 1.0,
            'medium': 1.2,
            'high': 1.5,
            'critical': 2.0,
        }
        return multipliers.get(risk_tier, 1.0)

    @staticmethod
    def get_taker_required_collateral(user_profile: UserProfile, task_reward: int) -> int:
        """
        Calculates mandatory security point deposit (collateral) for task taker based on risk tier.
        """
        multipliers = {
            'low': 0.0,
            'medium': 0.1,  # 10% collateral deposit
            'high': 0.25,   # 25% collateral deposit
            'critical': 0.5 # 50% collateral deposit
        }
        mult = multipliers.get(user_profile.risk_tier, 0.0)
        return int(task_reward * mult)

    @staticmethod
    def can_take_task(user_profile: UserProfile, task_reward: int) -> tuple[bool, str]:
        """
        Checks taker reputation score against task reward tier and blocks claims if risk thresholds are exceeded.
        """
        score = user_profile.reputation_score
        tier = user_profile.risk_tier

        if score < 20:
            return False, f"Your reputation score ({score}) is too low to claim tasks at this time."
        if tier == 'critical' and task_reward > 200:
            return False, f"Your reputation score ({score}) is in the Critical Risk tier. You cannot claim tasks with reward greater than 200 points."
        if tier == 'high' and task_reward > 500:
            return False, f"Your reputation score ({score}) is in the High Risk tier. You cannot claim tasks with reward greater than 500 points."
        if tier == 'medium' and task_reward > 1500:
            return False, f"Your reputation score ({score}) is in the Medium Risk tier. You cannot claim tasks with reward greater than 1500 points."

        return True, ""

    @classmethod
    def update_reputation(cls, profile: UserProfile, score_delta: int, reason: str, moderator=None) -> UserProfile:
        """
        Updates user reputation score, clamps to [0, 100], updates risk tier, and creates audit log.
        MUST be called inside transaction.atomic().
        """
        old_score = profile.reputation_score
        new_score = max(0, min(100, old_score + score_delta))
        actual_change = new_score - old_score

        profile.reputation_score = new_score
        profile.risk_tier = cls.calculate_risk_tier(new_score)
        profile.save()

        ReputationLog.objects.create(
            user=profile.user,
            change=actual_change,
            new_score=new_score,
            reason=reason,
            created_by=moderator
        )
        return profile

    @classmethod
    def record_task_completion(cls, doer_profile: UserProfile):
        """
        Increments tasks_completed and recalibrates reputation (+5 points).
        """
        doer_profile.tasks_completed += 1
        doer_profile.save()
        cls.update_reputation(doer_profile, 5, "Task Completed")

    @classmethod
    def record_task_abandonment(cls, doer_profile: UserProfile):
        """
        Increments tasks_abandoned and deducts reputation points (-15 points).
        """
        doer_profile.tasks_abandoned += 1
        doer_profile.save()
        cls.update_reputation(doer_profile, -15, "Task Abandoned")

    @classmethod
    def record_dispute_raised(cls, raiser_profile: UserProfile):
        """
        Increments disputes_raised counter.
        """
        raiser_profile.disputes_raised += 1
        raiser_profile.save()

    @classmethod
    def record_dispute_won(cls, winner_profile: UserProfile):
        """
        Increments disputes_won and increases reputation score (+5 points).
        """
        winner_profile.disputes_won += 1
        winner_profile.save()
        cls.update_reputation(winner_profile, 5, "Dispute Won")

    @classmethod
    def record_dispute_lost(cls, loser_profile: UserProfile):
        """
        Increments disputes_lost and deducts reputation score (-20 points).
        """
        loser_profile.disputes_lost += 1
        loser_profile.save()
        cls.update_reputation(loser_profile, -20, "Dispute Lost")
