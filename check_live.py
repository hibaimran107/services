import json, sys, urllib.request, urllib.error, uuid

BASE = sys.argv[1].rstrip("/")

def call(method, path, body=None, token=None, key=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", "Bearer " + token)
    if key: req.add_header("Idempotency-Key", key)
    try:
        r = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        r = e
    raw = r.read().decode()
    return r.getcode(), r.headers, (json.loads(raw) if raw else None)

def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)

code, _, _ = call("GET", "/expenses")
check("no credentials -> 401", code == 401)

tag = uuid.uuid4().hex[:8]
_, _, a = call("POST", "/register", {"email": f"a{tag}@example.com", "password": "password-one"})
_, _, b = call("POST", "/register", {"email": f"b{tag}@example.com", "password": "password-two"})

body = {"description": "Lunch", "amount_cents": 1250}
key = "key-" + tag
c1, h1, r1 = call("POST", "/expenses", body, a["token"], key)
c2, h2, r2 = call("POST", "/expenses", body, a["token"], key)
check("create -> 201", c1 == 201)
check("same key again -> same id, no new row", r1["id"] == r2["id"])
check("replay header present", h2.get("Idempotent-Replayed") == "true")
_, _, lst = call("GET", "/expenses", token=a["token"])
check("exactly one row stored", len(lst) == 1)

code, _, err = call("POST", "/expenses", {"description": "", "amount_cents": "x"}, a["token"], "bad-" + tag)
check("bad input -> 422 naming fields", code == 422 and len(err["error"]["fields"]) == 2)

code, _, _ = call("GET", f"/expenses/{r1['id']}", token=b["token"])
check("other user's expense -> 404", code == 404)
_, _, lst_b = call("GET", "/expenses", token=b["token"])
check("other user's list is empty", lst_b == [])