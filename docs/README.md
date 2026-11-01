# LazyOne Docs - Start Here

> Snapshot **2026-11-01** · branch `arch-v2/phase0` (local, not pushed, not deployed) · 123 tests green on SQLite + Postgres

## Status at a glance

| Area | State |
|------|-------|
| Phase 0 hardening (money bugs, POST-only, secrets) | ✅ done |
| Ledger, task state machine, outbox | ✅ done |
| API v1 + communities | ✅ done |
| **Wave distribution** | 🔒 **locked v1.1** - adaptive schedule, optimized feed, prestige head start (D15) |
| Dispute resolution v2 + prestige | ✅ built - settlement → jury → appeal; questions answered, ready to lock · AI assist planned (D16) |
| Push (FCM), sync endpoint, chat safety poll | 📝 designed, not built |
| Deployment | ⏳ Vercel chosen; code production-ready ([steps](Ideation/DEPLOYMENT.md#deploy-to-vercel-step-by-step)) |
| Security | ✅ code fixed, history purged, Firestore locked · owner: GitHub Support request, notify affected people |

## Read by purpose

| I want to… | Read |
|------------|------|
| Run it on my machine | [Development/LOCAL_DEV.md](Development/LOCAL_DEV.md) |
| Understand the overall system | [Ideation/ARCHITECTURE_V2.md](Ideation/ARCHITECTURE_V2.md) |
| Understand the business rules | [Development/BUSINESS_LAYER.md](Development/BUSINESS_LAYER.md) |
| Understand waves (core idea) | [Ideation/WAVE_DISTRIBUTION.md](Ideation/WAVE_DISTRIBUTION.md) 🔒 |
| Understand disputes & prestige | [Ideation/DISPUTE_RESOLUTION.md](Ideation/DISPUTE_RESOLUTION.md) |
| Know how messages/notifications reach users | [Ideation/DELIVERY_AND_REALTIME.md](Ideation/DELIVERY_AND_REALTIME.md) |
| Build the Android app | [Development/API.md](Development/API.md) |
| Know what's secure and what isn't | [Development/SECURITY.md](Development/SECURITY.md) |
| Plan hosting | [Ideation/DEPLOYMENT.md](Ideation/DEPLOYMENT.md) |
| See why things are the way they are | [Development/DECISIONS.md](Development/DECISIONS.md) |
| See what changed / what's next | [Development/CHANGELOG.md](Development/CHANGELOG.md) · [Development/TODO.md](Development/TODO.md) |
| Review the old PR backlog | [Development/PR_REVIEW_2026-10-31.md](Development/PR_REVIEW_2026-10-31.md) |

## The system in one picture

```
Web (templates) ─┐                          ┌─▶ PostgreSQL  (source of truth: users, tasks,
Android app ─────┼─HTTPS─▶ Django ─services─┤                ledger, disputes, chat, outbox)
                 │         (one app)        └─▶ Outbox ─▶ notifications · Firestore mirror · (FCM)
                 └── read-only live updates ◀── Firestore          ▲
                                                 run_jobs every minute: expiry, auto-approve,
                                                 jury tallies, wave alerts, retries
```

## Glossary
- **Wave** - ring of people who can see a task; closest friends first, whole community last.
- **Circle** - for one viewer: which posters rated them (and how close) + friends-of-friends.
- **Escrow** - ledger account holding a task's reward until it's paid or refunded.
- **Bond** - points the dispute raiser locks; refunded if settled or they win, shared by coherent jurors if they lose.
- **Prestige** - reputation score; matters more than points, gates jury duty, shown as a tier (Newcomer → Elder).
- **Settlement** - first dispute stage: the parties trade offers (taker's share %) before any jury.
- **Outbox** - DB table of side effects, executed after commit with retries.
- **Community** - campus/hostel group; all marketplace data is scoped to one.
