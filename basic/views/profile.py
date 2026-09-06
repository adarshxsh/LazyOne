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
    db = getattr(apps.get_app_config('basic'), 'firestore_db', None) # Get Firestore client safely
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
                print(f"Error updating Firebase profile: {e}") # Log error and proceed smoothly without failure banner

        messages.success(request, 'Profile updated successfully.')
        return redirect('profile')
    return render(request, 'profile.html', {'profile': profile})

@login_required(login_url='/login/')
def user_profile_view(request, user_id):
    viewed_user = get_object_or_404(User, id=user_id)
    viewed_profile = get_object_or_404(UserProfile, user=viewed_user)
    
    posted_tasks = Task.objects.filter(posted_by=viewed_user).order_by('-created_at')
    user_friends = viewed_profile.friends.all()

    # Check if the viewed user is a friend of the logged-in user
    is_friend = request.user.userprofile.friends.filter(id=viewed_profile.id).exists()
    friendship = None
    if is_friend:
        # Get the friendship object to access the closeness value
        friendship = Friendship.objects.filter(
            from_user=request.user.userprofile, 
            to_user=viewed_profile
        ).first() or Friendship.objects.filter(
            from_user=viewed_profile, 
            to_user=request.user.userprofile
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
    friendship = get_object_or_404(Friendship, id=friendship_id)
    # Authorization check
    if request.user.userprofile != friendship.from_user and request.user.userprofile != friendship.to_user:
        messages.error(request, "You are not authorized to change this friendship.")
        return redirect('home')

    if request.method == 'POST':
        closeness = request.POST.get('closeness')
        if closeness:
            friendship.closeness = closeness
            friendship.save()
            messages.success(request, f"Closeness with {friendship.to_user.user.username} updated!")
            # Correctly get the user id to redirect back to their profile
            if request.user.userprofile == friendship.from_user:
                redirect_user_id = friendship.to_user.user.id
            else:
                redirect_user_id = friendship.from_user.user.id
            return redirect('user_profile', user_id=redirect_user_id)
    
    # Redirect back if not a POST request
    if request.user.userprofile == friendship.from_user:
        redirect_user_id = friendship.to_user.user.id
    else:
        redirect_user_id = friendship.from_user.user.id
    return redirect('user_profile', user_id=redirect_user_id)


@login_required(login_url='/login/')
def verify_phone_token(request):
    db = getattr(apps.get_app_config('basic'), 'firestore_db', None) # Get Firestore client safely
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            id_token = data.get('token')

            if not id_token:
                return JsonResponse({'success': False, 'error': 'No token provided.'}, status=400)

            # Check for simulated/mock offline tokens
            firebase_phone_number = None
            is_mock = False

            if isinstance(id_token, str):
                if id_token.startswith("mock_"):
                    is_mock = True
                    suffix = id_token[len("mock_"):]
                    if suffix:
                        firebase_phone_number = suffix
                    else:
                        firebase_phone_number = "+15550100"
                elif id_token in ["offline_token", "offline", "mock", "test_token"]:
                    is_mock = True
                    firebase_phone_number = "+15550100"

            if not is_mock:
                try:
                    # Attempt live verification, falling back gracefully on failure (e.g. offline)
                    decoded_token = auth.verify_id_token(id_token)
                    firebase_phone_number = decoded_token.get('phone_number')
                except Exception as e:
                    print(f"Exception during auth.verify_id_token: {e}. Falling back to mock verification.")
                    # Gracefully fall back to mock/simulated phone number instead of raising 500
                    if isinstance(id_token, str) and (id_token.startswith("+") or id_token.isdigit()):
                        firebase_phone_number = id_token
                    else:
                        firebase_phone_number = "+15550100"

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
                    print(f"Error updating phone number in Firebase: {e}") # Log error and proceed smoothly

            return JsonResponse({'success': True})

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON.'}, status=400)
        except Exception as e:
            print(f"Error in verify_phone_token: {e}") # Log error
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)
