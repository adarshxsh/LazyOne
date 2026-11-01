# Security Status

> Last reviewed 2026-11-01 on branch `arch-v2/phase0` (local only, **not deployed**).
> Honest summary: the money, task and dispute core is protected; several items below must be fixed before launch.

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
| 1 | `firestore.rules` written but **not deployed**; console rules likely permissive (old chat wrote from the browser) | Anyone with the public Firebase config may read/write existing Firestore data **now** | `firebase deploy --only firestore:rules` | ⛔ needs owner |
| 2 | Sybil point farming: every new account gets 1500 points | Alt accounts funnel points to a main account / stack juries | Gate bonus + jury duty behind phone or campus-email verification | open |
| 3 | Home friend-circle graph sends phone/Instagram of up to 100 **non-friends** (`basic/views/home.py`) | PII exposure to any logged-in user | Contact details for accepted friends only | open |
| 4 | `claim-jules-pr.yml` on `master` interpolates `head_ref` into shell under `pull_request_target` | Script injection with a write token | Delete the workflow | ⛔ needs owner |
| 5 | Rate limits per process, API only | Ineffective with >1 worker; web forms unlimited | Redis-backed limits or Cloudflare rules | deploy-time |
| 6 | Unreviewed: profile, phone verification, `user_list.html` (builds HTML from Firestore data) | Possible XSS / authz bugs | Review | open |
| 7 | Vercel preview deployments of ~300 bot PRs | Secrets exposed to bot code if shared with previews | Check env-var scopes; close PRs | ⛔ needs owner |
| 8 | Old hardcoded secret key in git history | Session forgery if prod ever ran without the env var | New key in production | deploy-time |
| 9 | HSTS off, no CSP, `/admin/` default path, no admin 2FA | Defence in depth | Enable at deploy | deploy-time |
| 10 | Test docs written to the real Firestore during early local runs (`conversations/<id>` with `member_uids` only) | Clutter, no personal data | Clean-up script | open |

## Rules for contributors
- Never put business rules in views; use the services ([BUSINESS_LAYER.md](BUSINESS_LAYER.md)).
- Every state-changing endpoint: POST, CSRF (web) or token (API), service call, test.
- Local development uses `REALTIME_ENABLED=False` and a **separate** Firebase project if realtime is needed.
