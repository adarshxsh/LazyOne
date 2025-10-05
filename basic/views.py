from django.shortcuts import render, redirect, get_object_or_404
from firebase_admin import auth
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from .models import UserProfile, Task, FriendRequest
from django.contrib.auth.models import User, AnonymousUser
from django.contrib.auth import login, authenticate

# Create your views here.

def home(request):
    tasks = Task.objects.filter(is_taken=False)
    return render(request, 'home.html', {'tasks': tasks})

def login_view(request):
    if request.user.is_authenticated:
        return redirect('profile')

    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')

        if not email or not password:
            messages.error(request, "Please provide both email and password.")
            return render(request, 'login.html')

        user = authenticate(request, username=email, password=password)
        if user is not None:
            login(request, user)
            return redirect('profile')
        else:
            messages.error(request, "Invalid email or password.")
            return redirect('login')
    return render(request, 'login.html')


def register_view(request):
    if request.user.is_authenticated:
        return redirect('profile')

    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')

        if User.objects.filter(email=email).exists():
            messages.error(request, 'A user with this email already exists.')
            return redirect('register')

        try:
            # Create the Django user, which also handles password hashing
            django_user = User.objects.create_user(username=email, email=email, password=password)
            UserProfile.objects.create(user=django_user)
            messages.success(request, 'Successfully created user. Please log in.')
            return redirect('login')
        except Exception as e:
            messages.error(request, f'An unexpected error occurred: {e}')
            return redirect('register')
    return render(request, 'register.html')

@login_required
def profile_view(request):
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        # Update User model fields
        request.user.first_name = request.POST.get('first_name', '')
        request.user.last_name = request.POST.get('last_name', '')
        request.user.save()

        # Update UserProfile model fields
        profile.bio = request.POST.get('bio', '')
        profile.college = request.POST.get('college', '')
        profile.rollNo = request.POST.get('rollNo', '')
        profile.batch = request.POST.get('batch', 2029)
        profile.hostel = request.POST.get('hostel', '')
        if 'profile_picture' in request.FILES:
            profile.profile_picture = request.FILES['profile_picture']
        profile.save()
        messages.success(request, 'Profile updated successfully.')
        return redirect('profile')
    return render(request, 'profile.html', {'profile': profile})

@login_required
def add_task(request):
    if request.method == 'POST':
        title = request.POST.get('title')
        description = request.POST.get('description')
        incentive = request.POST.get('incentive')
        Task.objects.create(
            title=title,
            description=description,
            incentive=incentive,
            posted_by=request.user
        )
        messages.success(request, 'Task added successfully.')
        return redirect('home') # Or wherever you want to redirect after adding a task
    return render(request, 'add_task.html')

@login_required
def take_task(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if not task.is_taken:
        task.is_taken = True
        task.taken_by = request.user
        task.save()
        messages.success(request, 'Task has been assigned to you.')
    else:
        messages.error(request, 'This task has already been taken.')
    return redirect('home')

@login_required
def user_list(request):
    users = UserProfile.objects.exclude(user=request.user)
    return render(request, 'user_list.html', {'users': users})

@login_required
def friends_view(request):
    user_profile = UserProfile.objects.get(user=request.user)
    friends = user_profile.friends.all()
    friend_requests = FriendRequest.objects.filter(to_user=request.user, is_accepted=False)
    return render(request, 'friends.html', {'friends': friends, 'friend_requests': friend_requests})

@login_required
def send_friend_request(request, user_id):
    to_user = get_object_or_404(User, id=user_id)
    friend_request, created = FriendRequest.objects.get_or_create(
        from_user=request.user,
        to_user=to_user
    )
    if created:
        messages.success(request, 'Friend request sent.')
    else:
        messages.info(request, 'Friend request already sent.')
    return redirect('user_list')

@login_required
def accept_friend_request(request, request_id):
    friend_request = get_object_or_404(FriendRequest, id=request_id)
    if friend_request.to_user == request.user:
        friend_request.is_accepted = True
        friend_request.save()
        from_user_profile = UserProfile.objects.get(user=friend_request.from_user)
        to_user_profile = UserProfile.objects.get(user=request.user)
        from_user_profile.friends.add(to_user_profile)
        to_user_profile.friends.add(from_user_profile)
        messages.success(request, 'Friend request accepted.')
    else:
        messages.error(request, 'Invalid request.')
    return redirect('friends')

@login_required
def decline_friend_request(request, request_id):
    friend_request = get_object_or_404(FriendRequest, id=request_id)
    if friend_request.to_user == request.user:
        friend_request.delete()
        messages.success(request, 'Friend request declined.')
    else:
        messages.error(request, 'Invalid request.')
    return redirect('friends')
