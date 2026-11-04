import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
KEY = os.environ["API_KEY"]

fmt = "%Y-%m-%dT%H:%M:%SZ"
first_day = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
    hour=8, minute=0, second=0, microsecond=0
)

slots = []
number = 1
for day in range(3):
    for provider in ["Dr Ade", "Dr Bisi"]:
        for step in range(4):
            start = first_day + timedelta(days=day, minutes=30 * step)
            end = start + timedelta(minutes=30)
            slots.append(
                {
                    "id": f"s{number}",
                    "provider": provider,
                    "start": start.strftime(fmt),
                    "end": end.strftime(fmt),
                }
            )
            number += 1

req = urllib.request.Request(
    BASE + "/reset",
    data=json.dumps({"slots": slots}).encode(),
    method="POST",
)
req.add_header("Content-Type", "application/json")
req.add_header("X-API-Key", KEY)

try:
    with urllib.request.urlopen(req, timeout=120) as response:
        print("Done:", response.status, "with", len(slots), "slots")
except urllib.error.HTTPError as e:
    print("Failed:", e.code, e.read().decode())