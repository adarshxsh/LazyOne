from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import Task, Conversation, Notification, UserProfile
from django.db import transaction
from django.urls import reverse
from LazyOne.settings import db

@login_required(login_url='/login/')
def add_task(request):
    if request.method == 'POST':
        title = request.POST.get('title')
        description = request.POST.get('description')
        reward_str = request.POST.get('reward')

        try:
            reward = int(reward_str)
            if reward <= 0:
                messages.error(request, "Reward must be a positive number.")
                return render(request, 'add_task.html')

            user_profile = request.user.userprofile
            if user_profile.rewards < reward:
                messages.error(request, f"You only have {user_profile.rewards} points, not enough to offer this reward.")
                return render(request, 'add_task.html')

            with transaction.atomic():
                user_profile.rewards -= reward
                user_profile.save()

                new_task = Task.objects.create(
                    title=title,
                    description=description,
                    reward=reward,
                    posted_by=request.user
                )

                # Notify friends about the new task
                friends = user_profile.friends.all()
                for friend_profile in friends:
                    Notification.objects.create(
                        recipient=friend_profile.user,
                        message=f"{request.user.username} posted a new task: {new_task.title}",
                        link=reverse('home')  # Or a direct link to the task
                    )

                # Save to Firebase
                if db:
                    try:
                        task_ref = db.collection('tasks').document(str(new_task.id))
                        task_ref.set({
                            'title': new_task.title,
                            'description': new_task.description,
                            'reward': new_task.reward,
                            'posted_by': new_task.posted_by.username,
                            'created_at': new_task.created_at,
                            'is_taken': new_task.is_taken,
                            'is_completed': new_task.is_completed
                        })
                        messages.success(request, 'Task also saved to Firebase.')
                    except Exception as e:
                        messages.error(request, f'Error saving to Firebase: {e}')

            messages.success(request, f'Task added successfully! {reward} points have been reserved.')
            return redirect('home')
        except (ValueError, TypeError):
            messages.error(request, 'Invalid reward amount.')
            return render(request, 'add_task.html')

    return render(request, 'add_task.html')

@login_required(login_url='/login/')
def take_task(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if task.posted_by == request.user:
        messages.error(request, "You cannot take your own task.")
    elif task.is_taken:
        messages.error(request, "This task has already been taken.")
    else:
        with transaction.atomic():
            task.is_taken = True
            task.taken_by = request.user
            task.save()

            conversation, created = Conversation.objects.get_or_create(task=task)
            if created:
                conversation.participants.add(task.posted_by, task.taken_by)
                # Notify the task poster that their task has been taken
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"{request.user.username} has taken your task: {task.title}",
                    link=reverse('my_tasks') # Link to my tasks page
                )
                messages.success(request, "Task has been assigned to you and a chat has been created.")
            else:
                messages.success(request, "Task has been assigned to you.")

    return redirect('my_tasks')

@login_required(login_url='/login/')
def complete_task(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if task.posted_by != request.user:
        messages.error(request, "You are not authorized to complete this task.")
    elif not task.is_taken:
        messages.error(request, "This task has not been taken by anyone yet.")
    elif task.is_completed:
        messages.error(request, "This task has already been completed.")
    else:
        with transaction.atomic():
            task_doer_profile = task.taken_by.userprofile
            task_doer_profile.rewards += task.reward
            task_doer_profile.save()

            task.is_completed = True
            task.save()
            messages.success(request, f"Task marked as complete! {task.reward} points transferred to {task.taken_by.username}.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
def my_tasks(request):
    posted_tasks = Task.objects.filter(posted_by=request.user).order_by('-created_at')
    taken_tasks = Task.objects.filter(taken_by=request.user).order_by('-created_at')
    return render(request, 'my_tasks.html', {'posted_tasks': posted_tasks, 'taken_tasks': taken_tasks})
