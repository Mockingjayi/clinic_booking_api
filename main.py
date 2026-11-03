import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException

DATABASE_URL = os.environ["DATABASE_URL"]
API_KEY = os.environ["API_KEY"]

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


@app.post("/bookings", status_code=201)
async def create_booking(request: Request):
    try:
        body = await request.json()
    except Exception:
        return err(400, "request body must be valid JSON")
    if not isinstance(body, dict):
        return err(400, "request body must be a JSON object")

    slot_id = body.get("slot_id")
    name = body.get("name")
    email = body.get("email")

    if not isinstance(slot_id, str) or not slot_id.strip():
        return err(400, "slot_id is required")
    if not isinstance(name, str) or not name.strip():
        return err(400, "name is required")
    if not isinstance(email, str) or not email.strip():
        return err(400, "email is required")

    name = name.strip()
    email = email.strip().lower()

    if not EMAIL_RE.match(email):
        return err(400, "email is not valid")

    try:
        with pool.connection() as conn:
            slot = conn.execute(
                "SELECT * FROM slots WHERE id = %s", (slot_id,)
            ).fetchone()
            if not slot:
                return err(404, "slot not found")
            if slot["start_at"] <= datetime.now(timezone.utc):
                return err(400, "slot has already started")

            booking_id = str(uuid.uuid4())
            row = conn.execute(
                """
                INSERT INTO bookings (id, slot_id, name, email, status)
                VALUES (%s, %s, %s, %s, 'confirmed')
                RETURNING *
                """,
                (booking_id, slot_id, name, email),
            ).fetchone()
    except pg_errors.UniqueViolation:
        return err(409, "slot already booked")

    return booking_json(row)


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