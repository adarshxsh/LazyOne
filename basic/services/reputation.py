from django.db import transaction
from ..models import UserProfile, ReputationLog, RewardLedger, Dispute

class ReputationService:
    @staticmethod
    def calculate_risk_tier(reputation_score: int, disputes_won: int = 0, disputes_lost: int = 0) -> str:
        """
        Calculates risk tier ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL') based on reputation score and dispute ratio.
        Default starting score is 100 ('LOW').
        """
        if reputation_score >= 100:
            tier = 'LOW'
        elif reputation_score >= 70:
            tier = 'MEDIUM'
        elif reputation_score >= 40:
            tier = 'HIGH'
        else:
            tier = 'CRITICAL'

        total_disputes = disputes_won + disputes_lost
        if total_disputes >= 2 and (disputes_lost / total_disputes) >= 0.5:
            if tier in ['LOW', 'MEDIUM']:
                tier = 'HIGH'

        return tier

    @staticmethod
    def get_poster_collateral_multiplier(risk_tier: str) -> float:
        tier = risk_tier.upper() if risk_tier else 'LOW'
        multipliers = {
            'LOW': 1.0,
            'MEDIUM': 1.1,
            'HIGH': 1.25,
            'CRITICAL': 1.5,
        }
        return multipliers.get(tier, 1.0)

    @staticmethod
    def get_taker_required_collateral(user_profile: UserProfile, task_reward: int) -> int:
        """
        Calculates required security deposit (collateral factor) for task taker based on risk tier.
        LOW: 0%, MEDIUM: 10%, HIGH: 25%, CRITICAL: 50%.
        """
        tier = user_profile.risk_tier.upper() if user_profile.risk_tier else 'LOW'
        multipliers = {
            'LOW': 0.0,
            'MEDIUM': 0.10,
            'HIGH': 0.25,
            'CRITICAL': 0.50,
        }
        mult = multipliers.get(tier, 0.0)
        return int(task_reward * mult)

    @staticmethod
    def can_claim_task(user_profile: UserProfile, task_reward: int) -> tuple[bool, str]:
        """
        Checks if taker reputation metrics allow claiming task of given reward amount.
        """
        score = user_profile.reputation_score
        tier = user_profile.risk_tier.upper() if user_profile.risk_tier else 'LOW'

        if score < 20:
            return False, f"Your reputation score ({score}) is too low to claim tasks at this time."
        if tier == 'CRITICAL' and task_reward > 200:
            return False, f"Your account is in Critical Risk tier. You cannot claim tasks with reward greater than 200 points."
        if tier == 'HIGH' and task_reward > 500:
            return False, f"Your account is in High Risk tier. You cannot claim tasks with reward greater than 500 points."
        if tier == 'MEDIUM' and task_reward > 1500:
            return False, f"Your account is in Medium Risk tier. You cannot claim tasks with reward greater than 1500 points."

        return True, ""

    @staticmethod
    def can_raise_dispute(user_profile: UserProfile) -> tuple[bool, str]:
        """
        Enforces dispute creation limits and restrictions for high-risk accounts.
        """
        tier = user_profile.risk_tier.upper() if user_profile.risk_tier else 'LOW'
        active_disputes = Dispute.objects.filter(raised_by=user_profile.user, status='open').count()

        if tier == 'CRITICAL':
            if active_disputes >= 1:
                return False, "Accounts in Critical Risk tier can have at most 1 active dispute at a time."
            total_disputes = user_profile.disputes_won_count + user_profile.disputes_lost_count
            if total_disputes >= 3 and user_profile.dispute_win_rate < 30.0:
                return False, "Your dispute creation privileges are suspended due to elevated dispute loss rates."
        elif tier == 'HIGH':
            if active_disputes >= 2:
                return False, "Accounts in High Risk tier can have at most 2 active disputes at a time."

        return True, ""

    @staticmethod
    def is_jury_eligible(user_profile: UserProfile) -> bool:
        tier = user_profile.risk_tier.upper() if user_profile.risk_tier else 'LOW'
        return user_profile.reputation_score >= 80 and user_profile.completed_tasks_count >= 1 and tier in ['LOW', 'MEDIUM']

    @classmethod
    def update_reputation(cls, profile: UserProfile, score_delta: int, reason: str, moderator=None) -> UserProfile:
        """
        Updates user reputation score, clamps to [0, 1000], updates risk tier, and creates audit log.
        MUST be executed inside transaction.atomic().
        """
        old_score = profile.reputation_score
        new_score = max(0, min(1000, old_score + score_delta))
        actual_change = new_score - old_score

        profile.reputation_score = new_score
        profile.risk_tier = cls.calculate_risk_tier(new_score, profile.disputes_won_count, profile.disputes_lost_count)
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
        Increments completed tasks count and increases user reputation score (+10 points).
        """
        doer_profile.completed_tasks_count += 1
        doer_profile.save()
        cls.update_reputation(doer_profile, 10, "Task Completed")

    @classmethod
    def record_task_abandonment(cls, doer_profile: UserProfile):
        """
        Increments abandoned tasks count and decrements user reputation score (-25 points).
        """
        doer_profile.abandoned_tasks_count += 1
        doer_profile.save()
        cls.update_reputation(doer_profile, -25, "Task Abandoned")

    @classmethod
    def record_dispute_raised(cls, raiser_profile: UserProfile):
        """
        Increments disputes raised count.
        """
        raiser_profile.disputes_raised_count += 1
        raiser_profile.save()

    @classmethod
    def record_dispute_won(cls, winner_profile: UserProfile):
        """
        Increments disputes won count and increases reputation score (+15 points).
        """
        winner_profile.disputes_won_count += 1
        winner_profile.save()
        cls.update_reputation(winner_profile, 15, "Dispute Won")

    @classmethod
    def record_dispute_lost(cls, loser_profile: UserProfile):
        """
        Increments disputes lost count, decrements reputation score (-30 points), and recalculates risk tier.
        """
        loser_profile.disputes_lost_count += 1
        loser_profile.save()
        cls.update_reputation(loser_profile, -30, "Dispute Lost")
