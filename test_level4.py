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
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


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
    status, data = call("GET", "/slots")
    for slot in data["slots"]:
        if slot["id"] == slot_id:
            return slot["available"]


# ---------- Part A: safe retries ----------
slots_a = {"slots": [make_slot("a1", 0), make_slot("a2", 30), make_slot("a3", 60)]}
status, _ = call("POST", "/reset", slots_a)
check("reset works", status, 200)

ada = {"slot_id": "a1", "name": "Ada Obi", "email": "ada@example.com"}
retry1 = {"Idempotency-Key": "retrykey1"}

status1, first = call("POST", "/bookings", ada, extra=retry1)
check("first request with a key works", status1, 201)

status2, second = call("POST", "/bookings", ada, extra=retry1)
check("retry returns the same status", status2, 201)
check("retry returns the same booking id", second.get("id"), first.get("id"))

status, listing = call("GET", "/bookings?email=ada@example.com")
check("only one booking was created", len(listing["bookings"]), 1)

other = {**ada, "slot_id": "a2"}
status, _ = call("POST", "/bookings", other, extra=retry1)
check("same key with a different body is refused", status, 422)
check("the other slot was not booked", is_available("a2"), True)

tunde = {"slot_id": "a3", "name": "Tunde Bello", "email": "tunde@example.com"}


def send_retry(i):
    return call("POST", "/bookings", tunde, extra={"Idempotency-Key": "retrykey2"})


with ThreadPoolExecutor(max_workers=10) as workers:
    outcomes = list(workers.map(send_retry, range(10)))

statuses = [outcome[0] for outcome in outcomes]
ids = {outcome[1].get("id") for outcome in outcomes}
check("all 10 simultaneous retries get 201", statuses.count(201), 10)
check("they all share one booking id", len(ids), 1)

status, listing = call("GET", "/bookings?email=tunde@example.com")
check("only one booking exists after the retries", len(listing["bookings"]), 1)

# ---------- Part B: paging ----------
slots_b = {"slots": [make_slot(f"p{i}", 30 * (i - 1)) for i in range(1, 26)]}
status, _ = call("POST", "/reset", slots_b)
check("reset works again", status, 200)

created = 0
for i in range(1, 26):
    body = {"slot_id": f"p{i}", "name": "Pat Pager", "email": "Pager@Example.com"}
    status, _ = call("POST", "/bookings", body)
    if status == 201:
        created += 1
check("25 bookings were created", created, 25)

status, _ = call("GET", "/bookings?email=pager@example.com&limit=0")
check("limit 0 is refused", status, 400)

status, _ = call("GET", "/bookings?email=pager@example.com&limit=51")
check("limit 51 is refused", status, 400)

status, _ = call("GET", "/bookings?email=pager@example.com&limit=abc")
check("limit abc is refused", status, 400)

status, _ = call("GET", "/bookings")
check("missing email is refused", status, 400)

status, page = call("GET", "/bookings?email=pager@example.com")
check("default page holds 10", len(page["bookings"]), 10)
check("first page has a next cursor", page["next_cursor"] is not None, True)

status, page = call("GET", "/bookings?email=PAGER@EXAMPLE.COM&limit=5")
check("email match ignores case", len(page["bookings"]), 5)

collected = []
cursor = None
pages = 0
while True:
    path = "/bookings?email=pager@example.com&limit=10"
    if cursor:
        path += "&cursor=" + cursor
    status, page = call("GET", path)
    collected.extend(page["bookings"])
    pages += 1
    cursor = page["next_cursor"]
    if cursor is None or pages > 10:
        break

check("walking through takes 3 pages", pages, 3)
check("all 25 bookings were returned", len(collected), 25)
check("no booking appears twice", len({b["id"] for b in collected}), 25)
check(
    "oldest first",
    [b["slot_id"] for b in collected],
    [f"p{i}" for i in range(1, 26)],
)

status, page = call("GET", "/bookings?email=nobody@example.com")
check("unknown email gives an empty list", page["bookings"], [])
check("and no next cursor", page["next_cursor"], None)