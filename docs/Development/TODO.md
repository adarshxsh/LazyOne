# TODO

> Updated 2026-11-01. Local-first: everything here is built and verified locally before deployment.

## Next
- [x] Dispute resolution v2 + prestige ([design](../Ideation/DISPUTE_RESOLUTION.md))
- [x] Answer the open dispute questions (prestige head start on waves: D15)
- [ ] Lock disputes v2
- [ ] AI dispute assistance behind a kill switch ([plan](../Ideation/DISPUTE_RESOLUTION.md#next-iteration-ai-assistance-planned-not-built), D16)
- [ ] Show prestige/tier on profiles and the navbar
- [x] Fix home graph / Find people PII leaks
- [x] Sybil protection: bonus held until verification, one phone per account
- [ ] `social` service (friend requests, acceptor sets own closeness)

## Delivery & realtime ([design](../Ideation/DELIVERY_AND_REALTIME.md))
- [ ] Chat safety poll (20 s) + adaptive polling
- [ ] Notification `delivered_at`/`read_at`, `/api/v1/sync?since=`
- [ ] FCM device tokens + push handler; minimum-app-version endpoint; App Check

## Waves v2 (not locked, [ideas](../Ideation/WAVE_DISTRIBUTION.md#future-v2---not-locked))
- [ ] Record taker's wave + admin metrics (first)
- [ ] Trust-based closeness · per-community `WaveConfig` · notification caps

## Scale (local)
- [ ] Locust load test at 1,000 users · Redis cache/throttles

## Needs owner action
- [ ] **Purge `db.sqlite3` (real user data) from git history** + notify affected people
- [ ] Deploy `firestore.rules` · delete `claim-jules-pr.yml` · close duplicate bot PRs · unify `master`/`deployment` · push branch

## Deployment ([plan](../Ideation/DEPLOYMENT.md))
- [x] Hosting: Vercel; code production-ready
- [ ] Neon/Supabase + Upstash + SMTP + external cron; deploy
