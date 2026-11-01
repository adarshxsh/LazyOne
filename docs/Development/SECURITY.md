# Security Status

> Last reviewed 2026-11-01 on branch `arch-v2/phase0` (local only, **not deployed**).
> Honest summary: the code is ready for production; the remaining items need the owner (GitHub history purge, Firebase rules, workflow) or are set at deploy time.

## Protected (enforced in code, covered by tests)

| Area | Protection |
|------|------------|
| Points | Double-entry ledger, DB `CHECK (balance >= 0)`, idempotent transfers: no double payout, no minting |
| Task actions | POST-only + CSRF, row locks, per-action actor checks |
| Disputes & disputed chats | Visible only to the two parties, drawn jurors and staff |
| Chat writes | Browser never writes Firestore; every message passes `ChatService.can_send` |
| Chat rendering | Usernames and content HTML-escaped (XSS fixed) |
| API | Firebase ID token or session+CSRF, `Idempotency-Key` replay, optimistic `version` check, DRF throttles |
| Identity | Users keyed by Firebase uid; email links an existing account only if verified |
| Config | No hardcoded `SECRET_KEY` (startup fails without one when `DEBUG` is off), secure cookies when `DEBUG` is off, cron endpoint needs `CRON_SECRET` |
| Tests | Never contact Firebase (`REALTIME_ENABLED` forced off) |

## Open items (most severe first)

| # | Issue | Risk | Fix | Status |
|---|-------|------|-----|--------|
| 1 | `db.sqlite3` with real user data was in the public git history. **Purged 2026-11-01**: history rewritten on all 6 branches, 321 bot branches deleted (315 PRs closed). **Still reachable:** old blobs by SHA via closed-PR refs/GitHub cache, and the fork `tanuj123744/LazyOne` | Remaining copies downloadable by anyone with the link/fork | GitHub Support "remove sensitive data" request; ask fork owner to delete; notify the affected people | ⏳ owner: GitHub Support + notify |
| 2 | Firestore was readable/writable by any signed-in Firebase account | - | **Fixed 2026-11-01**: strict `firestore.rules` deployed (verified: signed-in stranger gets 403 on read and write); old data deleted (97 docs); Auth accounts kept | ✅ |
| 3 | `claim-jules-pr.yml` script injection | - | **Deleted on `master` 2026-11-01** | ✅ |
| 4 | Vercel preview deployments of bot PRs | Old previews may still be online | Delete old preview deployments in the Vercel dashboard; keep secrets in Production scope only | ⛔ owner |
| 5 | Old hardcoded secret key in git history | Session forgery if production used it | New `DJANGO_SECRET_KEY` in Vercel (code refuses to start without one) | deploy-time |
| 6 | Rate limits need a shared cache in production | Per-instance counting on serverless | Set `REDIS_URL` (Upstash free tier) | deploy-time |
| 7 | Not reviewed yet: chat template realtime code, notification views | Possible authz bugs | Review | open |

## Fixed on 2026-11-01

| Was | Fix |
|-----|-----|
| Home graph sent phone/Instagram of up to 100 **non-friends** | Contact details only for **mutual** friends; strangers limited to the same community, username only (`basic/views/home.py`, tests in `test_security.py`) |
| "Find people" page read the **whole Firestore `users` collection** (phones, roll numbers) in the browser | Server-rendered from Postgres, same community, public fields only (`views/friends.py:user_list`) |
| Profile page copied phone, roll no. etc. into Firestore | Removed: personal data stays in Postgres |
| Sybil farming: 1,500 free points per new account | Unverified accounts get 100; the rest is held in a ledger account and released on phone verification or campus email (`ledger.services.release_bonus`) |
| One phone number could verify many accounts | One verified phone number per account |
| Web forms had no rate limits | `basic/ratelimit.py` on post task, chat send, disputes, friend requests, phone verification |
| Phone verification echoed raw exception text | Generic message; details logged |
| Production could silently run on SQLite | Startup fails without `DATABASE_URL` when `DEBUG` is off |
| Default `/admin/` path, missing headers | `ADMIN_URL`, `X-Frame-Options: DENY`, `Referrer-Policy`, HSTS via env, https OAuth redirects; `check --deploy` clean |
| Firebase key had to be a file | `FIREBASE_SERVICE_ACCOUNT_JSON` env var; `.vercelignore` blocks `.env`, key files and databases from uploads |
| Dead `basic/views.py` (old copy of the leak), `test_env.py` | Deleted |

## Rules for contributors
- Never put business rules in views; use the services ([BUSINESS_LAYER.md](BUSINESS_LAYER.md)).
- Every state-changing endpoint: POST, CSRF (web) or token (API), service call, test.
- Local development uses `REALTIME_ENABLED=False` and a **separate** Firebase project if realtime is needed.
