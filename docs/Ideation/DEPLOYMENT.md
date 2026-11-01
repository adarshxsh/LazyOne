# Deployment Plan

> Status: **not deployed.** Hosting chosen 2026-11-01: **Vercel (option A)**. The code is production-ready; follow [Deploy to Vercel](#deploy-to-vercel-step-by-step) after the owner-only security steps in [SECURITY.md](../Development/SECURITY.md).

## Target load: 1,000 concurrent users

| Component | Expected load | Verdict |
|-----------|---------------|---------|
| Django | 50–100 req/s avg, ~300 peak | 1 × 2 vCPU, 4–8 workers |
| Postgres | < 100 queries/s, rare row-lock contention | fine; connection count is the real limit (use a pooler) |
| Feed (waves) | ~20 loads/s × 2–3 ms | ~4–6 % of one core (measured, [WAVE_DISTRIBUTION.md](WAVE_DISTRIBUTION.md)) |
| Chat | 3 s polling would add ~330 req/s | prefer Firestore or adaptive polling ([DELIVERY_AND_REALTIME.md](DELIVERY_AND_REALTIME.md)) |
| Scheduled jobs | every minute | needs a real cron/worker |

## Hosting options (choose later)

| | A. Vercel + managed free tiers (₹0) | B. One small VM (~€5–15/month) - recommended |
|---|---|---|
| App | Vercel serverless | gunicorn in Docker |
| DB | Managed Postgres free tier + **pooler** (`DB_CONN_MAX_AGE=0`) | Postgres in Docker + nightly backups |
| Jobs | External cron → `GET /internal/cron/` | `run_jobs --loop` container |
| Cache / rate limits | Free Redis tier | Redis container |
| Realtime | Firestore only (no WebSockets on Vercel) | Firestore, or Channels WebSockets later |
| Edge | Vercel | **Caddy** or **Cloudflare Tunnel** |
| Weak spots | cold starts, free-tier limits, cron frequency limits | single machine → backups + uptime monitor |

## Deploy to Vercel (step by step)

**0. Before anything:** purge `db.sqlite3` from the git history and deploy `firestore.rules` ([SECURITY.md](../Development/SECURITY.md) #1, #2). Recommended: a **new Firebase project** for production.

**1. Free managed services**

| Need | Service (free tier) | Env var |
|------|---------------------|---------|
| Postgres | Neon or Supabase, **pooled** connection string | `DATABASE_URL`, `DB_CONN_MAX_AGE=0` |
| Shared cache | Upstash Redis | `REDIS_URL` |
| Email (optional) | Brevo / Resend / Gmail app password (SMTP). **Not needed to launch:** sign-up is Google-only until `EMAIL_HOST` is set, then email + password sign-up turns on | `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` |
| Frequent jobs | cron-job.org (every 1–5 min) → `GET https://<app>/internal/cron/` with header `Authorization: Bearer <CRON_SECRET>` | `CRON_SECRET` |

Vercel Hobby cron runs **once a day** only; `vercel.json` registers a daily backstop, and the external cron does the real work (expiry, 48 h auto-approve, jury deadlines, wave alerts, outbox retries).

**2. Vercel project:** import the GitHub repo → set every variable from the Production block in [`.env.example`](../../.env.example) (Production scope only, not Preview) → deploy. Files: [`vercel.json`](../../vercel.json), [`.python-version`](../../.python-version) (3.12), [`.vercelignore`](../../.vercelignore) (keeps `.env`, key files, databases out of uploads).

**3. Database schema** (from your laptop, once per release):
```bash
DATABASE_URL='<direct (non-pooled) URL>' python manage.py migrate
DATABASE_URL='<direct URL>' python manage.py createsuperuser
```

**4. Google sign-in:** in Google Cloud Console add `https://<app>.vercel.app/accounts/google/login/callback/` (and your domain) to the OAuth client's redirect URIs. In Django admin, set **Sites** → domain to your production host.

**5. Firebase (realtime chat + phone verification):** put the service-account JSON in `FIREBASE_SERVICE_ACCOUNT_JSON`, the web config in `FIREBASE_*`, add the Vercel domain to Firebase Auth → Authorized domains.

**6. Check:** `/healthz/` returns `{"ok": true}`; sign up, verify email, post a task from a second account; run `python manage.py check_ledger` against production.

**7. After HTTPS works on the final domain:** `SECURE_HSTS_SECONDS=31536000`.

## Edge: Caddy (option B)

```
Internet ─HTTPS:443─▶ Caddy ─HTTP─▶ gunicorn/Django (127.0.0.1:8000)
                        └─ /static/ served directly, gzip, HTTP→HTTPS, WebSocket-ready
```
- Automatic Let's Encrypt certificates, TLS termination, buffering of slow mobile clients (protects sync workers).
- Alternatives: nginx + certbot; **Cloudflare Tunnel** (free TLS, no open ports, DDoS protection); Cloudflare in front of Caddy.

```caddyfile
lazyone.example.com {
    encode gzip
    handle_path /static/* { root * /srv/static; file_server }
    reverse_proxy 127.0.0.1:8000
}
```

## Production checklist

- [ ] `DEBUG=False`, new `DJANGO_SECRET_KEY` (never the old committed one)
- [ ] `DATABASE_URL` (Postgres), `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`
- [ ] `CRON_SECRET` + cron/worker calling `run_jobs` every minute
- [ ] `firebase deploy --only firestore:rules` (from repo `firestore.rules`)
- [ ] Shared cache (Redis) for rate limits and the wave circle cache
- [ ] `SECURE_HSTS_SECONDS` once HTTPS is permanent; CSP; move `/admin/`
- [ ] `python manage.py check --deploy` clean; `check_ledger` nightly with alert
- [ ] Sentry (or similar) + `/healthz/` uptime monitor; DB backups tested
- [ ] Resolve items in [SECURITY.md](../Development/SECURITY.md)
