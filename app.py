"""Expense API: accounts, per-user expenses, retry-safe expense creation."""
import hashlib, json, logging, os, re, secrets, sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,            -- sha256 of the token; the token itself is never stored
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expenses (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    description  TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    paid_on      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS expenses_by_user ON expenses(user_id, paid_on DESC, id DESC);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    user_id      INTEGER NOT NULL REFERENCES users(id),
    key          TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status       INTEGER NOT NULL,
    response     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)               -- keys are scoped per user
);
"""

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DUMMY_HASH = generate_password_hash("not-a-real-password")  # equalises login timing for unknown emails
log = logging.getLogger("expense-api")


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ApiError(Exception):
    def __init__(self, status, code, message, fields=None, headers=None):
        self.status, self.code, self.message = status, code, message
        self.fields, self.headers = fields, headers or {}


def create_app(db_path=None):
    app = Flask(__name__)
    db_path = db_path or os.environ.get("DATABASE_PATH", "data/expenses.db")
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    def connect():
        db = sqlite3.connect(db_path, timeout=10, isolation_level=None)  # we manage transactions
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        return db

    boot = connect(); boot.executescript(SCHEMA); boot.close()

    def get_db():
        if "db" not in g:
            g.db = connect()
        return g.db

    @app.teardown_appcontext
    def close_db(_):
        db = g.pop("db", None)
        if db:
            db.close()

    @contextmanager
    def tx():
        """BEGIN IMMEDIATE takes the write lock up front, so check-then-write blocks are atomic."""
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    # ---------- error handling: one JSON shape for everything ----------
    def error_response(status, code, message, fields=None, headers=None):
        body = {"error": {"code": code, "message": message}}
        if fields:
            body["error"]["fields"] = fields
        resp = jsonify(body)
        resp.status_code = status
        for k, v in (headers or {}).items():
            resp.headers[k] = v
        return resp

    @app.errorhandler(ApiError)
    def on_api_error(e):
        return error_response(e.status, e.code, e.message, e.fields, e.headers)

    @app.errorhandler(HTTPException)
    def on_http_error(e):
        if e.code == 404:
            return error_response(404, "route_not_found", f"No route {request.method} {request.path}.")
        if e.code == 405:
            allowed = sorted(e.valid_methods or [])
            return error_response(405, "method_not_allowed",
                                  f"{request.method} is not allowed on {request.path}. Allowed: {', '.join(allowed)}.",
                                  headers={"Allow": ", ".join(allowed)})
        return error_response(e.code or 500, "http_error", e.description)

    @app.errorhandler(Exception)
    def on_unexpected(e):
        log.exception("unhandled error")
        return error_response(500, "internal_error", "Something went wrong on our side. Retrying later may work.")

    # ---------- helpers ----------
    def json_body():
        if not request.get_data():
            raise ApiError(400, "empty_body", "Send a JSON object as the request body with Content-Type: application/json.")
        data = request.get_json(silent=True)
        if data is None:
            raise ApiError(400, "invalid_json",
                           "Body is not valid JSON. Send a JSON object with Content-Type: application/json.")
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Body must be a JSON object, e.g. {\"key\": \"value\"}.")
        return data

    def reject(fields):
        names = ", ".join(f["field"] for f in fields)
        raise ApiError(422, "validation_error", f"Fix these fields and resend: {names}.", fields)

    def new_token(db, user_id):
        token = "ex_" + secrets.token_urlsafe(32)
        db.execute("INSERT INTO tokens(token_hash, user_id, created_at) VALUES (?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), user_id, utcnow()))
        return token

    def require_auth(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            header = request.headers.get("Authorization", "")
            if not header:
                raise ApiError(401, "missing_credentials",
                               "Send an 'Authorization: Bearer <token>' header. Get a token from POST /register or POST /login.",
                               headers={"WWW-Authenticate": "Bearer"})
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                raise ApiError(401, "malformed_credentials",
                               "Authorization header must look like: Bearer <token>.",
                               headers={"WWW-Authenticate": "Bearer"})
            row = get_db().execute(
                "SELECT user_id FROM tokens WHERE token_hash=?",
                (hashlib.sha256(token.strip().encode()).hexdigest(),)).fetchone()
            if not row:
                raise ApiError(401, "invalid_credentials", "This token is not valid. Log in again at POST /login.",
                               headers={"WWW-Authenticate": "Bearer"})
            g.user_id = row["user_id"]
            return fn(*a, **kw)
        return wrapper

    def expense_json(r):
        return {"id": r["id"], "description": r["description"], "amount_cents": r["amount_cents"],
                "paid_on": r["paid_on"], "created_at": r["created_at"]}

    # ---------- public routes ----------
    @app.get("/health")
    def health():
        get_db().execute("SELECT 1")
        return jsonify(status="ok")

    @app.post("/register")
    def register():
        data = json_body()
        problems = []
        email = data.get("email")
        pw = data.get("password")
        if not isinstance(email, str) or not EMAIL_RE.match(email.strip()):
            problems.append({"field": "email", "problem": "must be a valid email address, e.g. you@example.com"})
        if not isinstance(pw, str) or len(pw) < 8:
            problems.append({"field": "password", "problem": "must be a string of at least 8 characters"})
        if problems:
            reject(problems)
        email = email.strip().lower()
        try:
            with tx() as db:
                cur = db.execute("INSERT INTO users(email, password_hash, created_at) VALUES (?,?,?)",
                                 (email, generate_password_hash(pw), utcnow()))
                token = new_token(db, cur.lastrowid)
        except sqlite3.IntegrityError:
            raise ApiError(409, "email_taken", "An account with this email already exists. Use POST /login instead.",
                           [{"field": "email", "problem": "already registered"}])
        return jsonify(id=cur.lastrowid, email=email, token=token), 201

    @app.post("/login")
    def login():
        data = json_body()
        email, pw = data.get("email"), data.get("password")
        if not isinstance(email, str) or not isinstance(pw, str):
            reject([{"field": f, "problem": "is required and must be a string"}
                    for f in ("email", "password") if not isinstance(data.get(f), str)])
        db = get_db()
        row = db.execute("SELECT id, password_hash FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        ok = check_password_hash(row["password_hash"] if row else DUMMY_HASH, pw)
        if not (row and ok):
            raise ApiError(401, "invalid_credentials", "Email or password is incorrect.")
        with tx() as db:
            token = new_token(db, row["id"])
        return jsonify(token=token)

    # ---------- protected routes ----------
    @app.get("/expenses")
    @require_auth
    def list_expenses():
        try:
            limit = int(request.args.get("limit", 50))
            if not 1 <= limit <= 200:
                raise ValueError
        except ValueError:
            reject([{"field": "limit", "problem": "query parameter must be an integer from 1 to 200",
                     "got": request.args.get("limit")}])
        rows = get_db().execute(
            "SELECT * FROM expenses WHERE user_id=? ORDER BY paid_on DESC, id DESC LIMIT ?",
            (g.user_id, limit)).fetchall()
        return jsonify([expense_json(r) for r in rows])

    @app.get("/expenses/<int:expense_id>")
    @require_auth
    def get_expense(expense_id):
        row = get_db().execute("SELECT * FROM expenses WHERE id=? AND user_id=?", (expense_id, g.user_id)).fetchone()
        if not row:  # 404, not 403: don't reveal that someone else's row exists
            raise ApiError(404, "expense_not_found", f"No expense with id {expense_id} on your account.")
        return jsonify(expense_json(row))

    @app.delete("/expenses/<int:expense_id>")
    @require_auth
    def delete_expense(expense_id):
        with tx() as db:
            cur = db.execute("DELETE FROM expenses WHERE id=? AND user_id=?", (expense_id, g.user_id))
        if not cur.rowcount:
            raise ApiError(404, "expense_not_found", f"No expense with id {expense_id} on your account.")
        return "", 204

    @app.post("/expenses")
    @require_auth
    def create_expense():
        key = request.headers.get("Idempotency-Key")
        if key is None:
            raise ApiError(400, "missing_idempotency_key",
                           "Send an 'Idempotency-Key' header (any unique string, e.g. a UUID, 1-200 characters). "
                           "Reuse the same key when retrying the same request.")
        if not 1 <= len(key) <= 200 or not key.isprintable():
            raise ApiError(400, "invalid_idempotency_key", "Idempotency-Key must be 1-200 printable characters.")

        data = json_body()
        problems = []
        unknown = sorted(set(data) - {"description", "amount_cents", "paid_on"})
        problems += [{"field": u, "problem": "unknown field; allowed: description, amount_cents, paid_on"} for u in unknown]
        desc = data.get("description")
        if not isinstance(desc, str) or not 1 <= len(desc.strip()) <= 200:
            problems.append({"field": "description", "problem": "required string, 1-200 characters", "got": desc})
        amt = data.get("amount_cents")
        if isinstance(amt, bool) or not isinstance(amt, int) or not 1 <= amt <= 1_000_000_000:
            problems.append({"field": "amount_cents",
                             "problem": "required integer number of cents, from 1 to 1000000000 (e.g. 1250 for 12.50)",
                             "got": amt})
        paid_on = data.get("paid_on")
        if paid_on is None:
            paid_on = date.today().isoformat()
        else:
            try:
                if not isinstance(paid_on, str):
                    raise ValueError
                paid_on = date.fromisoformat(paid_on).isoformat()
            except ValueError:
                problems.append({"field": "paid_on", "problem": "must be a date formatted YYYY-MM-DD", "got": paid_on})
        if problems:
            reject(problems)

        # Identify a repeat by (user, Idempotency-Key). The body hash detects key reuse with a different request.
        request_hash = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with tx() as db:  # lookup + insert happen under one write lock, so concurrent duplicates can't both insert
            prior = db.execute("SELECT request_hash, status, response FROM idempotency_keys WHERE user_id=? AND key=?",
                               (g.user_id, key)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise ApiError(422, "idempotency_key_reused",
                                   "This Idempotency-Key was already used with a different request body. "
                                   "Use a new key for a new expense, or resend the original body to retry.")
                resp = app.response_class(prior["response"], status=prior["status"], mimetype="application/json")
                resp.headers["Idempotent-Replayed"] = "true"
                return resp
            cur = db.execute("INSERT INTO expenses(user_id, description, amount_cents, paid_on, created_at) VALUES (?,?,?,?,?)",
                             (g.user_id, desc.strip(), amt, paid_on, utcnow()))
            body = json.dumps(expense_json(db.execute("SELECT * FROM expenses WHERE id=?", (cur.lastrowid,)).fetchone()))
            db.execute("INSERT INTO idempotency_keys(user_id,key,request_hash,status,response,created_at) VALUES (?,?,?,?,?,?)",
                       (g.user_id, key, request_hash, 201, body, utcnow()))
        return app.response_class(body, status=201, mimetype="application/json")

    return app


if __name__ == "__main__":
    create_app().run(port=int(os.environ.get("PORT", 5000)))
