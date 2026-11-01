# LazyOne

Hand small tasks to people you trust, for points. A campus task-sharing app where a new task reaches your **closest friends first** and spreads outward in waves, payments are held in escrow, and disagreements are settled by a **randomly drawn peer jury**.

> Status: v3 architecture built and tested locally (98 tests, SQLite + Postgres). Not yet deployed. See **[docs/README.md](docs/README.md)**.

## What it does

| Feature | How |
|---------|-----|
| **Wave distribution** | Close friends see a task immediately, then wider circles, then the whole campus. Empty circles are skipped |
| **Points with escrow** | Rewards are reserved when you post and released on approval (auto-approve after 48 h). Double-entry ledger, so no double payouts |
| **Peer-jury disputes** | Refundable bond, evidence window, 5 jurors with no close ties to either side, hidden votes, majority decides or a moderator steps in |
| **Task chat** | Private to the two people involved (plus jurors during a dispute) |
| **Communities** | Each campus is its own space, joined by email domain |
| **API for mobile** | `/api/v1` with Firebase login, safe retries and rate limits, for the Android app |

## Quick start (local)

```sh
brew services start postgresql@18 && createdb lazyone
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set DATABASE_URL
python manage.py migrate && python manage.py seed_demo
python manage.py runserver      # + in another terminal: python manage.py run_jobs --loop
```
Open http://127.0.0.1:8000 and log in as `bala@example.com` / `lazyone123`. Full guide: [docs/Development/LOCAL_DEV.md](docs/Development/LOCAL_DEV.md).

## Tech
Django 5.2 · PostgreSQL · Django REST Framework · Firebase Auth (+ Firestore as a read-only realtime mirror) · django-allauth (Google login) · GitHub Actions CI.

## Docs
- [Architecture](docs/Ideation/ARCHITECTURE_V2.md) · [Business layer](docs/Development/BUSINESS_LAYER.md) · [Wave distribution 🔒](docs/Ideation/WAVE_DISTRIBUTION.md)
- [API](docs/Development/API.md) · [Security status](docs/Development/SECURITY.md) · [Delivery & realtime](docs/Ideation/DELIVERY_AND_REALTIME.md) · [Deployment](docs/Ideation/DEPLOYMENT.md)
- [Decisions](docs/Development/DECISIONS.md) · [Changelog](docs/Development/CHANGELOG.md) · [TODO](docs/Development/TODO.md)

## Tests
```sh
python manage.py test basic
```
