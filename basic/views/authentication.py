from django.shortcuts import render, redirect
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.models import User
from django.contrib import messages
from django.utils import timezone
from datetime import timedelta
import random
from django.core.mail import send_mail
from django.conf import settings
from ..models import UserProfile
from ..forms import CustomUserCreationForm # Import the custom form

def register_view(request):
    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST) # Use the custom form
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = False  # Deactivate account until email is verified
            user.save()

            # Create profile and generate OTP
            profile = UserProfile.objects.create(user=user)
            otp = str(random.randint(100000, 999999))
            profile.email_otp = otp
            profile.email_otp_created_at = timezone.now()
            profile.save()

            # Send OTP email
            try:
                subject = 'Your LazyOne Account Verification Code'
                message = f'Hi {user.username},\n\nYour verification code is: {otp}\n\nThis code will expire in 10 minutes.\n'
                send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [user.email])
                messages.success(request, 'A verification code has been sent to your email.')
            except Exception as e:
                messages.error(request, f"Could not send verification email. Please contact support. Error: {e}")
                return render(request, 'register.html', {'form': form})

            # Store user's pk in session to know who is verifying
            request.session['otp_user_pk'] = user.pk
            return redirect('verify_otp')
        else:
            # Check if the error is due to a duplicate username
            if 'username' in form.errors and any('already exists' in e for e in form.errors['username']):
                username = request.POST.get('username')
                suggestions = []
                for i in range(1, 4):
                    suggestion = f'{username}{i}'
                    if not User.objects.filter(username=suggestion).exists():
                        suggestions.append(suggestion)
                
                context = {
                    'form': form,
                    'username_taken_error': True,
                    'username_suggestions': suggestions
                }
                return render(request, 'register.html', context)
            
            return render(request, 'register.html', {'form': form})
    else:
        form = CustomUserCreationForm() # Use the custom form for GET requests
    return render(request, 'register.html', {'form': form})

def verify_otp_view(request):
    user_pk = request.session.get('otp_user_pk')
    if not user_pk:
        messages.error(request, "Session expired. Please register again.")
        return redirect('register')

    try:
        user = User.objects.get(pk=user_pk)
    except User.DoesNotExist:
        messages.error(request, "User not found. Please register again.")
        return redirect('register')

    if request.method == 'POST':
        submitted_otp = request.POST.get('otp')
        profile = user.userprofile

        # Check if OTP is valid and not expired (e.g., within 10 minutes)
        if profile.email_otp == submitted_otp and (timezone.now() - profile.email_otp_created_at) < timedelta(minutes=10):
            user.is_active = True
            user.save()
            
            # Clear OTP fields
            profile.email_otp = None
            profile.email_otp_created_at = None
            profile.save()

            login(request, user)
            messages.success(request, "Email verified successfully. You are now logged in.")
            del request.session['otp_user_pk'] # Clean up session
            return redirect('home')
        else:
            messages.error(request, "Invalid or expired OTP. Please try again.")

    return render(request, 'verify_otp.html')

def login_page(request):
    if request.method == 'POST':
        identifier = request.POST.get('username')
        password = request.POST.get('password')
        
        if not identifier or not password:
            messages.error(request, "Please enter both username/email and password.")
            return render(request, 'login.html')

        user = authenticate(request, username=identifier, password=password)
        if user is None:
            try:
                user_obj = User.objects.get(email=identifier)
                user = authenticate(request, username=user_obj.username, password=password)
            except User.DoesNotExist:
                user = None

        if user is not None:
            if user.is_active:
                login(request, user)
                return redirect('home')
            else:
                # If user is inactive, redirect to OTP page
                request.session['otp_user_pk'] = user.pk
                messages.info(request, "Your account is not active. Please verify your email.")
                return redirect('verify_otp')
        else:
            messages.error(request, "Invalid credentials. Please try again.")
            
    return render(request, 'login.html')

def logout_view(request):
    logout(request)
    return redirect('login_page')
