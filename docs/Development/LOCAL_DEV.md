# Local development

Everything runs on your machine first; servers come later.

## One-time setup
```sh
brew services start postgresql@18
createdb lazyone
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set DATABASE_URL, keep REALTIME_ENABLED=False
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_demo    # 12 demo users, password: lazyone123
```

## Run (two terminals)
```sh
python manage.py runserver            # http://127.0.0.1:8000
python manage.py run_jobs --loop      # waves, auto-approve, jury tallies, outbox retries
```

## Try the core flows
| Flow | How |
|------|-----|
| Waves | Log in as `bala` (closeness 90 with asha) → sees asha's tasks now. `chen` (50) after 10 min, `dev`/`esha` after 30 min, everyone after 2 h. |
| Task lifecycle | Take → Submit Work → poster Approve & Pay (or wait 48 h for auto-approve). |
| Jury | The seed prints the jurors of a dispute; log in as one, open **My Tasks → Jury Duty**, vote. |
| Ledger | `python manage.py check_ledger` must print `Ledger OK`. Admin → Ledger shows every transfer. |
| API | `/api/v1/me/` etc. with the session cookie, or `Authorization: Bearer <Firebase ID token>`. |

## Tests
```sh
python manage.py test basic                                             # SQLite, fast
DATABASE_URL=postgres://$USER@localhost:5432/lazyone_ci python manage.py test basic --noinput   # Postgres, incl. race tests
```
Tests never contact Firebase. CI runs the Postgres variant on every push.

## Realtime locally
With `REALTIME_ENABLED=False` chat polls Django every 3 s. To test Firestore realtime, use a **separate dev Firebase project** (not production), set `REALTIME_ENABLED=True` and deploy `firestore.rules` to that project.
