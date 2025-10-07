from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import UserProfile, Task
from django.contrib.auth.models import User
import json
from django.http import JsonResponse
from firebase_admin import auth

@login_required(login_url='/login/')
def profile_view(request):
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        profile.first_name = request.POST.get('first_name', '')
        profile.last_name = request.POST.get('last_name', '')
        profile.bio = request.POST.get('bio', '')
        profile.college = request.POST.get('college', '')
        profile.major = request.POST.get('major', '')
        profile.roll_no = request.POST.get('roll_no', '')
        profile.batch = request.POST.get('batch', 2029)
        profile.phone_number = request.POST.get('phone_number', '')
        profile.instagram_username = request.POST.get('instagram_username', '')
        profile.save()
        messages.success(request, 'Profile updated successfully.')
        return redirect('profile')
    return render(request, 'profile.html', {'profile': profile})

@login_required(login_url='/login/')
def user_profile_view(request, user_id):
    viewed_user = get_object_or_404(User, id=user_id)
    viewed_profile = get_object_or_404(UserProfile, user=viewed_user)
    
    posted_tasks = Task.objects.filter(posted_by=viewed_user).order_by('-created_at')
    user_friends = viewed_profile.friends.all()

    context = {
        'viewed_profile': viewed_profile,
        'posted_tasks': posted_tasks,
        'user_friends': user_friends
    }
    return render(request, 'user_profile.html', context)

@login_required(login_url='/login/')
def verify_phone_token(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            id_token = data.get('token')

            if not id_token:
                return JsonResponse({'success': False, 'error': 'No token provided.'}, status=400)

            decoded_token = auth.verify_id_token(id_token)
            firebase_phone_number = decoded_token.get('phone_number')

            user_profile = request.user.userprofile

            if user_profile.phone_number == firebase_phone_number:
                user_profile.is_phone_verified = True
                user_profile.save()
                return JsonResponse({'success': True})
            else:
                return JsonResponse({'success': False, 'error': 'Phone number mismatch.'}, status=400)

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON.'}, status=400)
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)
