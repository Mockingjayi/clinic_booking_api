import hashlib
import json
import math
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException

DATABASE_URL = os.environ["DATABASE_URL"]
API_KEY = os.environ["API_KEY"]

# The real limit is 5 attempts per email per minute.
# The setting below only exists so you can raise it while testing on your own computer.
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "5"))
WINDOW_SECONDS = 60

INDEX_FILE = Path(__file__).parent / "index.html"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    check=ConnectionPool.check_connection,
    open=False,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    seq BIGSERIAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_per_slot
    ON bookings (slot_id) WHERE status = 'confirmed';
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key TEXT PRIMARY KEY,
    body_hash TEXT NOT NULL,
    status_code INTEGER,
    response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS booking_attempts (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS attempts_by_email
    ON booking_attempts (email, attempted_at);
"""


@asynccontextmanager
async def lifespan(app):
    pool.open()
    with pool.connection() as conn:
        conn.execute(SCHEMA)
    yield
    pool.close()


app = FastAPI(lifespan=lifespan)


def err(status, message):
    return JSONResponse(status_code=status, content={"error": message})


@app.middleware("http")
async def check_api_key(request: Request, call_next):
    # The web page itself is public. Every API call it makes still carries the key.
    if request.url.path == "/app" and request.method in ("GET", "HEAD"):
        return await call_next(request)

    sent = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(sent.encode(), API_KEY.encode()):
        return err(401, "invalid or missing API key")
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def http_exc(request: Request, exc: StarletteHTTPException):
    return err(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_exc(request: Request, exc: RequestValidationError):
    return err(400, "invalid request")


@app.api_route("/app", methods=["GET", "HEAD"], response_class=HTMLResponse)
def web_page():
    html = INDEX_FILE.read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__API_KEY__", json.dumps(API_KEY)))


def iso(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def iso_slot(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def booking_json(row):
    return {
        "id": row["id"],
        "slot_id": row["slot_id"],
        "name": row["name"],
        "email": row["email"],
        "status": row["status"],
        "created_at": iso(row["created_at"]),
    }


@app.post("/reset")
async def reset(request: Request):
    body = await request.json()
    slots = body.get("slots", [])
    with pool.connection() as conn:
        conn.execute("DELETE FROM booking_attempts")
        conn.execute("DELETE FROM idempotency_keys")
        conn.execute("DELETE FROM bookings")
        conn.execute("DELETE FROM slots")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO slots (id, provider, start_at, end_at) VALUES (%s, %s, %s, %s)",
                [(s["id"], s["provider"], s["start"], s["end"]) for s in slots],
            )
    return {"ok": True}


@app.get("/slots")
def list_slots(provider: str | None = None):
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.provider, s.start_at, s.end_at,
                   NOT EXISTS (
                       SELECT 1 FROM bookings b
                       WHERE b.slot_id = s.id AND b.status = 'confirmed'
                   ) AS available
            FROM slots s
            WHERE (%(p)s::text IS NULL OR s.provider = %(p)s)
            ORDER BY s.start_at, s.id
            """,
            {"p": provider},
        ).fetchall()
    return {
        "slots": [
            {
                "id": r["id"],
                "provider": r["provider"],
                "start": iso_slot(r["start_at"]),
                "end": iso_slot(r["end_at"]),
                "available": r["available"],
            }
            for r in rows
        ]
    }


def check_rate_limit(conn, email):
    """Uses one ticket for this email.
    Returns None if the attempt is allowed, or the number of seconds to wait."""
    # Only one request at a time can count tickets for the same email.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (email,))

    # Tickets older than 60 seconds are given back.
    conn.execute(
        """
        DELETE FROM booking_attempts
        WHERE email = %s AND attempted_at <= clock_timestamp() - interval '60 seconds'
        """,
        (email,),
    )

    row = conn.execute(
        """
        SELECT count(*) AS used, min(attempted_at) AS oldest, clock_timestamp() AS now
        FROM booking_attempts
        WHERE email = %s
        """,
        (email,),
    ).fetchone()

    if row["used"] >= RATE_LIMIT:
        wait = (
            row["oldest"] + timedelta(seconds=WINDOW_SECONDS) - row["now"]
        ).total_seconds()
        return max(1, math.ceil(wait))

    conn.execute("INSERT INTO booking_attempts (email) VALUES (%s)", (email,))
    return None


def make_booking(conn, slot_id, name, email):
    """Tries to create a booking. Returns (status_code, response_body)."""
    slot = conn.execute("SELECT * FROM slots WHERE id = %s", (slot_id,)).fetchone()
    if not slot:
        return 404, {"error": "slot not found"}
    if slot["start_at"] <= datetime.now(timezone.utc):
        return 400, {"error": "slot has already started"}

    try:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO bookings (id, slot_id, name, email, status)
                VALUES (%s, %s, %s, %s, 'confirmed')
                RETURNING *
                """,
                (str(uuid.uuid4()), slot_id, name, email),
            ).fetchone()
    except pg_errors.UniqueViolation:
        return 409, {"error": "slot already booked"}

    return 201, booking_json(row)


def process_booking(conn, body):
    """Checks the details, then tries to book. Returns (status_code, response_body)."""
    slot_id = body.get("slot_id")
    name = body.get("name")
    email = body.get("email")

    if not isinstance(slot_id, str) or not slot_id.strip():
        return 400, {"error": "slot_id is required"}
    if not isinstance(name, str) or not name.strip():
        return 400, {"error": "name is required"}
    if not isinstance(email, str) or not email.strip():
        return 400, {"error": "email is required"}

    name = name.strip()
    email = email.strip().lower()

    if not EMAIL_RE.match(email):
        return 400, {"error": "email is not valid"}

    return make_booking(conn, slot_id, name, email)


@app.post("/bookings")
async def create_booking(request: Request):
    try:
        body = await request.json()
    except Exception:
        return err(400, "request body must be valid JSON")
    if not isinstance(body, dict):
        return err(400, "request body must be a JSON object")

    idem_key = request.headers.get("Idempotency-Key") or None
    body_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True).encode()
    ).hexdigest()

    # The email that the rate limit counts against. If there is no email, nothing to count.
    raw_email = body.get("email")
    rate_email = None
    if isinstance(raw_email, str) and raw_email.strip():
        rate_email = raw_email.strip().lower()

    with pool.connection() as conn:
        # Step 1: safe retries. A repeated request gets the saved answer.
        if idem_key:
            claimed = conn.execute(
                """
                INSERT INTO idempotency_keys (idem_key, body_hash)
                VALUES (%s, %s)
                ON CONFLICT (idem_key) DO NOTHING
                RETURNING idem_key
                """,
                (idem_key, body_hash),
            ).fetchone()
            if not claimed:
                saved = conn.execute(
                    "SELECT * FROM idempotency_keys WHERE idem_key = %s",
                    (idem_key,),
                ).fetchone()
                if saved["body_hash"] != body_hash:
                    return err(
                        422,
                        "Idempotency-Key was already used with a different request body",
                    )
                return JSONResponse(
                    status_code=saved["status_code"], content=saved["response"]
                )

        # Step 2: the spam limit. Every new attempt uses one ticket.
        if rate_email:
            wait = check_rate_limit(conn, rate_email)
            if wait is not None:
                conn.rollback()
                return JSONResponse(
                    status_code=429,
                    content={"error": "too many attempts, try again later"},
                    headers={"Retry-After": str(wait)},
                )

        # Step 3: check the details and book.
        status, payload = process_booking(conn, body)

        # Step 4: save the answer for safe retries.
        if idem_key:
            conn.execute(
                """
                UPDATE idempotency_keys
                SET status_code = %s, response = %s
                WHERE idem_key = %s
                """,
                (status, Jsonb(payload), idem_key),
            )

    return JSONResponse(status_code=status, content=payload)


@app.get("/bookings")
def list_bookings(request: Request):
    params = request.query_params

    email = (params.get("email") or "").strip().lower()
    if not email:
        return err(400, "email is required")

    try:
        limit = int(params.get("limit", "10"))
    except ValueError:
        return err(400, "limit must be a whole number from 1 to 50")
    if limit < 1 or limit > 50:
        return err(400, "limit must be a whole number from 1 to 50")

    after = 0
    cursor = params.get("cursor")
    if cursor:
        try:
            after = int(cursor)
        except ValueError:
            return err(400, "cursor is not valid")

    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM bookings
            WHERE lower(email) = %s AND seq > %s
            ORDER BY seq
            LIMIT %s
            """,
            (email, after, limit + 1),
        ).fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = str(page[-1]["seq"]) if has_more else None

    return {
        "bookings": [booking_json(r) for r in page],
        "next_cursor": next_cursor,
    }


@app.get("/bookings/{booking_id}")
def get_booking(booking_id: str):
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM bookings WHERE id = %s", (booking_id,)
        ).fetchone()
    if not row:
        return err(404, "booking not found")
    return booking_json(row)


@app.delete("/bookings/{booking_id}")
def cancel_booking(booking_id: str):
    with pool.connection() as conn:
        row = conn.execute(
            """
            UPDATE bookings SET status = 'cancelled'
            WHERE id = %s AND status = 'confirmed'
            RETURNING *
            """,
            (booking_id,),
        ).fetchone()
        if row:
            return booking_json(row)
        exists = conn.execute(
            "SELECT 1 FROM bookings WHERE id = %s", (booking_id,)
        ).fetchone()
    if not exists:
        return err(404, "booking not found")
    return err(409, "booking is already cancelled")