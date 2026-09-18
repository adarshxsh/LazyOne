from django.contrib import admin
from django.db import transaction
from django.contrib import messages
from .models import Dispute, Task, RewardLedger, UserProfile, Notification


@admin.action(description="Force-resolve selected disputes (Refund Poster)")
def force_resolve_refund_poster(modeladmin, request, queryset):
    updated_count = 0
    for dispute in queryset:
        if dispute.status != 'open':
            continue
        task = dispute.task
        with transaction.atomic():
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Admin Dispute Resolution: Refund for task '{task.title}'"
            )

            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded upon admin dispute resolution for task: '{task.title}'"
            )
            dispute.status = 'resolved'
            dispute.save()

            task.status = 'cancelled'
            task.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff moderator has resolved the dispute for task '{task.title}'. Points refunded to your balance.",
                link=f"/dispute/{dispute.id}/"
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Staff moderator has resolved the dispute for task '{task.title}' in favor of poster.",
                    link=f"/dispute/{dispute.id}/"
                )
            updated_count += 1
    modeladmin.message_user(
        request,
        f"Successfully force-resolved {updated_count} dispute(s) with refund to poster.",
        messages.SUCCESS
    )


@admin.action(description="Force-resolve selected disputes (Award Taker)")
def force_resolve_award_taker(modeladmin, request, queryset):
    updated_count = 0
    for dispute in queryset:
        if dispute.status != 'open' or not dispute.task.taken_by:
            continue
        task = dispute.task
        with transaction.atomic():
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += task.reward
            taker_profile.save()

            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Admin Dispute Resolution: Award for task '{task.title}'"
            )

            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded upon admin dispute resolution for task: '{task.title}'"
            )
            dispute.status = 'resolved'
            dispute.save()

            task.status = 'completed'
            task.save()

            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Staff moderator has resolved the dispute for task '{task.title}'. Points awarded to your balance.",
                link=f"/dispute/{dispute.id}/"
            )
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff moderator has resolved the dispute for task '{task.title}' in favor of taker.",
                link=f"/dispute/{dispute.id}/"
            )
            updated_count += 1
    modeladmin.message_user(
        request,
        f"Successfully force-resolved {updated_count} dispute(s) with award to taker.",
        messages.SUCCESS
    )


@admin.action(description="Freeze selected disputes")
def freeze_dispute(modeladmin, request, queryset):
    updated_count = 0
    for dispute in queryset:
        if dispute.status != 'open':
            continue
        task = dispute.task
        with transaction.atomic():
            dispute.refund_deposit(
                reason_description=f"Security deposit bond refunded upon admin freezing dispute for task: '{task.title}'"
            )
            dispute.status = 'resolved'
            dispute.save()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"Staff moderator has frozen and closed the dispute for task '{task.title}'.",
                link=f"/dispute/{dispute.id}/"
            )
            if task.taken_by:
                Notification.objects.create(
                    recipient=task.taken_by,
                    message=f"Staff moderator has frozen and closed the dispute for task '{task.title}'.",
                    link=f"/dispute/{dispute.id}/"
                )
            updated_count += 1
    modeladmin.message_user(
        request,
        f"Successfully froze {updated_count} dispute(s).",
        messages.SUCCESS
    )


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'rewards', 'college', 'hostel')
    list_filter = ('college', 'batch')
    search_fields = ('user__username', 'first_name', 'last_name', 'phone_number')


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'posted_by', 'taken_by', 'reward', 'status', 'created_at', 'deadline')
    list_filter = ('status', 'created_at')
    search_fields = ('title', 'description', 'posted_by__username', 'taken_by__username')


@admin.register(RewardLedger)
class RewardLedgerAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'amount', 'transaction_type', 'task', 'created_at')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('user__username', 'description', 'task__title')


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'raised_by', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('task__title', 'raised_by__username', 'task__posted_by__username', 'task__taken_by__username', 'reason')
    actions = [force_resolve_refund_poster, force_resolve_award_taker, freeze_dispute]
