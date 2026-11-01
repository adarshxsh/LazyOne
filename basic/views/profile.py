from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import UserProfile, Task
from django.contrib.auth.models import User
import json
from django.http import JsonResponse
import logging
from ..ratelimit import ratelimit

logger = logging.getLogger(__name__)

@login_required(login_url='/login/')
def profile_view(request):
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        # Update Django model
        def field(name, limit):
            return request.POST.get(name, '').strip()[:limit]
        profile.first_name = field('first_name', 50)
        profile.last_name = field('last_name', 50)
        profile.bio = field('bio', 300)
        profile.college = field('college', 100)
        profile.major = field('major', 100)
        profile.roll_no = field('roll_no', 30)
        try:
            profile.batch = max(2000, min(2100, int(request.POST.get('batch') or profile.batch or 2029)))
        except (TypeError, ValueError):
            pass
        
        # Check if phone number has changed
        new_phone_number = field('phone_number', 20)
        if new_phone_number != profile.phone_number:
            profile.phone_number = new_phone_number
            profile.is_phone_verified = False # Reset verification status

        profile.instagram_username = field('instagram_username', 30).lstrip('@')
        profile.save()

        # Profile data stays in Postgres only; it is never copied to Firestore (personal data).

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

from basic.services.phone_verification import PhoneVerificationService

from django.views.decorators.http import require_POST


@require_POST
@login_required
@ratelimit('verify_phone', '10/hour')
def verify_phone_token(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            id_token = data.get('token')

            # Delegate verification to our PhoneVerificationService
            result = PhoneVerificationService.verify_and_extract_phone(id_token)

            if result['status'] == 'success':
                phone_number = result['phone_number']
                
                # Mark as verified in Django DB
                if UserProfile.objects.filter(phone_number=phone_number, is_phone_verified=True).exclude(user=request.user).exists():
                    # One phone number verifies one account (blocks fake-account farming).
                    return JsonResponse({'success': False, 'error': 'This phone number is already verified on another account.'}, status=400)
                success = PhoneVerificationService.mark_user_phone_verified(request.user, phone_number)
                if not success:
                    return JsonResponse({'success': False, 'error': 'Failed to update user profile.'}, status=500)

                return JsonResponse({'success': True})
            else:
                return JsonResponse({'success': False, 'error': result['message']}, status=400)

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON.'}, status=400)
        except Exception:
            logger.exception("verify_phone_token failed")
            return JsonResponse({'success': False, 'error': 'Verification failed. Please try again.'}, status=500)

    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)
