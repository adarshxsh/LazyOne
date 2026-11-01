# Business Layer

Where every rule about points, tasks, disputes and visibility lives. ~1,100 lines in 9 service modules.

## Layers

```
Entry points   web views · /api/v1 (DRF) · /internal/cron/ · management commands
               parse input → call ONE service → render. No rules here.
Services       ledger · marketplace · disputes · feed · messaging · identity · permissions · jobs
Side effects   outbox.emit()  →  basic/handlers.py   (runs after commit, retried)
Data           Django models on PostgreSQL
```

### Rules the code follows
1. Only `marketplace.apply()` changes `Task.status`.
2. Only `ledger.transfer()` moves points. `UserProfile.rewards` is a display copy kept in sync by the ledger.
3. No network call (Firestore, push, email) inside a DB transaction; it goes through the outbox.
4. Services raise `TransitionError` with a user-safe message → flash message (web) or HTTP 409/400 (API).

## Services

| Module | Responsibility | Key functions |
|--------|----------------|---------------|
| [`ledger/services.py`](../../ledger/services.py) | Double-entry points: wallets, task escrow, dispute bonds | `transfer(legs, key, reason)`, `user_account`, `task_escrow`, `dispute_bond`, `check_invariants` |
| [`basic/services/marketplace.py`](../../basic/services/marketplace.py) | Task lifecycle state machine + audit | `TRANSITIONS`, `apply(task, action, actor)`, `post_task`, `allowed_actions` |
| [`basic/services/disputes.py`](../../basic/services/disputes.py) | Peer-jury disputes | `raise_dispute`, `add_evidence`, `draw_jury`, `cast_vote`, `tally`, `withdraw`, `moderator_resolve` |
| [`basic/services/feed.py`](../../basic/services/feed.py) | Wave distribution (🔒 locked v1) | `wave_schedule`, `viewer_circle`, `feed_for`, `can_see` |
| [`basic/services/messaging.py`](../../basic/services/messaging.py) | Chat, Postgres first | `send`, `history`, `serialize` |
| [`basic/services/permissions.py`](../../basic/services/permissions.py) | Who can read/send/view | `ChatService.can_read/can_send/reader_ids`, `DisputeService` |
| [`basic/services/identity.py`](../../basic/services/identity.py) | Firebase uid → user, community by email domain | `user_from_firebase_claims` |
| [`basic/services/jobs.py`](../../basic/services/jobs.py) | Time-based work | `run_due` (expire, 48 h auto-approve, jury tallies, outbox retries) |
| [`outbox/services.py`](../../outbox/services.py) | Durable side-effect queue | `emit`, `process_due`, `@handler` |

## Task state machine

```
available ──accept──▶ in_progress ──submit──▶ submitted ──approve / auto_approve(48h)──▶ completed
  │   ▲                 │   │                    │
cancel└────abandon──────┘   ├──dispute──┐        └──dispute──┐
expire                      │           ▼                    ▼
  ▼                    accept_cancel  disputed ──verdict_taker / approve──▶ completed
cancelled/expired         ▼             disputed ──verdict_poster──▶ cancelled
                       cancelled        disputed ──withdraw_dispute──▶ in_progress
```

| Actor | Allowed actions |
|-------|-----------------|
| Poster | cancel, approve, request_cancel |
| Taker | submit, abandon, accept_cancel |
| Either party | dispute, withdraw_dispute |
| System only | expire, auto_approve, verdict_taker, verdict_poster |

## Money safety (ledger keys)

Every logical movement has a fixed idempotency key, so it can happen **at most once**:

| Key | Movement |
|-----|----------|
| `user:<id>:open` | Opening balance |
| `task:<id>:fund` | Poster → task escrow |
| `task:<id>:settle` | Escrow → taker (payout) **or** → poster (refund). One key for both, so never both |
| `dispute:<id>:bond` | Raiser → bond account |
| `dispute:<id>:bond_settle` | Bond → raiser (refund) or → majority jurors (forfeit) |

Plus: rows locked with `SELECT … FOR UPDATE (of self)`, `CHECK (balance >= 0)`, and at most one active dispute per task (partial unique constraint).

## Not yet in the business layer

| Area | Lives in | Issue |
|------|----------|-------|
| Friends (request/accept/closeness) | `views/friends.py` | Logic + notifications in the view; acceptor can't set own closeness |
| Profile, phone verification | `views/profile.py`, `services/phone_verification.py` | Not reviewed; verification gates nothing |
| Home friend-circle graph | `views/home.py` | **Leaks non-friends' phone numbers** (see [SECURITY.md](SECURITY.md)) |
| Sybil protection | - | 1500-point sign-up bonus can be farmed |
