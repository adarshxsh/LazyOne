# LazyOne - Task & Social App

A Django-based web application for college students to post tasks, earn rewards, and connect with friends in a visualized social circle.

## Features

*   **Firebase Authentication:** Secure sign-up and login with Email/Password and Google Sign-In.
*   **Task Management:** Users can post tasks with reward points, take tasks, and mark them as complete.
*   **Reward System:** A point-based economy where users are rewarded for signing up and completing tasks.
*   **Social Circle:** A unique, interactive visualization of a user's social network on the home page.
*   **Friend System:** Users can send, accept, and decline friend requests.
*   **Contact Verification (Optional):** Securely verify ownership of phone numbers (via Firebase SMS) and Instagram accounts (via OAuth).

---

## Setup and Configuration

This project requires credentials from external services (Firebase and Instagram) to run correctly. Please follow these steps carefully.

### 1. Prerequisites

*   Python 3.8+
*   pip

### 2. Initial Setup

1.  **Clone the repository:**
    ```sh
    git clone <your-repo-url>
    cd LazyOne
    ```

2.  **Create and activate a virtual environment:**
    ```sh
    python -m venv venv
    source venv/bin/activate
    # On Windows, use: venv\Scripts\activate
    ```

3.  **Install dependencies:**
    ```sh
    pip install -r requirements.txt
    ```

### 3. Environment Variables (`.env` file)

Create a file named `.env` in the root directory of the project (`/LazyOne/.env`). This file will hold all your secret keys. Copy and paste the following template into the file, and then fill in the values according to the instructions below.

```dotenv
# Django Secret Key (can be any long, random string)
DJANGO_SECRET_KEY="your-django-secret-key"

# Firebase Admin SDK Path (absolute path to your service account key file)
FIREBASE_SERVICE_ACCOUNT_KEY_PATH="/path/to/your/serviceAccountKey.json"

# --- Firebase Client-Side Config ---
FIREBASE_API_KEY="your-api-key"
FIREBASE_AUTH_DOMAIN="your-auth-domain"
FIREBASE_PROJECT_ID="your-project-id"
FIREBASE_STORAGE_BUCKET="your-storage-bucket"
FIREBASE_MESSAGING_SENDER_ID="your-messaging-sender-id"
FIREBASE_APP_ID="your-app-id"

# --- Instagram OAuth Credentials ---
INSTAGRAM_APP_ID="your-instagram-app-id"
INSTAGRAM_APP_SECRET="your-instagram-app-secret"
```

### 4. Getting Credentials

#### Firebase Credentials

1.  **Create a Firebase Project:** Go to the [Firebase Console](https://console.firebase.google.com/) and create a new project.

2.  **Get Admin SDK Key:**
    *   In your Firebase project, go to **Project settings** (gear icon) > **Service accounts**.
    *   Click **"Generate new private key"**. This will download your `serviceAccountKey.json` file.
    *   Place this file somewhere safe (e.g., in the project root) and update the `FIREBASE_SERVICE_ACCOUNT_KEY_PATH` in your `.env` file with its absolute path.

3.  **Get Client-Side Keys:**
    *   Go to **Project settings** > **General** tab.
    *   Scroll down to "Your apps" and click the **Web icon (`</>`)** to create a new web app.
    *   After creating the app, you will be shown a `firebaseConfig` object. Copy the values from this object into the corresponding `FIREBASE_...` variables in your `.env` file.

4.  **Enable Authentication Methods:**
    *   In the Firebase console, go to **Authentication** > **Sign-in method** tab.
    *   Enable the **Email/Password** and **Google** providers.

#### Instagram Credentials

1.  **Create a Meta App:** Go to [Meta for Developers](https://developers.facebook.com/apps/) and create a new app of type "Consumer".

2.  **Set up Instagram Basic Display:**
    *   From the App Dashboard, add the **"Instagram Basic Display"** product.

3.  **Configure Redirect URI:**
    *   Under "Instagram Basic Display", go to **Settings**.
    *   Add the following URL to the **"Valid OAuth Redirect URIs"** field: `http://127.0.0.1:8000/instagram/callback/`

4.  **Get App Credentials:**
    *   In the app's **Settings > Basic** page, you will find your **App ID** and **App Secret**.
    *   Copy these values into the `INSTAGRAM_APP_ID` and `INSTAGRAM_APP_SECRET` variables in your `.env` file.

### 5. Final Steps

1.  **Apply Database Migrations:**
    ```sh
    python manage.py makemigrations
    python manage.py migrate
    ```

2.  **Run the Development Server:**
    ```sh
    python manage.py runserver
    ```

The application will now be running at `http://127.0.0.1:8000/`.
