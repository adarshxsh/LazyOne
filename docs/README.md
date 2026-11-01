# LazyOne Docs - Start Here

> Snapshot **2026-11-01** · branch `arch-v2/phase0` (local, not pushed, not deployed) · 98 tests green on SQLite + Postgres

## Status at a glance

| Area | State |
|------|-------|
| Phase 0 hardening (money bugs, POST-only, secrets) | ✅ done |
| Ledger, task state machine, outbox | ✅ done |
| API v1 + communities | ✅ done |
| **Wave distribution** | 🔒 **locked v1** - adaptive schedule, optimized feed, measured |
| Dispute resolution (peer jury) | ✅ v1 built - **next to review in depth** |
| Push (FCM), sync endpoint, chat safety poll | 📝 designed, not built |
| Deployment | ⏳ after local work; hosting not chosen |
| Security | ⚠️ core protected; open items before launch |

## Read by purpose

| I want to… | Read |
|------------|------|
| Run it on my machine | [Development/LOCAL_DEV.md](Development/LOCAL_DEV.md) |
| Understand the overall system | [Ideation/ARCHITECTURE_V2.md](Ideation/ARCHITECTURE_V2.md) |
| Understand the business rules | [Development/BUSINESS_LAYER.md](Development/BUSINESS_LAYER.md) |
| Understand waves (core idea) | [Ideation/WAVE_DISTRIBUTION.md](Ideation/WAVE_DISTRIBUTION.md) 🔒 |
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
- **Bond** - points the dispute raiser locks; refunded if they win, shared by majority jurors if they lose.
- **Outbox** - DB table of side effects, executed after commit with retries.
- **Community** - campus/hostel group; all marketplace data is scoped to one.
