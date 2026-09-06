"""
Django settings for LazyOne project.
"""

import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
import dj_database_url
from dotenv import load_dotenv

print("--- Loading settings.py ---")

# Build paths
BASE_DIR = Path(__file__).resolve().parent.parent
print("BASE_DIR: OK")

# Load .env file
load_dotenv(BASE_DIR / '.env')
print("dotenv: OK")

# Secret Key
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    raise ImproperlyConfigured("CRITICAL ERROR: DJANGO_SECRET_KEY environment variable not set.")
print("SECRET_KEY: OK")

# Debug
DEBUG = os.getenv('DEBUG', 'False') == 'True'
print(f"DEBUG: {DEBUG}")

# Allowed Hosts
ALLOWED_HOSTS = []
if os.getenv('VERCEL_URL'):
    ALLOWED_HOSTS.append(os.getenv('VERCEL_URL').split('//')[1])
else:
    ALLOWED_HOSTS.extend(['127.0.0.1', 'localhost'])
print(f"ALLOWED_HOSTS: {ALLOWED_HOSTS}")

# Application definition
INSTALLED_APPS = [
    'basic.apps.BasicConfig',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'whitenoise.runserver_nostatic',
    'django.contrib.staticfiles',
]
print("INSTALLED_APPS: OK")

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
print("MIDDLEWARE: OK")

ROOT_URLCONF = 'LazyOne.urls'
print("ROOT_URLCONF: OK")

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'basic.context_processors.unread_notifications_count',
                'basic.context_processors.firebase_keys',
            ],
        },
    },
]
print("TEMPLATES: OK")

WSGI_APPLICATION = 'LazyOne.wsgi.application'
print("WSGI_APPLICATION: OK")

# Database
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise ImproperlyConfigured("CRITICAL ERROR: DATABASE_URL environment variable not set.")

DATABASES = {
    'default': dj_database_url.config(conn_max_age=600)
}
print("DATABASES: OK")


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
print("AUTH_PASSWORD_VALIDATORS: OK")

# Auth Backend
AUTHENTICATION_BACKENDS = ['django.contrib.auth.backends.ModelBackend']
print("AUTHENTICATION_BACKENDS: OK")

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
print("I18N: OK")

# Static files
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'
print("STATIC_FILES: OK")

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'mediafiles'
print("MEDIA_FILES: OK")

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
print("DEFAULT_AUTO_FIELD: OK")

# --- Firebase Client-Side Config ---
FIREBASE_API_KEY = os.getenv('FIREBASE_API_KEY', '')
FIREBASE_AUTH_DOMAIN = os.getenv('FIREBASE_AUTH_DOMAIN', '')
FIREBASE_PROJECT_ID = os.getenv('FIREBASE_PROJECT_ID', '')
FIREBASE_STORAGE_BUCKET = os.getenv('FIREBASE_STORAGE_BUCKET', '')
FIREBASE_MESSAGING_SENDER_ID = os.getenv('FIREBASE_MESSAGING_SENDER_ID', '')
FIREBASE_APP_ID = os.getenv('FIREBASE_APP_ID', '')

# Startup verification check for Firebase client-side variables
firebase_keys_list = [
    ('FIREBASE_API_KEY', FIREBASE_API_KEY),
    ('FIREBASE_AUTH_DOMAIN', FIREBASE_AUTH_DOMAIN),
    ('FIREBASE_PROJECT_ID', FIREBASE_PROJECT_ID),
    ('FIREBASE_STORAGE_BUCKET', FIREBASE_STORAGE_BUCKET),
    ('FIREBASE_MESSAGING_SENDER_ID', FIREBASE_MESSAGING_SENDER_ID),
    ('FIREBASE_APP_ID', FIREBASE_APP_ID),
]

missing_keys = [name for name, val in firebase_keys_list if not val or val.strip() == "" or "your-" in val]
if missing_keys:
    import sys
    print(f"WARNING: Missing or incomplete Firebase client configuration parameters: {', '.join(missing_keys)}", file=sys.stderr)

print("--- settings.py loaded successfully ---")
