# Delivery, Realtime and Queues

How events reach users today, how fast, and the agreed direction.

## Today

```
                     ONE GLOBAL QUEUE (server)
state change ──▶ OutboxEvent table ──▶ handlers ──▶ Notification rows / Firestore mirror
(same txn)       FIFO by available_at   right after commit, else run_jobs
                 retries + backoff      (30 s locally, 60 s via cron), SKIP LOCKED

                     PER-USER DATA (pulled, not queued)
Notifications ─▶ inbox rows: unread badge on every page, list at /notifications/
Feed          ─▶ computed on read (fan-out-on-read, ~2-3 ms, see WAVE_DISTRIBUTION.md)
Chat          ─▶ cursor: GET /chat/history/<id>/?after=<last id>
Jury duty     ─▶ JurorAssignment rows under My Tasks
```

- **No per-user queue** and **no message broker** (RabbitMQ/Kafka/Celery). The Postgres outbox is the queue: an event exists if and only if its DB change committed.
- **No API gateway.** Gateway duties are split: TLS/proxy at the edge (Caddy or Vercel), auth/throttling/idempotency/versioning in Django (`api/`).

## How fast does the other user get a chat message?

| Receiver | Delay | Notes |
|----------|-------|-------|
| Chat open, realtime **off** (local default) | 0–3 s (avg ~1.5 s) | 3 s polling |
| Chat open, **Firestore realtime on** | typically < 1 s (not yet measured) | commit → server writes mirror → listener |
| Chat open, mirror write failed | until outbox retry (30–60 s+) | gap: client only falls back to polling if the listener itself fails |
| Elsewhere on the site | next page load | no push |
| Android app closed/backgrounded | not delivered | needs FCM |

## Decisions

| Topic | Decision | Why |
|-------|----------|-----|
| Message broker | **Not now**; Postgres outbox | Transactional safety, one less service. Add Redis/RQ or Celery behind the outbox relay only for heavy jobs or >~100 events/s |
| API gateway | **Not needed** | One app, two first-party clients. Edge (Caddy/Cloudflare) + Django cover it |
| Per-user task queue | **No** (fan-out-on-read) | Writing each task into ~650 queues per wave costs more and goes stale; feed read is 2–3 ms |
| WebSockets | **Deferred** | Not possible on Vercel; on own server Django Channels + Redis is the upgrade path via `RealtimePublisher` (`basic/realtime.py`) without touching business code |
| Push | **FCM required** | Android kills background sockets; FCM is the only way to reach closed apps, whatever realtime is used |

## Planned (not built)

1. **Chat safety poll** - while Firestore is live, also poll every 20 s with the `after` cursor so a missed mirror arrives within 20 s.
2. **Adaptive polling** when realtime is off - 1 s while typing/focused, 5–10 s idle, paused when the tab is hidden.
3. **Delivery & sync**
   ```
   SERVER                                      CLIENT (Android / web)
   Notification + delivered_at/read_at   ──▶   FCM push wakes the app
   GET /api/v1/sync?since=<cursor>       ◀──   on open/reconnect: one call for everything new
   POST + Idempotency-Key (exists)       ◀──   local outbox on device, retried until acked
   ```
4. Run `run_jobs` every 10 s on an always-on server if 30–60 s timer lag is too much.
