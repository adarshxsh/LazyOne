from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import UserProfile, Task, Friendship
from django.contrib.auth.models import User
import json
from django.http import JsonResponse, HttpResponse
from firebase_admin import auth
from django.apps import apps # Import apps to access app config

# Health check endpoint
def ping(request):
    """A simple view that returns a 200 OK response."""
    return HttpResponse("pong")

@login_required(login_url='/login/')
def profile_view(request):
    db = apps.get_app_config('basic').firestore_db # Get Firestore client
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
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)
    viewed_user = get_object_or_404(User, id=user_id)
    viewed_profile, _ = UserProfile.objects.get_or_create(user=viewed_user)
    
    posted_tasks = Task.objects.filter(posted_by=viewed_user).order_by('-created_at')
    user_friends = viewed_profile.friends.all()

    # Check if the viewed user is a friend of the logged-in user
    is_friend = user_profile.friends.filter(id=viewed_profile.id).exists()
    friendship = None
    if is_friend:
        # Get the friendship object to access the closeness value
        friendship = Friendship.objects.filter(
            from_user=user_profile, 
            to_user=viewed_profile
        ).first() or Friendship.objects.filter(
            from_user=viewed_profile, 
            to_user=user_profile
        ).first()

    context = {
        'viewed_profile': viewed_profile,
        'posted_tasks': posted_tasks,
        'user_friends': user_friends,
        'is_friend': is_friend,
        'friendship': friendship
    }
    return render(request, 'user_profile.html', context)

@login_required(login_url='/login/')
def update_closeness(request, friendship_id):
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)
    friendship = get_object_or_404(Friendship, id=friendship_id)
    # Authorization check
    if user_profile != friendship.from_user and user_profile != friendship.to_user:
        messages.error(request, "You are not authorized to change this friendship.")
        return redirect('home')

    other_profile = friendship.to_user if user_profile == friendship.from_user else friendship.from_user

    if request.method == 'POST':
        closeness_raw = request.POST.get('closeness')
        if closeness_raw is not None:
            try:
                closeness = int(closeness_raw)
            except (ValueError, TypeError):
                closeness = 50
            friendship.closeness = closeness
            friendship.save()

            # If reverse friendship exists, update it too
            reverse_friendship = Friendship.objects.filter(
                from_user=other_profile,
                to_user=user_profile
            ).first()
            if reverse_friendship:
                reverse_friendship.closeness = closeness
                reverse_friendship.save()

            messages.success(request, f"Closeness with {other_profile.user.username} updated!")
            return redirect('user_profile', user_id=other_profile.user.id)
    
    return redirect('user_profile', user_id=other_profile.user.id)


@login_required(login_url='/login/')
def verify_phone_token(request):
    db = apps.get_app_config('basic').firestore_db # Get Firestore client
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            id_token = data.get('token')

            if not id_token:
                return JsonResponse({'success': False, 'error': 'No token provided.'}, status=400)

            decoded_token = auth.verify_id_token(id_token)
            firebase_phone_number = decoded_token.get('phone_number')

            if not firebase_phone_number:
                return JsonResponse({'success': False, 'error': 'Could not verify phone number from token.'}, status=400)

            user_profile = request.user.userprofile

            # Trust the number from the Firebase token, since the user just verified it.
            user_profile.is_phone_verified = True
            user_profile.phone_number = firebase_phone_number
            user_profile.save()

            # Also update the phone number in Firestore
            if db and user_profile.firebase_uid:
                try:
                    user_ref = db.collection('users').document(user_profile.firebase_uid)
                    user_ref.set({
                        'phone_number': firebase_phone_number,
                        'is_phone_verified': True
                    }, merge=True)
                except Exception as e:
                    print(f"Error updating phone number in Firebase: {e}") # Log error

            return JsonResponse({'success': True})

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON.'}, status=400)
        except Exception as e:
            print(f"Error in verify_phone_token: {e}") # Log error
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)
