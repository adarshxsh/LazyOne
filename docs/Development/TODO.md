# TODO

> Updated 2026-11-01. Local-first: everything here is built and verified locally before deployment.

## Next
- [ ] **Dispute resolution deep-dive** (review v1 rules, edge cases, then lock like waves)
- [ ] Fix home graph leaking non-friends' phone numbers ([SECURITY #3](SECURITY.md))
- [ ] Sybil protection: verification before bonus / jury duty ([SECURITY #2](SECURITY.md))
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
- [ ] Deploy `firestore.rules` · delete `claim-jules-pr.yml` · close duplicate bot PRs · unify `master`/`deployment` · push branch

## Deployment ([plan](../Ideation/DEPLOYMENT.md))
- [ ] Choose hosting A/B · production checklist
