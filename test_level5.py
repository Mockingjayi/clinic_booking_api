import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE = "http://localhost:8000"
KEY = os.environ["API_KEY"]


def call(method, path, body=None, key=KEY, extra=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key is not None:
        req.add_header("X-API-Key", key)
    for header_name, header_value in (extra or {}).items():
        req.add_header(header_name, header_value)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), e.headers


def check(label, got, expected):
    result = "PASS" if got == expected else "FAIL"
    print(f"{result}  {label}  (got {got}, expected {expected})")


def make_slot(slot_id, minutes_from_start):
    start = datetime(2030, 12, 11, 8, 0, tzinfo=timezone.utc) + timedelta(
        minutes=minutes_from_start
    )
    end = start + timedelta(minutes=30)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "id": slot_id,
        "provider": "Dr Ade",
        "start": start.strftime(fmt),
        "end": end.strftime(fmt),
    }


def is_available(slot_id):
    status, data, _ = call("GET", "/slots")
    for slot in data["slots"]:
        if slot["id"] == slot_id:
            return slot["available"]


slot_list = [make_slot("r1", 0), make_slot("r2", 30), make_slot("r3", 60)]
slot_list += [make_slot("c1", 90), make_slot("c2", 120)]
slot_list += [make_slot(f"b{i}", 150 + 30 * (i - 1)) for i in range(1, 13)]

status, _, _ = call("POST", "/reset", {"slots": slot_list})
check("reset works", status, 200)

# ---------- Part A: the 6th attempt is refused ----------
rae = "ratetest@example.com"
attempts = [
    {"slot_id": "r1", "email": rae},  # missing name, fails with 400
    {"slot_id": "nope", "name": "Rae Test", "email": rae},  # unknown slot, 404
    {"slot_id": "r1", "name": "Rae Test", "email": rae},  # works, 201
    {"slot_id": "r1", "name": "Rae Test", "email": rae},  # slot taken, 409
    {"slot_id": "r2", "name": "Rae Test", "email": rae},  # works, 201
]
results = [call("POST", "/bookings", body)[0] for body in attempts]
check("5 attempts, failures included, are answered normally", results, [400, 404, 201, 409, 201])

sixth = {"slot_id": "r3", "name": "Rae Test", "email": rae}
status, body, headers = call("POST", "/bookings", sixth)
check("the 6th attempt is refused", status, 429)
check("the refusal uses the error format", "error" in body, True)

retry_after = headers.get("Retry-After")
wait = int(retry_after) if retry_after and retry_after.isdigit() else 0
check("Retry-After is a whole number of seconds", retry_after is not None and retry_after.isdigit(), True)
check("Retry-After is between 1 and 60", 1 <= wait <= 60, True)

check("the refused attempt booked nothing", is_available("r3"), True)

messy = {"slot_id": "r3", "name": "Rae Test", "email": "  RATETEST@Example.com "}
status, _, _ = call("POST", "/bookings", messy)
check("the same email in capitals and spaces is also limited", status, 429)

sam = {"slot_id": "r3", "name": "Sam Other", "email": "other@example.com"}
status, _, _ = call("POST", "/bookings", sam)
check("a different email is not affected", status, 201)

# ---------- Part B: retries do not use tickets ----------
replay_body = {"slot_id": "c1", "name": "Rey Play", "email": "replay@example.com"}
replay_header = {"Idempotency-Key": "ratereplay1"}
replay_statuses = []
replay_ids = set()
for i in range(8):
    status, body, _ = call("POST", "/bookings", replay_body, extra=replay_header)
    replay_statuses.append(status)
    replay_ids.add(body.get("id"))
check("8 replays of one request all get the saved answer", replay_statuses.count(201), 8)
check("and they share one booking id", len(replay_ids), 1)

fresh = {"slot_id": "c2", "name": "Rey Play", "email": "replay@example.com"}
status, _, _ = call("POST", "/bookings", fresh)
check("replays did not use up the tickets", status, 201)

# ---------- Part C: 12 attempts at the same moment ----------
def burst_attempt(i):
    body = {"slot_id": f"b{i}", "name": "Burst Test", "email": "burst@example.com"}
    return call("POST", "/bookings", body)[0]


with ThreadPoolExecutor(max_workers=12) as workers:
    burst = list(workers.map(burst_attempt, range(1, 13)))

check("burst: exactly 5 of 12 simultaneous attempts get through", burst.count(201), 5)
check("burst: the other 7 are refused with 429", burst.count(429), 7)