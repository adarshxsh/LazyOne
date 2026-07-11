# LazyOne API 

Here is our new RESTful API design! We’re moving away from scattered URLs and internal database IDs. Instead, we’re using a clean `/api/v1/` namespace and secure `public_id`s (like `A7F9K2M8XQ4L1ZPW`) to keep our database keys hidden. 

### The Standard Response
Every endpoint returns a predictable JSON response. No surprises:
```json
{
  "status": "success",
  "data": { ... },
  "meta": { "pagination": { "next": "..." } }
}
```

---

### Tasks
*Use the 16-character `public_id` for `{id}`.*

* **GET `/api/v1/tasks/`** — Fetch a list of tasks. Supports pagination and filters.
* **POST `/api/v1/tasks/`** — Create a new task. Just send `{ title, description, reward, deadline }`.
* **GET `/api/v1/tasks/{id}/`** — Get all the details for a single task.

**State Changes (Actions):**
* **POST `/api/v1/tasks/{id}/take/`** — Accept a task.
* **POST `/api/v1/tasks/{id}/complete/`** — Mark it done.
* **POST `/api/v1/tasks/{id}/abandon/`** — Drop a task you took.

**Cancellations:**
* **POST `/api/v1/tasks/{id}/cancellations/`** — Request to cancel a task.
* **POST `/api/v1/tasks/{id}/cancellations/accept/`** — Accept a cancellation request.

---

### Chats
*Use the 16-character `public_id` for `{id}`.*

* **GET `/api/v1/chats/`** — Get a list of your active conversations.
* **GET `/api/v1/chats/{id}/`** — Fetch chat details.
* **GET `/api/v1/chats/{id}/messages/`** — Load messages (cursor-based pagination for infinite scroll).
* **POST `/api/v1/chats/{id}/messages/`** — Send a new message.

---

### Disputes
*Use the 16-character `public_id` for `{id}`.*

* **POST `/api/v1/tasks/{id}/disputes/`** — Raise a dispute on a task.
* **GET `/api/v1/disputes/{id}/`** — View the dispute details.
* **POST `/api/v1/disputes/{id}/withdraw/`** — Withdraw a dispute you raised.

---

### Users & Social
* **GET `/api/v1/users/me/`** — Get your own profile and reward points.
* **GET `/api/v1/friends/`** — See your friends list.
* **POST `/api/v1/users/{username}/friend-requests/`** — Add a friend.
* **POST `/api/v1/friend-requests/{id}/accept/`** — Accept a request.
* **POST `/api/v1/friend-requests/{id}/decline/`** — Decline a request.
