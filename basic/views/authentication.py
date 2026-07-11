from django.shortcuts import render, redirect
from django.contrib.auth import logout

def login_page(request):
    return render(request, 'login.html')


def logout_view(request):
    logout(request)
    return redirect('login_page')
