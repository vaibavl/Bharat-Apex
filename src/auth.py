"""Demo & Dataset login: supports the 3 default demo logins AND all 100 customer credentials from customers.json."""

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from fastapi import Cookie, HTTPException

from . import db
from .synthetic_data.data_loader import get_all_customers

COOKIE_NAME = "apex_session"

# username -> (customer_id, env var holding the password override, default password)
_DEMO_USERS = {
    "aarav": ("CUST-IN-101", "DEMO_AARAV_PASSWORD", "Apex@101"),
    "rajesh": ("CUST-IN-102", "DEMO_RAJESH_PASSWORD", "Apex@102"),
    "priya": ("CUST-IN-103", "DEMO_PRIYA_PASSWORD", "Apex@103"),
}

# Simple brute-force guard: username -> list of recent failed attempt timestamps
_failures: Dict[str, list] = {}
_MAX_FAILS, _WINDOW_SECONDS = 5, 300


def session_ttl_hours() -> int:
    try:
        return int(os.getenv("SESSION_TTL_HOURS", "12"))
    except ValueError:
        return 12


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _is_locked(username: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(username, []) if now - t < _WINDOW_SECONDS]
    _failures[username] = recent
    return len(recent) >= _MAX_FAILS


def authenticate(username: str, password: str) -> Optional[str]:
    """Returns the customer_id on success, None on failure. Constant-time compare."""
    raw_user = (username or "").strip()
    user_lower = raw_user.lower()
    if _is_locked(user_lower):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in a few minutes.")

    matched_cid: Optional[str] = None

    # 1. Check classic demo aliases (aarav / Apex@101, rajesh / Apex@102, priya / Apex@103)
    entry = _DEMO_USERS.get(user_lower)
    if entry:
        expected = os.getenv(entry[1], entry[2])
        if hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
            matched_cid = entry[0]

    # 2. Check all 100 customer profiles in customers.json (login_credentials.username, first_name, or customer_id)
    if not matched_cid:
        for cust in get_all_customers():
            creds = cust.get("login_credentials") or {}
            c_user = str(creds.get("username", "")).lower()
            c_pass = str(creds.get("password", ""))
            first_name = cust.get("name", "").split()[0].lower()
            cid_lower = cust.get("id", "").lower()

            if user_lower in (c_user, first_name, cid_lower) and c_pass:
                if hmac.compare_digest(password.encode("utf-8"), c_pass.encode("utf-8")):
                    matched_cid = cust["id"]
                    break

    if not matched_cid:
        _failures.setdefault(user_lower, []).append(time.time())
        return None

    _failures.pop(user_lower, None)
    return matched_cid


def create_session(customer_id: str, username: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=session_ttl_hours())
    db.save_session(_hash_token(token), customer_id, username.lower(), expires.isoformat())
    return token


def end_session(token: Optional[str]) -> None:
    if token:
        db.delete_session(_hash_token(token))


def current_session(apex_session: Optional[str] = Cookie(default=None)) -> Dict[str, str]:
    """FastAPI dependency: the logged-in customer, or 401."""
    if not apex_session:
        raise HTTPException(status_code=401, detail="Not signed in")
    sess = db.load_session(_hash_token(apex_session))
    if not sess:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    return sess
