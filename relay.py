"""Booking relay: validates a service request from the Agentplace agent and
hands it to an external test service exactly once.

POST /requests   submit one request  (also GET /services, GET /requests/{ref}, GET /health)
If RELAY_API_KEY is set, POST /requests needs "Authorization: Bearer <key>".
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

SERVICES = {
    "cleaning": "Home deep cleaning",
    "plumbing": "Plumbing repair",
    "electrical": "Electrical repair",
    "ac_service": "AC servicing",
    "pest_control": "Pest control",
}
# Postman Echo answers like a real API and keeps nothing, so it is safe for test data.
EXTERNAL_URL = os.getenv("EXTERNAL_URL", "https://postman-echo.com/post")
DB_PATH = os.getenv("DB_PATH", "relay.db")
API_KEY = os.getenv("RELAY_API_KEY", "")
ATTEMPTS = 3
RETRY_DELAY = 0.5  # seconds, doubled after each failed attempt
HTTP_TRANSPORT = None  # tests swap in an httpx.MockTransport

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9 ()-]{7,20}$")

# shortcut: SQLite on the host's local disk, wiped on redeploy; use Postgres or Redis for real traffic.
db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.execute(
    """CREATE TABLE IF NOT EXISTS requests (
        key TEXT PRIMARY KEY, ref TEXT, status TEXT, service TEXT,
        preferred_time TEXT, created_at TEXT)"""
)


def validate(data: dict) -> tuple[dict, list[dict]]:
    """Return (clean request, errors). Errors name the field so the agent can ask again."""
    errors, clean = [], {}

    def err(field, message):
        errors.append({"field": field, "message": message})

    service = str(data.get("service") or "").strip().lower()
    if service not in SERVICES:
        err("service", f"Choose one of: {', '.join(SERVICES)}.")
    clean["service"] = service

    name = str(data.get("name") or "").strip()
    if not 2 <= len(name) <= 100:
        err("name", "Give the customer's name (2 to 100 characters).")
    clean["name"] = name

    email = str(data.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        err("email", "Give a valid email address.")
    clean["email"] = email

    phone = str(data.get("phone") or "").strip()
    if phone and not PHONE_RE.match(phone):
        err("phone", "Phone may only contain digits, spaces, +, - and brackets.")
    clean["phone"] = phone

    raw_time = str(data.get("preferred_time") or "").strip()
    try:
        when = datetime.fromisoformat(raw_time)
        if when.tzinfo is None:
            err("preferred_time", "Include a timezone offset, e.g. 2026-12-01T15:00:00+05:30.")
        elif when <= datetime.now(timezone.utc):
            err("preferred_time", "The preferred time must be in the future.")
        else:
            clean["preferred_time"] = when.astimezone(timezone.utc).isoformat()
    except ValueError:
        err("preferred_time", "Use ISO 8601 date and time, e.g. 2026-12-01T15:00:00+05:30.")

    notes = str(data.get("notes") or "").strip()
    if len(notes) > 500:
        err("notes", "Keep notes under 500 characters.")
    clean["notes"] = notes
    return clean, errors


def request_key(clean: dict) -> str:
    # Same customer, same service, same slot = same request, even if the name is retyped.
    basis = f"{clean['service']}|{clean['email']}|{clean['preferred_time']}"
    return hashlib.sha256(basis.encode()).hexdigest()


async def forward(clean: dict, key: str) -> None:
    """POST to the external service, retrying network errors and 5xx. Raises on failure."""
    delay = RETRY_DELAY
    async with httpx.AsyncClient(timeout=8, transport=HTTP_TRANSPORT) as client:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                resp = await client.post(EXTERNAL_URL, json=clean, headers={"Idempotency-Key": key})
                if resp.status_code < 500:
                    resp.raise_for_status()  # 4xx: our payload is wrong, retrying won't help
                    return
            except httpx.TransportError:
                pass
            if attempt < ATTEMPTS:
                await asyncio.sleep(delay)
                delay *= 2
    raise httpx.HTTPError("external service unavailable")


async def submit(data: dict, idempotency_key: str | None = None) -> tuple[int, dict]:
    clean, errors = validate(data)
    if errors:
        return 422, {"status": "invalid", "errors": errors}

    key = idempotency_key or request_key(clean)
    ref = "REQ-" + hashlib.sha256(key.encode()).hexdigest()[:8].upper()
    # Claim the key before calling out, so two identical calls in flight can't both send.
    claimed = db.execute(
        "INSERT OR IGNORE INTO requests VALUES (?, ?, 'pending', ?, ?, ?)",
        (key, ref, clean["service"], clean["preferred_time"], datetime.now(timezone.utc).isoformat()),
    ).rowcount
    if not claimed:
        status = db.execute("SELECT status FROM requests WHERE key = ?", (key,)).fetchone()[0]
        if status == "pending":
            return 409, {"status": "in_progress", "reference": ref,
                         "message": "This request is being sent right now. Don't resend it."}
        return 200, {"status": "duplicate", "reference": ref,
                     "message": "This request was already submitted. No new request was created."}

    try:
        await forward(clean, key)
    except httpx.HTTPError as e:
        db.execute("DELETE FROM requests WHERE key = ?", (key,))  # release the claim so a retry can send
        if isinstance(e, httpx.HTTPStatusError):
            return 502, {"status": "rejected", "retryable": False,
                         "message": "The booking system refused this request. Nothing was created."}
        return 503, {"status": "unavailable", "retryable": True,
                     "message": "The booking system is unavailable. Nothing was created; try again in a few minutes."}

    db.execute("UPDATE requests SET status = 'created' WHERE key = ?", (key,))
    return 201, {"status": "created", "reference": ref, "service": SERVICES[clean["service"]],
                 "preferred_time": clean["preferred_time"]}


app = FastAPI(title="Booking relay")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/services")
def services():
    return SERVICES


@app.post("/requests")
async def create_request(request: Request, idempotency_key: str | None = Header(None),
                         authorization: str = Header("")):
    if API_KEY and not secrets.compare_digest(authorization, f"Bearer {API_KEY}"):
        return JSONResponse({"status": "unauthorized"}, 401)
    try:
        data = await request.json()
        assert isinstance(data, dict)
    except (json.JSONDecodeError, AssertionError):
        return JSONResponse({"status": "invalid", "errors": [{"field": "body", "message": "Send a JSON object."}]}, 422)
    code, body = await submit(data, idempotency_key)
    return JSONResponse(body, code)


@app.get("/requests/{ref}")
def get_request(ref: str):
    # Contact details are deliberately not returned: this endpoint is public.
    row = db.execute("SELECT ref, status, service, preferred_time, created_at FROM requests WHERE ref = ?",
                     (ref.upper(),)).fetchone()
    if not row:
        return JSONResponse({"status": "not_found"}, 404)
    return dict(zip(["reference", "status", "service", "preferred_time", "created_at"], row))

