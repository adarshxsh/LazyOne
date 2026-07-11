from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import UserProfile, Task
from django.contrib.auth.models import User
import json
from django.http import JsonResponse
from firebase_admin import auth
from LazyOne.settings import db # Import the Firestore client

@login_required(login_url='/login/')
def profile_view(request):
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        # Update Django model
        profile.first_name = request.POST.get('first_name', '')
        profile.last_name = request.POST.get('last_name', '')
        profile.bio = request.POST.get('bio', '')
        profile.college = request.POST.get('college', '')
        profile.major = request.POST.get('major', '')
        profile.roll_no = request.POST.get('roll_no', '')
        profile.batch = request.POST.get('batch', 2029)
        
        # Check if phone number has changed
        new_phone_number = request.POST.get('phone_number', '')
        if new_phone_number != profile.phone_number:
            profile.phone_number = new_phone_number
            profile.is_phone_verified = False # Reset verification status

        profile.instagram_username = request.POST.get('instagram_username', '')
        profile.save()

        # Update Firestore document
        if db and request.user.userprofile.firebase_uid:
            try:
                user_ref = db.collection('users').document(request.user.userprofile.firebase_uid)
                user_ref.set({
                    'username': request.user.username,
                    'first_name': profile.first_name,
                    'last_name': profile.last_name,
                    'bio': profile.bio,
                    'college': profile.college,
                    'major': profile.major,
                    'roll_no': profile.roll_no,
                    'batch': profile.batch,
                    'phone_number': profile.phone_number,
                    'is_phone_verified': profile.is_phone_verified,
                    'instagram_username': profile.instagram_username,
                }, merge=True) # merge=True prevents overwriting the whole document
                messages.success(request, 'Profile updated in Firebase.')
            except Exception as e:
                messages.error(request, f'Error updating Firebase profile: {e}')

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

@login_required(login_url='/accounts/login/')
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
                success = PhoneVerificationService.mark_user_phone_verified(request.user, phone_number)
                if not success:
                    return JsonResponse({'success': False, 'error': 'Failed to update user profile.'}, status=500)

                # Also update the phone number in Firestore
                user_profile = request.user.userprofile
                if db and user_profile.firebase_uid:
                    try:
                        user_ref = db.collection('users').document(user_profile.firebase_uid)
                        user_ref.set({
                            'phone_number': phone_number,
                            'is_phone_verified': True
                        }, merge=True)
                    except Exception as e:
                        print(f"Error updating phone number in Firebase: {e}") # Log error

                return JsonResponse({'success': True})
            else:
                return JsonResponse({'success': False, 'error': result['message']}, status=400)

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON.'}, status=400)
        except Exception as e:
            print(f"Error in verify_phone_token: {e}") # Log error
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)
