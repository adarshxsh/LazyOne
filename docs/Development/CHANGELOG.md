# Changelog

All notable changes to the LazyOne project are documented in this file.

## [2.0.1] - Unreleased
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
