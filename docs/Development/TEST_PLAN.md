# Test Plan

This document outlines the testing strategy and validation procedures for LazyOne.

## Functional Testing

### 1. Authentication Flow
- Verify signing up creates a new `User` and matching `UserProfile`.
- Verify user starts with `1500` default reward points.
- Verify Google Sign-In authenticates via Firebase.

### 2. Task Flow
- **Post a Task**: Verify posting a task deducts specified points from the poster's profile.
- **Take a Task**: Verify a user can accept a task, updating `is_taken=True` and setting `taken_by`.
- **Complete a Task**: Verify completing a task awards the points to the task taker.

### 3. Friendship Flow
- Send a friend request and verify a pending request appears.
- Accept a friend request and verify they appear in the user's friend list and home page "Social Circle" visualization.

### 4. WebSocket Chat Flow
- Navigate to the conversation page for an active task.
- Send messages and confirm they are delivered to the recipient instantly via WebSockets (without page reload).

## Running Tests

Run Django's built-in test suite:
```sh
python manage.py test
```