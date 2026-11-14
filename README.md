# Expense API

A small JSON API for keeping a personal list of expenses. Each person has an account and sees
only their own rows. Creating an expense is safe to retry.

- **Live:** `https://YOUR-APP-URL` &nbsp;·&nbsp; **Repo:** `https://github.com/YOUR-USERNAME/expense-api`
- Stack: Python 3.12, Flask, SQLite (WAL), gunicorn. No secrets are needed to run it (see below).

## Try it

```bash
BASE=https://YOUR-APP-URL

# 1. Create an account. The response contains your token.
curl -X POST $BASE/register -H 'Content-Type: application/json' \
     -d '{"email":"you@example.com","password":"at-least-8-chars"}'

# 2. Create an expense. Idempotency-Key is required; use a fresh UUID per new expense.
curl -X POST $BASE/expenses -H "Authorization: Bearer $TOKEN" \
     -H 'Idempotency-Key: 6f1c2a9e-0001' -H 'Content-Type: application/json' \
     -d '{"description":"Lunch","amount_cents":1250,"paid_on":"2026-11-14"}'

# 3. Send the exact same request again: same response, header "Idempotent-Replayed: true", still one row.

# 4. List yours.
curl $BASE/expenses -H "Authorization: Bearer $TOKEN"
```

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /health` | no | liveness |
| `POST /register` | no | `{email, password}` → `201 {id, email, token}` |
| `POST /login` | no | `{email, password}` → `{token}` |
| `POST /expenses` | yes | `{description, amount_cents, paid_on?}` + `Idempotency-Key` header |
| `GET /expenses?limit=50` | yes | your expenses, newest first |
| `GET /expenses/<id>` | yes | one of your expenses |
| `DELETE /expenses/<id>` | yes | delete one of your expenses |

Amounts are integer cents, never floats.

## Accounts and isolation

Passwords are hashed with scrypt (Werkzeug). Tokens are random 256-bit strings; only their SHA-256 is
stored, so a leaked database does not leak usable tokens. That is also why there is no signing secret to
configure or commit.

- No `Authorization` header, a malformed one, or an unknown token → **401** with `WWW-Authenticate: Bearer`.
- Every query on `expenses` filters on `user_id` taken from the token, never from the request. Asking for
  another user's expense returns **404**, the same response as an id that does not exist, so existence is
  not leaked.
- Idempotency keys are scoped per user, so one user cannot replay or collide with another's.

`test_api.py::test_user_cannot_read_or_write_another_users_rows` is the proof: Alice creates an expense;
Bob cannot read it by id, does not see it in his list, cannot delete it (404 each time), and Alice's row
is still intact afterwards. `test_idempotency_keys_are_scoped_per_user` covers the replay side.

## Retry-safe write: `POST /expenses`

A duplicate expense silently inflates someone's totals, and clients do retry (timeouts, double taps,
flaky mobile networks), so this is the write that must be safe.

**How a repeat is recognised:** by the pair **(authenticated user, `Idempotency-Key` header)**. The
client picks the key and reuses it when retrying.

1. First request with a key: the expense is inserted and the exact response is stored with the key.
   Both happen in one transaction.
2. Same user, same key, same body: nothing is inserted; the **stored first response** is returned with
   the same status (`201`) and an extra `Idempotent-Replayed: true` header.
3. Same key, **different** body: `422 idempotency_key_reused`, so a client bug cannot silently
   "succeed" with the wrong data.
4. Bodies are compared as canonical JSON, so key order and whitespace do not matter.
5. Requests that fail validation do not consume the key; fix the body and retry with the same key.

Concurrent duplicates are handled too: the lookup and the insert run inside `BEGIN IMMEDIATE`, which
takes the database write lock first, so two simultaneous identical requests cannot both see "no prior
key". The `(user_id, key)` primary key is a second backstop. `test_concurrent_identical_requests_create_exactly_one_row`
fires 12 at once and checks there is one row and one id. (On PostgreSQL you would rely on that primary
key, via `INSERT ... ON CONFLICT`, in place of the write lock.)

Keys are stored indefinitely in this version. A production system would expire them after e.g. 24 hours.

## Errors

Every error, including unknown routes, wrong methods and unexpected failures, has one JSON shape:

```json
{"error": {"code": "validation_error",
           "message": "Fix these fields and resend: description, amount_cents.",
           "fields": [
             {"field": "description", "problem": "required string, 1-200 characters", "got": ""},
             {"field": "amount_cents", "problem": "required integer number of cents, from 1 to 1000000000 (e.g. 1250 for 12.50)", "got": "12"}]}}
```

`code` is stable for programs; `message` says what to do; `fields` names each bad field, what is wrong
with it and what was received. Statuses: `400` malformed request (bad JSON, missing `Idempotency-Key`),
`401` credentials, `404` not found, `405` wrong method (lists allowed ones), `409` email taken,
`422` well-formed but invalid, `500` is deliberately vague and never leaks internals.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest -q                                             # 23 tests
python app.py                                         # http://localhost:5000
```

Configuration (all optional): `DATABASE_PATH` (default `data/expenses.db`), `PORT`.

## Deploy

The `Dockerfile` runs gunicorn on `$PORT`, so any container host works. The only state is the SQLite
file at `DATABASE_PATH` (default `/data/expenses.db` in the image), which **must sit on a persistent
volume**, otherwise data disappears on every redeploy.

**Fly.io** (free allowance, volumes supported):
```bash
fly launch --no-deploy --internal-port 8000      # accept the generated fly.toml
fly volumes create data --size 1
# add to fly.toml:   [mounts]  source = "data"  destination = "/data"
fly deploy
```
**Render:** New → Web Service → connect the repo → Docker. Add a Disk mounted at `/data` (paid plans only;
on the free plan the data is wiped on restart, fine for a demo, not for real use).

## Secrets

None are committed and none are required: there is no signing key (tokens are random and stored hashed),
no third-party API key, and the database path is the only setting. `.gitignore` excludes `.env` and
`*.db` in case you add any later; set real secrets in your host's environment settings, never in the repo.

## Known limits

No rate limiting on `/login`, no password reset or email verification, no token revocation endpoint, no
pagination beyond `limit`. SQLite serialises writes, so this suits one small instance, not horizontal scale.
