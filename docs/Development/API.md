# LazyOne API v1

Base path: `/api/v1/` · Code: [`api/`](../../api/) · Used by the Android (Flutter) app and any future SPA. Same backend and rules as the website.

## Conventions

| Topic | Rule |
|-------|------|
| Auth | `Authorization: Bearer <Firebase ID token>` (mobile) or Django session + `X-CSRFToken` (web) |
| IDs | Public, non-guessable `public_id` strings - never database ids |
| Retries | Send `Idempotency-Key: <uuid>` on POSTs; a retry replays the stored response (`Idempotent-Replay: true`). Same key on a different path → `422` |
| Concurrency | Task actions accept optional `version`; if the task changed → `409` with the current `version` |
| Errors | `{"detail": "<user-safe message>"}` - `400` invalid input, `403` not allowed, `404` not found / not visible, `409` illegal transition, `429` throttled, `503` realtime unavailable |
| Throttles | `user` 240/min, `post_task` 30/hour, `messages` 60/min (env-configurable) |
| Compatibility | Old app versions stay installed: never break `v1`; add `v2` instead |

## Endpoints

### Me & ledger
| Method | Path | Returns |
|--------|------|---------|
| GET | `/me/` | id, username, email, `balance`, community, `phone_verified` |
| GET | `/ledger/` | balance + last 100 entries (amount, reason, task, time) |

### Tasks
| Method | Path | Notes |
|--------|------|-------|
| GET | `/tasks/feed/?q=` | Wave-filtered feed (max 50) |
| GET | `/tasks/mine/` | Posted + taken |
| POST | `/tasks/` | `{title, description?, reward, deadline?, wave_policy?}` → 201. Reward is moved to escrow; `400` if balance too low or friends-only with no friends |
| GET | `/tasks/{id}/` | Visible to poster, taker, viewers whose wave has started, jurors, staff |
| GET | `/tasks/{id}/events/` | Audit trail (poster, taker, staff) |
| POST | `/tasks/{id}/{action}/` | `action` ∈ `accept, submit, approve, cancel, abandon, request_cancel, accept_cancel`; body `{note?, version?}` |

Task objects include `status`, `version`, `allowed_actions` (for the caller), `conversation_id` (parties only), `active_dispute_id`.

### Disputes
| Method | Path | Notes |
|--------|------|-------|
| POST | `/tasks/{id}/disputes/` | `{reason, dispute_type?}`; takes a bond of max(20 % of reward, 50) |
| GET | `/disputes/{id}/` | Parties, jurors, staff only. `tally` shows counts only while open; votes revealed after close |
| POST | `/disputes/{id}/evidence/` | `{text}`; parties, before `evidence_deadline` |
| POST | `/disputes/{id}/vote/` | `{vote: "taker"|"poster"}`; jurors, before `voting_deadline` |
| POST | `/disputes/{id}/withdraw/` | Raiser only; bond refunded |

### Chat & realtime
| Method | Path | Notes |
|--------|------|-------|
| GET | `/conversations/{id}/messages/?after=` | History / polling cursor |
| POST | `/conversations/{id}/messages/` | `{content}` → 201 |
| GET | `/realtime/token/` | Firebase custom token (uid `django:<id>`) for read-only Firestore listeners; `503` if realtime is off |

## Example: take a task and get paid
```http
GET  /api/v1/tasks/feed/
POST /api/v1/tasks/Ab3.../accept/        {"version": 1}       Idempotency-Key: 6f1c...
POST /api/v1/tasks/Ab3.../submit/        {"note": "Left it at your door"}
# poster:
POST /api/v1/tasks/Ab3.../approve/
GET  /api/v1/me/                         → balance increased by the reward
```
