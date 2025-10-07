from django.shortcuts import render, redirect
from django.contrib.auth import login, logout, authenticate
import json
from django.http import JsonResponse
from firebase_admin import auth
from django.views.decorators.csrf import csrf_exempt

@csrf_exempt
def firebase_login(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            id_token = data.get('token')
            if not id_token:
                return JsonResponse({'status': 'error', 'message': 'Token not provided'}, status=400)

            user = authenticate(request, token=id_token)

            if user is not None:
                login(request, user)
                return JsonResponse({'status': 'success', 'message': 'User logged in successfully'})
            else:
                return JsonResponse({'status': 'error', 'message': 'Invalid token or user not found.'}, status=401)

        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

    return JsonResponse({'status': 'error', 'message': 'Invalid request method'}, status=405)


def login_page(request):
    return render(request, 'login.html')


def logout_view(request):
    logout(request)
    return redirect('login_page')
