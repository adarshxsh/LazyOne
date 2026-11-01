# Wave Distribution

> LazyOne's core idea: a task reaches the poster's closest friends first, then spreads outward.
> Code: [`basic/services/feed.py`](../../basic/services/feed.py) · Tests: [`basic/tests/test_feed.py`](../../basic/tests/test_feed.py)
> **🔒 LOCKED - v1 (2026-11-01).** Sections 1–5 and 10 describe the frozen behaviour: implemented, tested
> (98 tests, SQLite + Postgres) and benchmarked. Changing them needs a new decision entry in
> [DECISIONS.md](../Development/DECISIONS.md). Sections 6–9 are **future (v2) ideas, not part of the lock**.

## 1. The model

| Wave | Who | Standard start |
|------|-----|----------------|
| 0 | friends the **poster** rates closeness ≥ 80 | t + 0 |
| 1 | friends rated ≥ 40 | t + 10 min |
| 2 | all friends + friends-of-friends | t + 30 min |
| 3 | everyone in the community | t + 2 h |

- The **poster's** rating of the viewer decides the viewer's wave (`Friendship(from=poster, to=viewer).closeness`).
- Poster policy per task: `default`, `friends_only` (never reaches wave 3), `community_now` (everyone immediately).
- A task that is abandoned re-opens and **restarts** its waves (schedule recomputed).
- Knobs: `settings.LAZYONE` → `WAVE_DELAYS`, `CLOSENESS_WAVE0`, `CLOSENESS_WAVE1`.

## 2. Adaptive schedule (implemented)

Problem: with fixed delays, a poster with no close friends made everyone wait 10 min, and a poster with no friends made everyone wait **2 h** for nothing.

Rule: when a task opens, waves with nobody in them take **zero time**. Each non-empty wave starts after the previous non-empty one plus its own standard gap. An empty wave opens together with the next non-empty one, so start times never decrease.

| Poster's circle | wave1 | wave2 | wave3 |
|-----------------|-------|-------|-------|
| close + mid + other friends | +10 | +30 | +120 (standard) |
| mid + other friends, no close | 0 | +20 | +110 |
| only close friends | +90 | +90 | +90 |
| no friends | 0 | 0 | 0 (community immediately) |
| `friends_only`, no friends | rejected at posting ("would reach nobody") | | |

Stored on the task when it opens: `opened_at` (wave 0), `wave1_at`, `wave2_at`, `wave3_at` (NULL = never community-wide). Existing tasks were backfilled with the standard schedule (migration `0004_wave_schedule`).

## 3. Feed query (implemented)

```
viewer_circle(viewer)  →  {poster_user_id: earliest wave}     cached 60 s per viewer,
                                                              cleared when friendships change
A. community-wide:  open tasks in community with wave3_at <= now        (indexed, LIMIT n)
B. young tasks:     newest open tasks with wave3_at > now or NULL       (tuples only, ≤ 500)
                    keep if starts[circle[poster]] <= now                (O(1) in Python)
feed = merge(A, B) newest first, LIMIT n
```

`can_see(user, task)` uses the same O(1) check (used by the API's task detail).
The cache is cleared for both people **and their friends** on any friendship change (friend-of-friend sets change too).

## 4. Measured cost and latency

Benchmark: local Postgres 18 (MacBook), 1,000 users, ~30 friends each (29,716 friendship rows), DEBUG off, 200 random viewers, same process and data for every variant.

| Feed request | 2,000 open tasks | 200 open tasks |
|--------------|------------------|----------------|
| Plain feed (no waves) | 0.55 ms | 0.48 ms |
| Old wave feed (one SQL with correlated subqueries) | 9.76 ms | 9.26 ms |
| **New, warm circle cache** (normal case) | **2.86 ms** | **2.05 ms** |
| New, cold cache (after a friendship change / 60 s) | 5.02 ms | 3.95 ms |

Write side: posting a task adds ~0.5–1 ms (schedule + up to 3 delayed notification rows); wave notification fan-out is ~2.5–3.5 ms per task and runs in the background.

What it means at **1,000 concurrent users** (assume ~20 feed loads/s):

| | DB/app time per second | Share of one CPU core |
|---|---|---|
| Plain feed | ~10 ms | ~1% |
| Old wave feed | ~190 ms | ~19% |
| New wave feed (warm) | ~40–60 ms | **~4–6%** |

- **User-perceived latency:** +1.5–2.5 ms on a 50–150 ms page load → invisible.
- **Money:** fits in the spare capacity of a single small server (₹0 extra). On metered serverless Postgres it is now a minor share of compute.
- **Scaling behaviour:** the old query scanned the whole friendship table twice per request (grows with friendships); the new path costs O(circle) once per minute per viewer plus O(1) per task.

Why not ~1 ms: at 30 friends each, friends-of-friends cover ~2/3 of a 1,000-user campus (circle ≈ 650 posters). Sending that list to SQL as `IN (...)` cost ~5 ms, so the circle is applied in Python instead; the remaining cost is ~4 small queries vs 1 for the plain feed.

### Re-running the benchmark
The scripts generate a synthetic campus in a throwaway database (`createdb lazyone_bench` → migrate → generate → time → `dropdb`). Ask for them to be added under `scripts/bench/` if you want them in the repo.

## 5. Notifications

Delayed outbox events (`task.wave`) fire at each wave's stored start time and notify friends in that wave. They are skipped if the task was taken, cancelled or re-opened since scheduling (the event carries `opened_at`). `community_now` notifies all friends at once. Wave 3 (whole community) sends no push - it only appears in the feed.

---

# Future (v2) - not locked

## 6. Trust-based closeness

Today closeness is a number chosen once, and accepting a friend request copies the **requester's** rating into both directions.

Proposed:
- Each person rates the other independently (acceptor chooses their own rating).
- `effective_closeness = clamp(manual + 5 × tasks completed together − 15 × disputes lost against them, 0, 100)`.
- Always explainable in the UI: *"You're in asha's first wave: close friend, 3 tasks done together."*

## 7. Per-community configuration

Move the knobs from `settings.LAZYONE` to a `WaveConfig` model per community (thresholds, delays, notification caps), editable in Django admin. Each campus tunes without a deploy; `settings` stays the default.

## 8. Notification fatigue

- At most **N wave notifications per user per day** (suggested 10), quiet hours (e.g. 23:00–07:00).
- Users who ignore many waves in a row get lower priority.
- Feed visibility is **not** affected - only push/in-app alerts.

## 9. Measure to tune (recommended first v2 item)

Record the taker's wave at acceptance (`TaskEvent.note` or a `taken_in_wave` field). Admin view:
- time-to-accept per wave, % of tasks taken per wave, % expired,
- notifications sent per accepted task.

Tune `WAVE_DELAYS` / thresholds from these numbers instead of guesses.

---

## 10. Known limits of v1 (locked)

- `YOUNG_SCAN_LIMIT = 500`: a viewer inspects at most the 500 newest not-yet-community-wide tasks per request. Above ~500 tasks opened within 2 h in one community, an older circle task could be missed until it reaches wave 3. Raise the limit or add pagination if a campus gets there.
- The circle cache uses Django's default per-process cache; with several app processes, use a shared cache (Redis) so invalidation reaches every process (stale for at most 60 s otherwise).
