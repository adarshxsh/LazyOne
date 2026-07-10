# Architectural Decisions

This document logs architectural design decisions made during the development of LazyOne.

## 1. Django Channels & WebSockets
- **Decision**: Use Django Channels with a Redis back-end instead of polling.
- **Rationale**: Direct task negotiations and chat messages require real-time updates. Django Channels provides robust WebSocket capabilities integrated with Django's existing authentication and model layer.

## 2. Firebase Admin SDK
- **Decision**: Integrate Firebase for Authentication, Google Sign-in, and SMS Verification.
- **Rationale**: Outsourcing secure password handling, OAuth flows, and SMS verification reduces backend complexity and liability. The Python Admin SDK enables secure token verification.

## 3. SQLite Database (Local)
- **Decision**: SQLite for development.
- **Rationale**: Zero setup cost and easy versioning in development.
- **Next Steps**: A migration plan is needed to move to a scalable database engine (e.g., PostgreSQL) for staging and production on AWS.