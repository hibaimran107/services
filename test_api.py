import threading
import pytest
from app import create_app


@pytest.fixture
def app(tmp_path):
    return create_app(str(tmp_path / "t.db"))

@pytest.fixture
def client(app):
    return app.test_client()

def register(client, email, pw="correct horse"):
    r = client.post("/register", json={"email": email, "password": pw})
    assert r.status_code == 201, r.get_json()
    return {"Authorization": f"Bearer {r.get_json()['token']}"}

def add(client, auth, key, **body):
    body = {"description": "Coffee", "amount_cents": 450, **body}
    return client.post("/expenses", json=body, headers={**auth, "Idempotency-Key": key})


# ---- 401 on every protected route ----
@pytest.mark.parametrize("method,path", [("get", "/expenses"), ("post", "/expenses"),
                                         ("get", "/expenses/1"), ("delete", "/expenses/1")])
def test_protected_routes_return_401_without_credentials(client, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "missing_credentials"
    assert r.headers["WWW-Authenticate"] == "Bearer"

def test_bad_token_is_401(client):
    r = client.get("/expenses", headers={"Authorization": "Bearer ex_nope"})
    assert r.status_code == 401 and r.get_json()["error"]["code"] == "invalid_credentials"

def test_malformed_authorization_header_is_401(client):
    r = client.get("/expenses", headers={"Authorization": "Token abc"})
    assert r.status_code == 401 and r.get_json()["error"]["code"] == "malformed_credentials"

def test_health_is_public(client):
    assert client.get("/health").status_code == 200


# ---- isolation between users ----
def test_user_cannot_read_or_write_another_users_rows(client):
    alice, bob = register(client, "alice@example.com"), register(client, "bob@example.com")
    eid = add(client, alice, "k1", description="Alice's secret").get_json()["id"]

    # bob cannot read it, directly or via list
    assert client.get(f"/expenses/{eid}", headers=bob).status_code == 404
    assert client.get("/expenses", headers=bob).get_json() == []
    # bob cannot delete it
    assert client.delete(f"/expenses/{eid}", headers=bob).status_code == 404
    # ...and it is still there for alice, untouched
    r = client.get(f"/expenses/{eid}", headers=alice)
    assert r.status_code == 200 and r.get_json()["description"] == "Alice's secret"
    # 404 body is identical to a truly missing id, so existence isn't leaked
    assert client.get(f"/expenses/{eid}", headers=bob).get_json()["error"]["code"] == \
           client.get("/expenses/99999", headers=bob).get_json()["error"]["code"]

def test_idempotency_keys_are_scoped_per_user(client):
    alice, bob = register(client, "alice@example.com"), register(client, "bob@example.com")
    a = add(client, alice, "same-key", description="Alice's").get_json()
    b = add(client, bob, "same-key", description="Bob's")
    assert b.status_code == 201 and b.get_json()["id"] != a["id"]           # bob did NOT get alice's response
    assert b.get_json()["description"] == "Bob's"
    assert len(client.get("/expenses", headers=alice).get_json()) == 1


# ---- retry safety ----
def test_same_key_same_body_returns_first_result_and_creates_one_row(client):
    auth = register(client, "a@example.com")
    first = add(client, auth, "order-1")
    second = add(client, auth, "order-1")
    assert first.status_code == second.status_code == 201
    assert first.get_json() == second.get_json()
    assert second.headers["Idempotent-Replayed"] == "true" and "Idempotent-Replayed" not in first.headers
    assert len(client.get("/expenses", headers=auth).get_json()) == 1

def test_body_key_order_does_not_matter(client):
    auth = register(client, "a@example.com")
    r1 = client.post("/expenses", data='{"description":"x","amount_cents":5}',
                     headers={**auth, "Idempotency-Key": "k", "Content-Type": "application/json"})
    r2 = client.post("/expenses", data='{"amount_cents":5, "description":"x"}',
                     headers={**auth, "Idempotency-Key": "k", "Content-Type": "application/json"})
    assert r1.get_json()["id"] == r2.get_json()["id"]

def test_key_reused_with_different_body_is_rejected(client):
    auth = register(client, "a@example.com")
    add(client, auth, "k", amount_cents=100)
    r = add(client, auth, "k", amount_cents=999)
    assert r.status_code == 422 and r.get_json()["error"]["code"] == "idempotency_key_reused"
    assert len(client.get("/expenses", headers=auth).get_json()) == 1

def test_different_keys_make_different_rows(client):
    auth = register(client, "a@example.com")
    add(client, auth, "k1"); add(client, auth, "k2")
    assert len(client.get("/expenses", headers=auth).get_json()) == 2

def test_missing_idempotency_key_explains_what_to_send(client):
    auth = register(client, "a@example.com")
    r = client.post("/expenses", json={"description": "x", "amount_cents": 1}, headers=auth)
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "missing_idempotency_key"
    assert "Idempotency-Key" in r.get_json()["error"]["message"]

def test_concurrent_identical_requests_create_exactly_one_row(app):
    auth = register(app.test_client(), "a@example.com")
    results = []
    def go():
        results.append(add(app.test_client(), auth, "race"))
    threads = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert {r.status_code for r in results} == {201}
    assert len({r.get_json()["id"] for r in results}) == 1
    assert len(app.test_client().get("/expenses", headers=auth).get_json()) == 1


# ---- errors tell the caller what to change ----
def test_validation_errors_name_each_bad_field(client):
    auth = register(client, "a@example.com")
    r = add(client, auth, "k", description="", amount_cents=-5, paid_on="yesterday", colour="red")
    assert r.status_code == 422
    err = r.get_json()["error"]
    assert err["code"] == "validation_error"
    by_field = {f["field"]: f for f in err["fields"]}
    assert set(by_field) == {"description", "amount_cents", "paid_on", "colour"}
    assert "YYYY-MM-DD" in by_field["paid_on"]["problem"]
    assert by_field["amount_cents"]["got"] == -5
    assert "cents" in by_field["amount_cents"]["problem"]

def test_float_and_bool_amounts_rejected(client):
    auth = register(client, "a@example.com")
    for bad in (12.5, True, "12"):
        assert add(client, auth, f"k{bad}", amount_cents=bad).status_code == 422

def test_invalid_json_and_empty_body(client):
    auth = register(client, "a@example.com")
    h = {**auth, "Idempotency-Key": "k", "Content-Type": "application/json"}
    assert client.post("/expenses", data="{oops", headers=h).get_json()["error"]["code"] == "invalid_json"
    assert client.post("/expenses", data="[1]", headers=h).get_json()["error"]["code"] == "invalid_json"
    assert client.post("/expenses", headers=h).get_json()["error"]["code"] == "empty_body"

def test_register_errors(client):
    r = client.post("/register", json={"email": "nope", "password": "short"})
    assert r.status_code == 422
    assert {f["field"] for f in r.get_json()["error"]["fields"]} == {"email", "password"}
    register(client, "dup@example.com")
    r = client.post("/register", json={"email": "DUP@example.com", "password": "another-pass"})
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "email_taken"

def test_login(client):
    register(client, "a@example.com", "right-password")
    assert client.post("/login", json={"email": "a@example.com", "password": "wrong-pass"}).status_code == 401
    assert client.post("/login", json={"email": "ghost@example.com", "password": "whatever1"}).status_code == 401
    r = client.post("/login", json={"email": "a@example.com", "password": "right-password"})
    assert r.status_code == 200
    assert client.get("/expenses", headers={"Authorization": f"Bearer {r.get_json()['token']}"}).status_code == 200

def test_unknown_route_and_wrong_method_are_json(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.get_json()["error"]["code"] == "route_not_found"
    r = client.put("/health")
    assert r.status_code == 405 and "GET" in r.get_json()["error"]["message"]

def test_bad_limit_param(client):
    auth = register(client, "a@example.com")
    r = client.get("/expenses?limit=banana", headers=auth)
    assert r.status_code == 422 and r.get_json()["error"]["fields"][0]["field"] == "limit"

def test_tokens_and_passwords_are_not_stored_in_plaintext(tmp_path):
    import sqlite3
    path = str(tmp_path / "p.db")
    c = create_app(path).test_client()
    auth = register(c, "a@example.com", "plain-text-pw")
    token = auth["Authorization"].split()[1]
    db = sqlite3.connect(path)
    dump = "\n".join(str(r) for t in ("users", "tokens") for r in db.execute(f"SELECT * FROM {t}"))
    assert token not in dump and "plain-text-pw" not in dump
