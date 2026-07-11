# Architectural Decisions

This document logs architectural design decisions made during the development of LazyOne.


## . Authentication 
- **Decision:** Use google OAuth and the firebase email and mobile otp verification because firebase sign in with google mostly support with the js based app 
	The use authentication database will be on the firebase to incorporate the email verification 

## .Profile Details 
- Decision :  Separate authentication from profile. --> Allows future migration away from Firebase without losing application data.
- ``` 
	   Firebase User --> User (Django) 
	   id firebase_uid email created_at │ ▼ Profile name department year hostel bio avatar skills rating
  ```


## . Task Details

- For `crud operation`

```

Task

	id,
	
	creator
	
	title
	
	description
	
	reward_points
	
	deadline
	
	visibility
	
	status
	
	created_at
	
	updated_at


TaskApplication

	task
	
	user
	
	status
	
	
	Pending
	
	Accepted
	
	Rejected
	
	Cancelled
	
	DISPUTED

TaskAssignment
	
	task
	
	assigned_user
	
	accepted_time
	
	completed_time

Dispute 
	id task opened_by reason status created_at resolved_at
	
DisputeVote- dispute user vote created_at

Applications and assignments are different concepts.
	Never store accepted_user_id inside Task.

```

## .Task FlowLayer 

```
DRAFT

↓

OPEN

↓

APPLICATIONS

↓

ASSIGNED

↓

IN_PROGRESS

↓

SUBMITTED

↓

COMPLETED
```

plus

```
IN_PROGRESS

↓

DISPUTED

↓

RESOLVED
```

This defines the legal lifecycle of a task.


## . Chat Rules 

```
Unaccepted chat is like bid 
Every accepted task has a private task chat.

Messages are stored in PostgreSQL.

Redis only delivers realtime events.
```

### Explicit Chat Permissions
To remove ambiguity in backend authorization and frontend behavior, chat permissions are defined as follows:

| Chat State    | Creator        | Assigned User  | Other Users | Moderator      |
| ------------- | -------------- | -------------- | ----------- | -------------- |
| Normal Task   | ✅ Read/Write | ✅ Read/Write | ❌          | ✅             |
| Disputed Task | ✅ Read/Write | ✅ Read/Write | ✅ Read Only| ✅ Read/Write |

---

## Architecture Review (Implementation vs. Planned)

Based on an audit of the current codebase (`basic/views/`, `basic/models.py`, `basic/services/`), the actual implementation diverges significantly from the planned architecture above.

### Authentication Flow
- **Current State:** Handled via `FirebaseBackend` verifying ID tokens. Valid approach. `PhoneVerificationService` extracts numbers.
- **Issues:** Firebase Admin is initialized globally in `models.py`. Moving initialization to `apps.py` is cleaner, but it won't determine whether the app succeeds. Treat it as a cleanup task (🟢 Can Wait).

### Service Layer
- **Current State:** Transitioning. We've introduced `DisputeService` and `ChatService` to handle authorization and complex state changes (like `resolve_dispute()`). However, some basic task transitions are still inside Django views (`basic/views/tasks.py`), which we'll continue refactoring.
- **Missing Abstractions:** Moving the rest of the business logic into `TaskService` and `LedgerService` remains a priority for better maintainability.

### Repository Layer
- **Decision:** The Django ORM natively acts as the repository layer. A dedicated repository layer often becomes a thin wrapper without adding much value. The design should follow: `APIView -> TaskService -> Models / Managers / QuerySets`.
- **Current State:** Acceptable and well-integrated.

### Chat Architecture
- **Decision:** PostgreSQL is the authoritative data store. Firestore is used strictly as a real-time synchronization cache. If Firestore is unavailable, messages must still be safely stored in PostgreSQL.
- **Current State:** Needs alignment to ensure writes don't unnecessarily require both systems to succeed atomically.

### Notification Architecture
- **Decision:** Notifications should not be created in every view. Eventually, the flow should be: `Task Assigned -> NotificationService -> DB -> Celery -> Push`.
- **Current State:** Synchronous creation inside views. Polled/marked-as-read synchronously on page load. Needs migration to the planned background flow.

### Task Workflow
- **Current State:** Managed mostly inside views with `transaction.atomic()`. 
- **Race Conditions:** 
  - **Fixed:** We now use `select_for_update()` when taking tasks to prevent concurrent double-booking. 
  - **Fixed:** Reward balance updates use `F('rewards')` expressions to safely increment/decrement wallets without lost-update race conditions.

### Dispute Workflow
- **Current State:** The `Dispute` model overrides `save()` to make synchronous HTTP calls to Firebase Firestore.
- **Coupling & Scalability (Critical Issue):** Making network calls inside a synchronous Django ORM `save()` method within a database transaction is a severe anti-pattern. If Firebase is slow or down, the database transaction stays open and locks the rows, which will quickly exhaust the WSGI worker pool and crash the app even at 400 users.

### Overall Conclusion
We are steadily moving away from the "Fat Views" anti-pattern. By introducing `DisputeService` and fixing critical race conditions in the ORM, the app is much more stable. The next critical step is to decouple external network calls (like Firestore sync) from synchronous database transactions and push notifications to Celery.

---

## . Dispute Chat Behavior 
```
When a dispute is raised

↓

Task chat becomes public (read-only)

↓

Participants continue discussion

↓

Other authenticated users can read

↓

Users vote
```


## .Chatting 

```
Architecture : 
	Flutter
	↓
	WebSocket
	↓
	Django Channels
	↓
	Redis
	↓
	PostgreSQL
	
ChatRoom 
	id
	task
	created_at
	
Message
	room
	sender
	content
	created_at
	read_at
	edited

```
	Store every message in PostgreSQL.
	Redis is only for realtime delivery.


## .Websocket + Reddis database 

use Django Channels --> channel layer (Redis)
Redis acts as the event broker.
Never permanently store chat inside Redis.


## . Database 

Single PostgreSQL database.

Tables : Everything is relational 

```
User

Profile

Task

TaskApplication

TaskAssignment

ChatRoom

Message

Dispute

DisputeVote

Notification
	do not push notification from the bussiness logic 
	instead : task accepted --> create notification --> save db --> send push --> mark delivered
	Notification table 
		user
		title
		body
		type
		is_read
		created_at
		
```

## .Manager Plane (Service Layer )

- Avoid putting business logic in views 
	```
	APIView --> TaskService --> Models / Managers / QuerySets

	TaskService
		create_task()
		accept_application()
		assign_user()
		close_task()
		
		raise_dispute()
		resolve_dispute()
	
	ChatService 
		send_message()
		mark_read()
	
	DisputeService 
		create_dispute() 
		cast_vote() 
		close_dispute()
	
	NotificationService 
		notify_assignment()
		notify_completion()	
		notify_dispute()
		
	```


## .Search
For 1000 users, Simple PostgreSQL search.
```
icontains
	or 
Trigram Index
```



## .Media Storage

In this case not allowed image storage 

## .Background Jobs


```
Use Celery + Redis

Tasks
	- reminder notifications
	- email sending
	- cleanup
	- Dispute reminder notifications

Avoid cron logic inside views.
```


## .API Design

REST API.
```
/tasks/
/applications/
/chat/
/notifications/
/profile/
/disputes/
/votes/

```

### Separate Internal ID and Public Chat ID

Keep the database efficient while exposing a non-sequential identifier.
```
ChatRoom

id              BIGINT PRIMARY KEY
chat_id         CHAR(16) UNIQUE
task_id
created_at
is_public
```

```
id = 42
chat_id = "A7F9K2M8XQ4L1ZPW"
```

The API becomes:

```
GET /api/v1/chats/A7F9K2M8XQ4L1ZPW/
```

Users never see the database primary key.

- **Current State:** Fully Implemented. The frontend and backend now exclusively communicate using the 16-character `public_id` for Tasks, Chats, and Disputes, hiding the internal PostgreSQL keys from users entirely.

---
