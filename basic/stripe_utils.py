import os
import stripe
from django.conf import settings

STRIPE_SECRET_KEY = getattr(settings, 'STRIPE_SECRET_KEY', os.getenv('STRIPE_SECRET_KEY', ''))
STRIPE_PUBLIC_KEY = getattr(settings, 'STRIPE_PUBLIC_KEY', os.getenv('STRIPE_PUBLIC_KEY', ''))

# Determine if we should run in mock mode
IS_MOCK_STRIPE = not STRIPE_SECRET_KEY or STRIPE_SECRET_KEY.startswith('mock') or STRIPE_SECRET_KEY == 'test_secret'

if not IS_MOCK_STRIPE:
    stripe.api_key = STRIPE_SECRET_KEY

def create_connect_account(user_email, username):
    """
    Creates a Stripe Connect Express account.
    """
    if IS_MOCK_STRIPE:
        return f"acct_mock_{username}_{os.urandom(4).hex()}"
    
    try:
        account = stripe.Account.create(
            type="express",
            email=user_email,
            capabilities={
                "transfers": {"requested": True},
            },
            business_profile={
                "name": f"LazyOne Task Worker - {username}",
            }
        )
        return account.id
    except Exception as e:
        # Fallback/raise
        raise e

def create_account_onboarding_link(stripe_account_id, refresh_url, return_url):
    """
    Creates an onboarding link for the connected Stripe Express account.
    """
    if IS_MOCK_STRIPE:
        return return_url
    
    try:
        account_link = stripe.AccountLink.create(
            account=stripe_account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding",
        )
        return account_link.url
    except Exception as e:
        raise e

def create_escrow_payment_intent(amount_cents, description):
    """
    Creates and auto-confirms a pre-authorization hold (PaymentIntent with manual capture).
    """
    if IS_MOCK_STRIPE:
        return "pi_mock_" + os.urandom(8).hex()
    
    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            payment_method="pm_card_visa",
            confirm=True,
            capture_method="manual",
            description=description,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"}
        )
        return intent.id
    except Exception as e:
        raise e

def payout_to_connected_account(stripe_payment_intent_id, amount_cents, worker_stripe_account_id, description=""):
    """
    Captures the pre-authorized escrow payment intent and transfers funds to the worker's Stripe Connect account.
    """
    if IS_MOCK_STRIPE:
        return "tr_mock_" + os.urandom(8).hex()
    
    try:
        # 1. Capture the escrow funds
        try:
            pi = stripe.PaymentIntent.retrieve(stripe_payment_intent_id)
            if pi.status == "requires_capture":
                stripe.PaymentIntent.capture(stripe_payment_intent_id)
        except Exception as e:
            # If capture fails (e.g. already captured), we proceed anyway
            pass
        
        # 2. Transfer the funds to the worker's connected Stripe account
        transfer = stripe.Transfer.create(
            amount=amount_cents,
            currency="usd",
            destination=worker_stripe_account_id,
            description=description or f"USD Task payout"
        )
        return transfer.id
    except Exception as e:
        raise e

def refund_escrow_payment(stripe_payment_intent_id):
    """
    Cancels/Releases the pre-authorization hold or refunds the payment if captured.
    """
    if IS_MOCK_STRIPE:
        return "re_mock_" + os.urandom(8).hex()
    
    try:
        pi = stripe.PaymentIntent.retrieve(stripe_payment_intent_id)
        if pi.status == "requires_capture":
            # Release authorization hold
            cancel_intent = stripe.PaymentIntent.cancel(stripe_payment_intent_id)
            return cancel_intent.id
        else:
            # Refund already captured payment
            refund = stripe.Refund.create(payment_intent=stripe_payment_intent_id)
            return refund.id
    except Exception as e:
        raise e
