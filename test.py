import json
import os
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
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


slots = {
    "slots": [
        {
            "id": "s1",
            "provider": "Dr Ade",
            "start": "2030-12-10T09:00:00Z",
            "end": "2030-12-10T09:30:00Z",
        },
        {
            "id": "past1",
            "provider": "Dr Ade",
            "start": "2020-01-01T09:00:00Z",
            "end": "2020-01-01T09:30:00Z",
        },
    ]
}

good = {"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"}

status, _ = call("POST", "/reset", slots)
check("reset works with the key", status, 200)

status, _ = call("GET", "/slots", key=None)
check("no key is rejected", status, 401)

status, _ = call("GET", "/slots", key="wrongkey")
check("wrong key is rejected", status, 401)

status, _ = call("POST", "/bookings", {"slot_id": "s1", "email": "ada@example.com"})
check("missing name", status, 400)

status, _ = call("POST", "/bookings", {**good, "name": "   "})
check("blank name", status, 400)

status, _ = call("POST", "/bookings", {"slot_id": "s1", "name": "Ada Obi"})
check("missing email", status, 400)

status, _ = call("POST", "/bookings", {**good, "email": "not an email"})
check("invalid email", status, 400)

status, _ = call("POST", "/bookings", {"name": "Ada Obi", "email": "ada@example.com"})
check("missing slot_id", status, 400)

status, _ = call("POST", "/bookings", {**good, "slot_id": "nope"})
check("unknown slot", status, 404)

status, _ = call("POST", "/bookings", {**good, "slot_id": "past1"})
check("slot in the past", status, 400)

messy = {"slot_id": "s1", "name": "  Ada Obi  ", "email": "  ADA@Example.com "}
status, booking = call("POST", "/bookings", messy)
check("good booking works", status, 201)
check("name was trimmed", booking.get("name"), "Ada Obi")
check("email was trimmed and lower cased", booking.get("email"), "ada@example.com")

status, _ = call("GET", "/bookings/" + booking["id"])
check("look up the booking", status, 200)