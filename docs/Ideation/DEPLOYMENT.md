# Deployment Plan

> Status: **not deployed.** Workflow is local-first ([LOCAL_DEV.md](../Development/LOCAL_DEV.md)); hosting is chosen after local work is complete.

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
