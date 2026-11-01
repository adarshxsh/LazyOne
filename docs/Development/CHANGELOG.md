# Changelog

All notable changes to the LazyOne project are documented in this file.

## [3.0.0-dev] - Unreleased (branch `arch-v2/phase0`)
### Security & correctness
- Fixed: double payout on double-click, points minted on accepted cancellation, re-payout via withdrawing a resolved dispute, GET-triggerable task actions (CSRF), hardcoded secret key, chat username XSS.
- Disputes and disputed chats now private to parties, jurors and staff; browser can no longer write Firestore.
- Fixed `/login/` 404 and logout crash; friend accept/decline are POST-only.
### Added
- Double-entry points ledger (`ledger` app) with escrow, bonds, `check_ledger`.
- Task state machine with audit trail; `submitted` (48 h auto-approve) and `expired` states.
- Transactional outbox (`outbox` app) + `run_jobs` / `/internal/cron/`.
- REST API `/api/v1` (Firebase token auth, idempotency keys, throttling) and communities by email domain.
- Wave distribution 🔒 v1: adaptive schedule, cached circles, 3.5–4.5× faster feed.
- Disputes v2: settlement offers with split outcomes, default judgment, jury sized by reward, required vote reasons, median verdict, one appeal, moderator.
- Prestige: reputation log, tiers, juror eligibility, anti-farming pair cap.
- `seed_demo`, `LOCAL_DEV.md`, CI on Postgres, 115 tests.

## [2.0.1] - 2026-10
### Changed
- Swapped out internal database IDs for secure, 16-character `public_id`s across Tasks, Chats, and Disputes.
- Fixed a sneaky loophole where task cancellations could bypass active disputes.
- Upgraded the Dispute engine! You can now reopen a withdrawn dispute, and the backend has a `resolve_dispute` safety net to prevent tasks from getting stuck in an "Eternal Dispute".
- Squashed a bug that was wiping the history of withdrawn disputes when a task was completed.

## [1.0.0] - 2025-11-16
### Added
- Deployment configurations and setup for Amazon Web Services (AWS).
- Finalized database indexes and production environment configurations.

## [0.9.0] - 2025-10-07
### Changed
- Refactored `LazyOne/settings.py` settings for security and environment variable integration.

## [0.8.0] - 2025-10-06
### Added
- Completed Django Channels and WebSocket chat updates with direct/task chat interfaces.
- Implemented real-time updates and templates (`chat.html`, `friends.html`, and `user_list.html`).
- Integrated dynamic social circle node visualization on the home page.
- Completed Firebase Authentication backend with helper endpoints.
- Added database migrations covering `Conversation`, `Friendship`, `FriendRequest`, and `Notification` models.
- Integrated friend list views and user profiles views with customizable closeness metrics.
