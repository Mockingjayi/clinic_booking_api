import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
KEY = os.environ["API_KEY"]


def call(method, path, body=None, key=KEY):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key is not None:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def check(label, got, expected):
    result = "PASS" if got == expected else "FAIL"
    print(f"{result}  {label}  (got {got}, expected {expected})")


def is_available(slot_id):
    status, data = call("GET", "/slots")
    for slot in data["slots"]:
        if slot["id"] == slot_id:
            return slot["available"]


slots = {
    "slots": [
        {
            "id": "s1",
            "provider": "Dr Ade",
            "start": "2030-12-10T09:00:00Z",
            "end": "2030-12-10T09:30:00Z",
        },
        {
            "id": "s2",
            "provider": "Dr Ade",
            "start": "2030-12-10T10:00:00Z",
            "end": "2030-12-10T10:30:00Z",
        },
    ]
}

first = {"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"}
second = {"slot_id": "s1", "name": "Tunde Bello", "email": "tunde@example.com"}

status, _ = call("POST", "/reset", slots)
check("reset works", status, 200)

# Part A: two people, same slot
status, booking = call("POST", "/bookings", first)
check("first booking works", status, 201)

status, _ = call("POST", "/bookings", second)
check("second booking of the same slot is refused", status, 409)

check("slot shows as taken", is_available("s1"), False)

# Part B: cancelling
status, cancelled = call("DELETE", "/bookings/" + booking["id"])
check("cancel works", status, 200)
check("status is cancelled", cancelled.get("status"), "cancelled")

check("slot is free again", is_available("s1"), True)

status, _ = call("DELETE", "/bookings/" + booking["id"])
check("cancelling twice is refused", status, 409)

status, _ = call("DELETE", "/bookings/doesnotexist")
check("cancelling an unknown booking", status, 404)

status, again = call("GET", "/bookings/" + booking["id"])
check("cancelled booking can still be looked up", again.get("status"), "cancelled")

status, _ = call("POST", "/bookings", second)
check("slot can be booked again after a cancel", status, 201)


# Part C: 20 people click at the same moment
def attempt(i):
    body = {
        "slot_id": "s2",
        "name": f"Person {i}",
        "email": f"person{i}@example.com",
    }
    status, _ = call("POST", "/bookings", body)
    return status


with ThreadPoolExecutor(max_workers=20) as workers:
    results = list(workers.map(attempt, range(20)))

check("exactly one of 20 simultaneous bookings wins", results.count(201), 1)
check("the other 19 are refused", results.count(409), 19)