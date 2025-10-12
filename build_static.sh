#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

echo "Installing dependencies..."
# Use the specific python version provided by Vercel
python3.9 -m pip install -r requirements.txt

echo "Collecting static files..."
python3.9 manage.py collectstatic --noinput

echo "Static files collected."
