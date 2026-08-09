from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.urls import reverse
from ..models import UserProfile
from ..stripe_utils import create_connect_account, create_account_onboarding_link

@login_required(login_url='/login/')
def stripe_connect_view(request):
    user_profile, created = UserProfile.objects.get_or_create(user=request.user)
    
    stripe_account_id = user_profile.stripe_account_id
    if not stripe_account_id:
        try:
            stripe_account_id = create_connect_account(request.user.email, request.user.username)
            user_profile.stripe_account_id = stripe_account_id
            user_profile.save()
        except Exception as e:
            messages.error(request, f"Failed to create Stripe account: {str(e)}")
            return redirect('profile')
    
    refresh_url = request.build_absolute_uri(reverse('stripe_callback')) + '?status=refresh'
    return_url = request.build_absolute_uri(reverse('stripe_callback')) + '?status=success'
    
    try:
        onboarding_url = create_account_onboarding_link(stripe_account_id, refresh_url, return_url)
        return redirect(onboarding_url)
    except Exception as e:
        messages.error(request, f"Failed to generate onboarding link: {str(e)}")
        return redirect('profile')

@login_required(login_url='/login/')
def stripe_callback_view(request):
    status = request.GET.get('status', '')
    if status == 'success':
        messages.success(request, "Successfully connected your Stripe account! You can now take USD fiat-backed tasks.")
    elif status == 'refresh':
        messages.warning(request, "Stripe onboarding was interrupted. Please try again.")
        return redirect('stripe_connect')
    else:
        messages.error(request, "Stripe onboarding failed or was cancelled.")
    
    return redirect('profile')
