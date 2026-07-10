# Installation Instructions

This guide provides steps for setting up LazyOne locally for development.

## 1. System Requirements
- Python 3.11 or later
- Redis Server (v6.0+)
- Virtualenv setup tools

## 2. Dependencies
- Install dependencies listed in requirements file:
  ```sh
  pip install -r requirements.txt
  ```

## 3. Configuration Setup
- Add `serviceAccountKey.json` from Firebase Console.
- Set up a `.env` file in project root with required fields.

## 4. Run Migration
```sh
python manage.py makemigrations
python manage.py migrate
```

## 5. Launch Services
- In terminal 1, run Redis Server:
  ```sh
  redis-server
  ```
- In terminal 2, launch Django:
  ```sh
  python manage.py runserver
  ```
- Access application at `http://127.0.0.1:8000/`.