"""CIVICLENS API: SQLite-backed civic reporting and administration."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, field_validator
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool


ROOT = Path(__file__).resolve().parent.parent


def load_local_env() -> None:
    """Read a simple .env file without requiring a dotenv package."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_local_env()


def configured_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    path = Path(value) if value else default
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


DATA_DIR = configured_path("CIVICLENS_DATA_DIR", ROOT / "backend" / "data")
UPLOAD_DIR = configured_path("CIVICLENS_UPLOAD_DIR", DATA_DIR / "uploads")
DB_PATH = configured_path("CIVICLENS_DB_PATH", DATA_DIR / "civiclens.sqlite3")
WEB_DIR = ROOT
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DEMO_MODE = os.getenv("CIVICLENS_DEMO_MODE", "true").lower() in {"1", "true", "yes", "on"}
TOKEN_SECRET = os.getenv("CIVICLENS_SECRET_KEY", "development-only-change-me")
TOKEN_LIFETIME_HOURS = int(os.getenv("CIVICLENS_TOKEN_HOURS", "24"))
AI_API_KEY = os.getenv("CIVICLENS_AI_API_KEY", "")
AI_API_URL = os.getenv("CIVICLENS_AI_API_URL", "https://api.openai.com/v1/chat/completions")
AI_MODEL = os.getenv("CIVICLENS_AI_MODEL", "gpt-5.4-mini")
SMTP_HOST = os.getenv("CIVICLENS_SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("CIVICLENS_SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("CIVICLENS_SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("CIVICLENS_SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("CIVICLENS_EMAIL_FROM", "").strip()
SMTP_USE_SSL = os.getenv("CIVICLENS_SMTP_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}
SMTP_USE_STARTTLS = os.getenv("CIVICLENS_SMTP_STARTTLS", "true").lower() in {"1", "true", "yes", "on"}
FIRST_RESPONSE_TARGET_HOURS = 48
MAX_IMAGE_BYTES = 6 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 25_000_000

if not DEMO_MODE and (not AI_API_KEY and os.getenv("CIVICLENS_REQUIRE_AI", "false").lower() == "true"):
    raise RuntimeError("CIVICLENS_REQUIRE_AI is true, but CIVICLENS_AI_API_KEY is missing.")
if not DEMO_MODE and TOKEN_SECRET == "development-only-change-me":
    raise RuntimeError("Set a unique CIVICLENS_SECRET_KEY before running with demo mode disabled.")
if not DEMO_MODE and len(TOKEN_SECRET) < 32:
    raise RuntimeError("CIVICLENS_SECRET_KEY must contain at least 32 characters.")
if not DEMO_MODE and not (os.getenv("CIVICLENS_ADMIN_EMAIL") and os.getenv("CIVICLENS_ADMIN_PASSWORD")):
    raise RuntimeError("Set CIVICLENS_ADMIN_EMAIL and CIVICLENS_ADMIN_PASSWORD when demo mode is disabled.")

app = FastAPI(title="CIVICLENS API", version="1.0.0", description="Civic reports, impact scoring, and municipal operations.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("CIVICLENS_ALLOWED_ORIGIN", "http://127.0.0.1:8000"), "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
bearer = HTTPBearer(auto_error=False)
rate_buckets: dict[str, list[float]] = defaultdict(list)
rate_lock = threading.Lock()
last_rate_sweep = 0.0


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(self)"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob: https://unpkg.com https://*.tile.openstreetmap.org; "
        "connect-src 'self' https://nominatim.openstreetmap.org; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    return response

CATEGORIES = {
    "pothole": "Pothole / damaged road",
    "garbage": "Overflowing garbage",
    "blocked_drain": "Blocked drain / waterlogging",
    "streetlight": "Broken streetlight",
    "footpath": "Damaged footpath",
    "illegal_dumping": "Illegal dumping",
    "fallen_tree": "Fallen tree / obstruction",
    "other": "Other civic issue",
    "official_conduct": "Suspected corruption or misconduct by a public official (private)",
}
AI_CATEGORIES = {key: label for key, label in CATEGORIES.items() if key != "official_conduct"}
SEVERITIES = {"Low", "Medium", "High", "Critical"}
STATUSES = {
    "Submitted", "Pending Verification", "Verified", "Assigned",
    "In Progress", "Resolved", "Rejected", "Reopened",
}
SEVERITY_POINTS = {"Low": 6, "Medium": 14, "High": 22, "Critical": 30}
SAFETY_POINTS = {
    "pothole": 20, "garbage": 12, "blocked_drain": 22, "streetlight": 13,
    "footpath": 13, "illegal_dumping": 14, "fallen_tree": 27, "other": 12,
}
DEPARTMENTS = {
    "pothole": "Roads & Public Works", "garbage": "Sanitation",
    "blocked_drain": "Stormwater & Drainage", "streetlight": "Electrical Services",
    "footpath": "Roads & Public Works", "illegal_dumping": "Sanitation",
    "fallen_tree": "Parks & Horticulture", "other": "Municipal Operations",
    "official_conduct": "CIVICLENS Confidential Review",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=15, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def password_hash(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return f"{base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(derived).decode()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        salt_text, digest_text = encoded.split("$", 1)
        salt = base64.urlsafe_b64decode(salt_text.encode())
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


def init_db() -> None:
    with connect() as db:
        db.execute("PRAGMA journal_mode = WAL")
        schema_file = Path(__file__).resolve().parent / "schema.sql"
        db.executescript(schema_file.read_text(encoding="utf-8"))
        columns = {row["name"] for row in db.execute("PRAGMA table_info(reports)").fetchall()}
        if "ai_category" not in columns:
            db.execute("ALTER TABLE reports ADD COLUMN ai_category TEXT NOT NULL DEFAULT ''")
        if "ai_severity" not in columns:
            db.execute("ALTER TABLE reports ADD COLUMN ai_severity TEXT NOT NULL DEFAULT ''")
        if "resolved_at" not in columns:
            db.execute("ALTER TABLE reports ADD COLUMN resolved_at TEXT")
        if "photo_hash" not in columns:
            db.execute("ALTER TABLE reports ADD COLUMN photo_hash TEXT NOT NULL DEFAULT ''")
        report_migrations = {
            "is_sensitive": "INTEGER NOT NULL DEFAULT 0",
            "reported_official_role": "TEXT NOT NULL DEFAULT ''",
            "reported_official_name": "TEXT NOT NULL DEFAULT ''",
            "incident_at": "TEXT NOT NULL DEFAULT ''",
            "first_response_due_at": "TEXT NOT NULL DEFAULT ''",
            "first_response_at": "TEXT",
            "resolution_confirmed_at": "TEXT",
            "submission_key": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in report_migrations.items():
            if column not in columns:
                db.execute(f"ALTER TABLE reports ADD COLUMN {column} {definition}")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS email_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
          user_id TEXT REFERENCES users(id),
          event_type TEXT NOT NULL,
          recipient TEXT NOT NULL,
          delivery_status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resolution_evidence (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
          uploader_id TEXT NOT NULL REFERENCES users(id),
          photo_name TEXT NOT NULL,
          caption TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS report_submission_key_idx
          ON reports(reporter_id, submission_key) WHERE submission_key != '';
        CREATE INDEX IF NOT EXISTS report_sensitive_created_idx ON reports(is_sensitive, created_at);
        CREATE INDEX IF NOT EXISTS evidence_report_idx ON resolution_evidence(report_id, created_at);
        """)
        count = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        if DEMO_MODE:
            ensure_demo_user(db, "demo-citizen", "Demo Citizen", "citizen@civiclens.demo", os.getenv("CIVICLENS_DEMO_CITIZEN_PASSWORD", "CitizenDemo#2026"), "citizen")
            ensure_demo_user(db, "demo-admin", "Demo Administrator", "admin@civiclens.demo", os.getenv("CIVICLENS_DEMO_ADMIN_PASSWORD", "AdminDemo#2026"), "admin")
        elif os.getenv("CIVICLENS_ADMIN_EMAIL") and os.getenv("CIVICLENS_ADMIN_PASSWORD"):
            admin_email = os.getenv("CIVICLENS_ADMIN_EMAIL", "").strip().lower()
            admin_password = os.getenv("CIVICLENS_ADMIN_PASSWORD", "")
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", admin_email) or admin_email.endswith("@civiclens.demo"):
                raise RuntimeError("Set a valid, non-demo CIVICLENS_ADMIN_EMAIL.")
            if len(admin_password) < 12:
                raise RuntimeError("CIVICLENS_ADMIN_PASSWORD must be at least 12 characters.")
            existing_admin = db.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone()
            if existing_admin:
                db.execute("UPDATE users SET role='admin' WHERE id=?", (existing_admin["id"],))
            else:
                bootstrap = db.execute("SELECT id FROM users WHERE id='bootstrap-admin'").fetchone()
                if bootstrap:
                    db.execute("UPDATE users SET name=?,email=?,role='admin' WHERE id='bootstrap-admin'", (os.getenv("CIVICLENS_ADMIN_NAME", "CivicLens Administrator"), admin_email))
                else:
                    ensure_demo_user(db, "bootstrap-admin", os.getenv("CIVICLENS_ADMIN_NAME", "CivicLens Administrator"), admin_email, admin_password, "admin")
            db.execute("UPDATE users SET role='citizen' WHERE id IN ('demo-citizen','demo-admin')")
        else:
            db.execute("UPDATE users SET role='citizen' WHERE id IN ('demo-citizen','demo-admin')")
        if count == 0 and DEMO_MODE:
            seed_demo(db)


def ensure_demo_user(db: sqlite3.Connection, user_id: str, name: str, email: str, password: str, role: str) -> None:
    if not db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        db.execute("INSERT INTO users(id,name,email,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                   (user_id, name, email, password_hash(password), role, now_iso()))


def seed_demo(db: sqlite3.Connection) -> None:
    """Seed synthetic reports only on a new demo-mode database."""
    citizen_id = "demo-citizen"
    examples = [
        ("CL-DEMO-01", "pothole", "Large pothole near the bus stop", "A deep pothole has formed in the left lane and is difficult to see after rain.", "Hazratganj, near the central bus stop", "Ward 1 · Hazratganj", 26.8520, 80.9440, "High", "In Progress", "Roads & Public Works", 20),
        ("CL-DEMO-02", "garbage", "Waste spilling onto the footpath", "The community bin has been overflowing since yesterday morning.", "Indira Nagar Market, Sector 8", "Ward 4 · Indira Nagar", 26.8820, 80.9850, "Medium", "Verified", "Sanitation", 8),
        ("CL-DEMO-03", "blocked_drain", "Standing water at a blocked inlet", "Water is pooling across the service lane and the roadside drain looks blocked.", "Alambagh, near the bus terminal", "Ward 9 · Alambagh", 26.8150, 80.9120, "High", "Assigned", "Stormwater & Drainage", 18),
        ("CL-DEMO-04", "streetlight", "Streetlight out on a busy crossing", "Two lamps at the crossing are not working after dark.", "Gomti Nagar, Vishal Khand crossing", "Ward 7 · Gomti Nagar", 26.8625, 80.9780, "Medium", "Pending Verification", "Electrical Services", 10),
        ("CL-DEMO-05", "footpath", "Broken paving creates a trip hazard", "Several paving slabs are loose beside the school entrance.", "Chowk, near the school gate", "Ward 12 · Chowk", 26.8710, 80.9180, "Low", "Resolved", "Roads & Public Works", 72),
        ("CL-DEMO-06", "illegal_dumping", "Construction debris left on roadside", "A small truckload of debris has been dumped beside the road.", "Mahanagar, near the community park", "Ward 3 · Mahanagar", 26.8790, 80.9520, "Medium", "Submitted", "Sanitation", 4),
        ("CL-DEMO-07", "fallen_tree", "Fallen branch partially blocks the road", "A large branch is blocking part of the carriageway after heavy rain.", "Kaiserbagh, near the post office", "Ward 2 · Kaiserbagh", 26.8460, 80.9250, "Critical", "Verified", "Parks & Horticulture", 3),
    ]
    for row in examples:
        rid, category, title, description, location, ward, lat, lng, severity, status, department, age_hours = row
        created = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(timespec="seconds").replace("+00:00", "Z")
        db.execute("""INSERT INTO reports(id,reporter_id,category,title,description,location,ward,lat,lng,severity,status,department,created_at,updated_at,demo)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""", (rid, citizen_id, category, title, description, location, ward, lat, lng, severity, status, department, created, created))
        db.execute("INSERT INTO timeline(report_id,status,actor_id,detail,created_at) VALUES(?,?,?,?,?)", (rid, "Submitted", citizen_id, "Synthetic demonstration report", created))
        if status != "Submitted":
            db.execute("INSERT INTO timeline(report_id,status,actor_id,detail,created_at) VALUES(?,?,?,?,?)", (rid, status, "demo-admin", "Synthetic demonstration status", created))
        if status == "Resolved":
            resolved_at = (datetime.fromisoformat(created.replace("Z", "+00:00")) + timedelta(hours=26)).isoformat(timespec="seconds").replace("+00:00", "Z")
            db.execute("UPDATE reports SET resolution_details=?,resolved_at=? WHERE id=?", ("Synthetic demo: repair marked complete for interface demonstration.", resolved_at, rid))


init_db()


class RegisterBody(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split()).strip()
        if len(cleaned) < 2:
            raise ValueError("Enter your name.")
        return cleaned


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ProfileBody(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    password: Optional[str] = Field(default=None, min_length=8, max_length=128)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split()).strip()
        if len(cleaned) < 2:
            raise ValueError("Enter your name.")
        return cleaned


class ReportUpdate(BaseModel):
    status: Optional[str] = None
    severity: Optional[str] = None
    department: Optional[str] = Field(default=None, max_length=100)
    assignee: Optional[str] = Field(default=None, max_length=100)
    resolution_details: Optional[str] = Field(default=None, max_length=2000)
    impact_override: Optional[int] = Field(default=None, ge=0, le=100)
    override_reason: Optional[str] = Field(default=None, max_length=500)


class NoteBody(BaseModel):
    note: str = Field(min_length=2, max_length=2000)

    @field_validator("note")
    @classmethod
    def clean_note(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 2:
            raise ValueError("Enter at least two characters.")
        return cleaned


class MergeBody(BaseModel):
    canonical_report_id: str = Field(min_length=5, max_length=40)


def issue_token(user: sqlite3.Row) -> str:
    payload = {"sub": user["id"], "exp": int(time.time()) + TOKEN_LIFETIME_HOURS * 3600}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(TOKEN_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    return encoded + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def read_token(token: str) -> str:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(TOKEN_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
        provided = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, provided):
            raise ValueError("bad signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if payload.get("exp", 0) < time.time():
            raise ValueError("expired")
        return payload["sub"]
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Your session expired. Please sign in again.") from exc


def user_payload(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"], "createdAt": row["created_at"]}


def get_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> Optional[sqlite3.Row]:
    if not credentials:
        return None
    user_id = read_token(credentials.credentials)
    with connect() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="Account is no longer available.")
    return user


def signed_in(user: Optional[sqlite3.Row] = Depends(get_user)) -> sqlite3.Row:
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return user


def administrator(user: sqlite3.Row = Depends(signed_in)) -> sqlite3.Row:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user


def throttle(request: Request, action: str, limit: int = 12, window: int = 60) -> None:
    global last_rate_sweep
    key = f"{action}:{request.client.host if request.client else 'unknown'}"
    current = time.time()
    with rate_lock:
        if current - last_rate_sweep > 300:
            stale_before = current - 3600
            for bucket_key in list(rate_buckets):
                recent_entries = [stamp for stamp in rate_buckets[bucket_key] if stamp >= stale_before]
                if recent_entries:
                    rate_buckets[bucket_key] = recent_entries
                else:
                    rate_buckets.pop(bucket_key, None)
            last_rate_sweep = current
        recent = [stamp for stamp in rate_buckets[key] if current - stamp < window]
        if len(recent) >= limit:
            rate_buckets[key] = recent
            raise HTTPException(status_code=429, detail="Too many attempts. Please wait a minute and try again.")
        recent.append(current)
        rate_buckets[key] = recent


def add_audit(db: sqlite3.Connection, report_id: str, action: str, actor_id: Optional[str], payload: dict) -> None:
    db.execute("INSERT INTO audit_log(report_id,action,actor_id,payload,created_at) VALUES(?,?,?,?,?)",
               (report_id, action, actor_id, json.dumps(payload, ensure_ascii=False), now_iso()))


def add_timeline(db: sqlite3.Connection, report_id: str, status: str, actor_id: Optional[str], detail: str) -> None:
    db.execute("INSERT INTO timeline(report_id,status,actor_id,detail,created_at) VALUES(?,?,?,?,?)",
               (report_id, status, actor_id, detail, now_iso()))


def email_is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM and (bool(SMTP_USERNAME) == bool(SMTP_PASSWORD)))


def send_mail(recipient: str, subject: str, body: str) -> str:
    if not email_is_configured():
        return "not_configured"
    message = EmailMessage()
    message["From"] = SMTP_FROM
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        context = ssl.create_default_context()
        client_class = smtplib.SMTP_SSL if SMTP_USE_SSL else smtplib.SMTP
        connection = client_class(SMTP_HOST, SMTP_PORT, timeout=12, context=context) if SMTP_USE_SSL else client_class(SMTP_HOST, SMTP_PORT, timeout=12)
        with connection as client:
            if not SMTP_USE_SSL and SMTP_USE_STARTTLS:
                client.starttls(context=context)
                client.ehlo()
            if SMTP_USERNAME:
                client.login(SMTP_USERNAME, SMTP_PASSWORD)
            client.send_message(message)
        return "sent"
    except (OSError, smtplib.SMTPException, ssl.SSLError):
        return "failed"


def deliver_report_email(report_id: str, event_type: str, status: str = "") -> str:
    with connect() as db:
        row = db.execute("""SELECT r.id,r.is_sensitive,r.first_response_due_at,r.reporter_id,u.email
          FROM reports r LEFT JOIN users u ON u.id=r.reporter_id WHERE r.id=?""", (report_id,)).fetchone()
    if not row or not row["email"]:
        return "unavailable"
    recipient = row["email"]
    private_prefix = "Private report" if row["is_sensitive"] else "CIVICLENS report"
    if event_type == "report_received":
        subject = f"{private_prefix} received · {report_id}"
        body = (
            f"CIVICLENS has received your report {report_id}. It is recorded as Submitted.\n\n"
            f"Our target is to provide an initial CIVICLENS review update within 48 hours (by {row['first_response_due_at']}). "
            "This is a CIVICLENS service target; any government department investigation or final action may take longer and is not guaranteed by this independent platform.\n\n"
            "Sign in to CIVICLENS to follow the report timeline. Please contact local emergency services directly if anyone is in immediate danger."
        )
    else:
        subject = f"{private_prefix} update · {report_id}"
        body = (
            f"There is a new CIVICLENS update for report {report_id}.\n"
            f"Current status: {status or 'Updated'}\n\n"
            "Sign in to CIVICLENS to view the timeline and next steps. This update describes CIVICLENS processing and does not guarantee a government department outcome."
        )
    delivery_status = send_mail(recipient, subject, body)
    with connect() as db:
        db.execute("INSERT INTO email_events(report_id,user_id,event_type,recipient,delivery_status,created_at) VALUES(?,?,?,?,?,?)",
                   (report_id, row["reporter_id"], event_type, recipient, delivery_status, now_iso()))
    return delivery_status


def impact_breakdown(db: sqlite3.Connection, report: sqlite3.Row) -> dict:
    severity_points = SEVERITY_POINTS.get(report["severity"], 6)
    text = (report["title"] + " " + report["description"]).lower()
    safety_points = SAFETY_POINTS.get(report["category"], 12)
    if any(term in text for term in ("injury", "hospital", "live wire", "electric shock", "ambulance", "school crossing")):
        safety_points = min(30, safety_points + 5)
    corroborations = db.execute("SELECT COUNT(*) FROM verifications WHERE report_id=?", (report["id"],)).fetchone()[0]
    duplicate_count = db.execute("SELECT COUNT(*) FROM reports WHERE duplicate_of=?", (report["id"],)).fetchone()[0]
    corroboration_points = min(20, corroborations * 4 + duplicate_count * 5)
    try:
        created = datetime.fromisoformat(report["created_at"].replace("Z", "+00:00"))
        age_hours = max(0, (datetime.now(timezone.utc) - created).total_seconds() / 3600)
    except (ValueError, TypeError):
        age_hours = 0
    age_points = min(20, int(age_hours / 24 * 2))
    raw = min(100, severity_points + safety_points + corroboration_points + age_points)
    effective = report["impact_override"] if report["impact_override"] is not None else raw
    return {
        "score": effective, "calculatedScore": raw, "isOverridden": report["impact_override"] is not None,
        "overrideReason": report["override_reason"] or None,
        "factors": [
            {"name": "Severity level", "points": severity_points, "maximum": 30, "explanation": f"Current {report['severity']} level maps to {severity_points} points."},
            {"name": "Potential safety impact", "points": safety_points, "maximum": 30, "explanation": "Category baseline with a small, documented safety-keyword adjustment."},
            {"name": "Community corroboration", "points": corroboration_points, "maximum": 20, "explanation": f"{corroborations} confirmations and {duplicate_count} linked duplicate report(s)."},
            {"name": "Report age", "points": age_points, "maximum": 20, "explanation": f"{int(age_hours)} hours old; points increase by 2 per day up to 20."},
        ],
    }


def timeline_for(db: sqlite3.Connection, report_id: str) -> list[dict]:
    rows = db.execute("SELECT status,detail,created_at FROM timeline WHERE report_id=? ORDER BY id", (report_id,)).fetchall()
    return [{"status": row["status"], "detail": row["detail"], "createdAt": row["created_at"]} for row in rows]


def public_text(value: str) -> str:
    value = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[contact hidden]", value, flags=re.IGNORECASE)
    return re.sub(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)", "[contact hidden]", value)


def response_sla_state(due_at: str, response_at: Optional[str]) -> str:
    try:
        due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        response = datetime.fromisoformat(response_at.replace("Z", "+00:00")) if response_at else None
    except (AttributeError, ValueError):
        return "unknown"
    if response:
        return "met" if response <= due else "late"
    return "overdue" if datetime.now(timezone.utc) > due else "on_track"


def report_payload(db: sqlite3.Connection, report: sqlite3.Row, include_private: bool = False, include_admin: bool = False) -> dict:
    publicly_visible = not include_private and not include_admin
    timeline = timeline_for(db, report["id"])
    if publicly_visible:
        for item in timeline:
            item["detail"] = public_text(item["detail"])
    due_at = report["first_response_due_at"] if "first_response_due_at" in report.keys() else ""
    if not due_at:
        try:
            due_at = (datetime.fromisoformat(report["created_at"].replace("Z", "+00:00")) + timedelta(hours=FIRST_RESPONSE_TARGET_HOURS)).isoformat(timespec="seconds").replace("+00:00", "Z")
        except ValueError:
            due_at = ""
    evidence = [dict(row) for row in db.execute("SELECT id,caption,created_at AS createdAt FROM resolution_evidence WHERE report_id=? ORDER BY id", (report["id"],)).fetchall()]
    result = {
        "id": report["id"], "category": report["category"], "categoryLabel": CATEGORIES.get(report["category"], "Other civic issue"),
        "title": public_text(report["title"]) if publicly_visible else report["title"],
        "description": public_text(report["description"]) if publicly_visible else report["description"],
        "location": public_text(report["location"]) if publicly_visible else report["location"],
        "ward": public_text(report["ward"]) if publicly_visible else report["ward"],
        "lat": report["lat"], "lng": report["lng"], "severity": report["severity"], "status": report["status"],
        "department": report["department"], "assignee": report["assignee"], "createdAt": report["created_at"],
        "updatedAt": report["updated_at"], "resolvedAt": report["resolved_at"],
        "resolutionDetails": public_text(report["resolution_details"]) if publicly_visible else report["resolution_details"],
        "impact": impact_breakdown(db, report), "timeline": timeline,
        "verificationCount": db.execute("SELECT COUNT(*) FROM verifications WHERE report_id=?", (report["id"],)).fetchone()[0],
        "duplicateOf": report["duplicate_of"], "isDemo": bool(report["demo"]),
        "firstResponseDueAt": due_at, "firstResponseAt": report["first_response_at"] if "first_response_at" in report.keys() else None,
        "responseSlaStatus": response_sla_state(due_at, report["first_response_at"] if "first_response_at" in report.keys() else None),
        "resolutionEvidence": evidence,
        "resolutionConfirmedAt": report["resolution_confirmed_at"] if "resolution_confirmed_at" in report.keys() else None,
    }
    if include_private:
        result["photoAvailable"] = bool(report["photo_name"])
        result["isSensitive"] = bool(report["is_sensitive"])
        result["reportedOfficialRole"] = report["reported_official_role"]
        result["reportedOfficialName"] = report["reported_official_name"]
        result["incidentAt"] = report["incident_at"]
        result["ai"] = {"provider": report["ai_provider"], "suggestedCategory": report["ai_category"] or None,
                         "suggestedSeverity": report["ai_severity"] or None,
                         "confidence": report["ai_confidence"], "explanation": report["ai_explanation"]}
    if include_admin:
        result["isSensitive"] = bool(report["is_sensitive"])
        result["reportedOfficialRole"] = report["reported_official_role"]
        result["reportedOfficialName"] = report["reported_official_name"]
        result["incidentAt"] = report["incident_at"]
        result["reporter"] = None
        if report["reporter_id"]:
            reporter = db.execute("SELECT id,name,email FROM users WHERE id=?", (report["reporter_id"],)).fetchone()
            if reporter:
                result["reporter"] = {"id": reporter["id"], "name": reporter["name"], "email": reporter["email"]}
        result["internalNotes"] = [dict(row) for row in db.execute("""SELECT n.note,n.created_at AS createdAt,u.name AS author
              FROM internal_notes n JOIN users u ON u.id=n.author_id WHERE n.report_id=? ORDER BY n.id DESC""", (report["id"],)).fetchall()]
        result["auditLog"] = [
            {"action": row["action"], "payload": json.loads(row["payload"]), "createdAt": row["created_at"], "actor": row["actor"]}
            for row in db.execute("""SELECT a.action,a.payload,a.created_at,u.name AS actor FROM audit_log a
              LEFT JOIN users u ON u.id=a.actor_id WHERE a.report_id=? ORDER BY a.id DESC LIMIT 100""", (report["id"],)).fetchall()
        ]
        result["emailEvents"] = [dict(row) for row in db.execute("""SELECT event_type AS eventType,recipient,delivery_status AS deliveryStatus,created_at AS createdAt
          FROM email_events WHERE report_id=? ORDER BY id DESC LIMIT 20""", (report["id"],)).fetchall()]
    return result


def report_filters(db: sqlite3.Connection, category: str, severity: str, status: str, date_from: Optional[str], date_to: Optional[str], search: str, open_only: bool = False, include_sensitive: bool = False) -> list[sqlite3.Row]:
    where, values = ([] if include_sensitive else ["is_sensitive=0"]), []
    if category:
        where.append("category=?"); values.append(category)
    if severity:
        where.append("severity=?"); values.append(severity)
    if status:
        where.append("status=?"); values.append(status)
    if date_from:
        where.append("date(created_at)>=date(?)"); values.append(date_from)
    if date_to:
        where.append("date(created_at)<=date(?)"); values.append(date_to)
    if search:
        where.append("(title LIKE ? OR description LIKE ? OR location LIKE ? OR id LIKE ?)")
        pattern = f"%{search[:100]}%"; values.extend([pattern] * 4)
    if open_only:
        where.append("status NOT IN ('Resolved','Rejected')")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return db.execute("SELECT * FROM reports" + clause + " ORDER BY created_at DESC LIMIT 500", values).fetchall()


def validate_report_filters(category: str, severity: str, status: str, date_from: Optional[str], date_to: Optional[str]) -> None:
    if category and category not in CATEGORIES:
        raise HTTPException(status_code=422, detail="Choose a valid issue category filter.")
    if severity and severity not in SEVERITIES:
        raise HTTPException(status_code=422, detail="Choose a valid severity filter.")
    if status and status not in STATUSES:
        raise HTTPException(status_code=422, detail="Choose a valid status filter.")
    parsed_dates = []
    for value in (date_from, date_to):
        if value is None:
            parsed_dates.append(None)
            continue
        try:
            parsed_dates.append(date.fromisoformat(value))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD format.") from exc
    if parsed_dates[0] and parsed_dates[1] and parsed_dates[0] > parsed_dates[1]:
        raise HTTPException(status_code=422, detail="The start date must be on or before the end date.")


def validate_image(data: bytes, filename: str) -> str:
    if not data:
        raise HTTPException(status_code=400, detail="Choose an image to upload.")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Images must be 6 MB or smaller.")
    signatures = {
        "jpg": data.startswith(b"\xff\xd8\xff"), "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "webp": len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    }
    extension = Path(filename or "photo").suffix.lower().lstrip(".")
    if extension == "jpeg":
        extension = "jpg"
    if extension not in signatures or not signatures[extension]:
        raise HTTPException(status_code=415, detail="Use a valid JPG, PNG, or WebP image. SVG and other files are not accepted.")
    return extension


def sanitize_photo(data: bytes) -> bytes:
    """Decode and re-encode an uploaded photo to remove metadata and active payloads."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            if getattr(source, "n_frames", 1) > 1:
                raise HTTPException(status_code=415, detail="Animated images are not accepted. Upload a still photo.")
            if source.width * source.height > 25_000_000:
                raise HTTPException(status_code=413, detail="Image dimensions are too large.")
            source.load()
            image = ImageOps.exif_transpose(source)
            if "A" in image.getbands() or "transparency" in image.info:
                foreground = image.convert("RGBA")
                flattened = Image.new("RGB", foreground.size, "white")
                flattened.paste(foreground, mask=foreground.getchannel("A"))
                image = flattened
            else:
                image = image.convert("RGB")
            image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            for quality in (88, 80, 72):
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True)
                cleaned = output.getvalue()
                if len(cleaned) <= MAX_IMAGE_BYTES:
                    return cleaned
            raise HTTPException(status_code=413, detail="The image could not be compressed below the 6 MB upload limit.")
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=415, detail="The selected file is not a readable image.") from exc


def perceptual_image_hash(data: bytes) -> str:
    """Return a small average-hash used only to suggest visually similar uploads."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 25_000_000:
                raise HTTPException(status_code=413, detail="Image dimensions are too large.")
            pixels = list(image.convert("L").resize((8, 8), Image.Resampling.LANCZOS).getdata())
        average = sum(pixels) / len(pixels)
        value = 0
        for pixel in pixels:
            value = (value << 1) | int(pixel >= average)
        return f"{value:016x}"
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=415, detail="The selected file is not a readable image.") from exc


def image_similarity(hash_a: str, hash_b: str) -> float:
    if len(hash_a) != 16 or len(hash_b) != 16:
        return 0.0
    distance = (int(hash_a, 16) ^ int(hash_b, 16)).bit_count()
    return 1 - distance / 64


def normalize_ai_result(value: dict, provider: str) -> dict:
    category = value.get("category")
    if category not in AI_CATEGORIES:
        category = "other"
    severity = value.get("severity")
    if severity not in SEVERITIES:
        severity = "Medium"
    try:
        parsed_confidence = float(value.get("confidence"))
        confidence = max(0, min(1, parsed_confidence)) if math.isfinite(parsed_confidence) else None
    except (TypeError, ValueError):
        confidence = None
    explanation = str(value.get("explanation", ""))[:800]
    return {"mode": "configured-ai", "provider": provider, "category": category, "categoryLabel": AI_CATEGORIES[category],
            "severity": severity, "confidence": confidence, "explanation": explanation,
            "disclaimer": "AI suggestion only. Review the photo and correct the category or severity before submitting."}


def sign_analysis_result(data: bytes, description: str, result: dict) -> str:
    payload = {
        "image": hashlib.sha256(data).hexdigest(),
        "description": hashlib.sha256(description.strip().encode("utf-8")).hexdigest(),
        "provider": result["provider"], "category": result.get("category"),
        "severity": result.get("severity"), "confidence": result.get("confidence"),
        "explanation": result.get("explanation", ""), "exp": int(time.time()) + 1200,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(TOKEN_SECRET.encode(), ("analysis:" + encoded).encode(), hashlib.sha256).digest()
    return encoded + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def verify_analysis_result(token: str, data: bytes, description: str) -> dict:
    try:
        if len(token) > 12_000:
            raise ValueError("oversized token")
        encoded, signature = token.split(".", 1)
        expected = hmac.new(TOKEN_SECRET.encode(), ("analysis:" + encoded).encode(), hashlib.sha256).digest()
        provided = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, provided):
            raise ValueError("invalid signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        image_hash = hashlib.sha256(data).hexdigest()
        description_hash = hashlib.sha256(description.strip().encode("utf-8")).hexdigest()
        if payload.get("exp", 0) < time.time() or payload.get("image") != image_hash or payload.get("description") != description_hash:
            raise ValueError("analysis context changed")
        if payload.get("category") not in AI_CATEGORIES and payload.get("category") is not None:
            raise ValueError("unknown category")
        if payload.get("severity") not in SEVERITIES and payload.get("severity") is not None:
            raise ValueError("unknown severity")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=422, detail="The analysis no longer matches this photo and description. Analyze it again before submitting.") from exc


def analyze_with_provider(data: bytes, extension: str, description: str) -> dict:
    content_type = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[extension]
    image_b64 = base64.b64encode(data).decode("ascii")
    prompt = (
        "Inspect this photo for a civic infrastructure issue. Return ONLY JSON with keys category, severity, confidence, explanation. "
        "category must be one of pothole, garbage, blocked_drain, streetlight, footpath, illegal_dumping, fallen_tree, other. "
        "severity must be Low, Medium, High, or Critical. confidence is a number from 0 to 1. "
        "Do not infer an issue that is not visible. Use other and low confidence when uncertain. Keep explanation under 45 words. "
        f"Citizen description for context: {description[:1000]}"
    )
    body = json.dumps({"model": AI_MODEL, "response_format": {"type": "json_object"}, "messages": [
        {"role": "system", "content": "You classify civic issue photos carefully and report uncertainty honestly."},
        {"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{image_b64}"}}]},
    ]}).encode()
    request = urllib.request.Request(AI_API_URL, data=body, headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            response_bytes = response.read(1_048_577)
        if len(response_bytes) > 1_048_576:
            raise ValueError("provider response exceeded limit")
        payload = json.loads(response_bytes.decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return normalize_ai_result(parsed, f"Configured vision provider · {AI_MODEL}")
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"AI provider returned HTTP {exc.code}. Check the configured endpoint and key.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="Could not reach the configured AI provider. Try again or continue without analysis.") from exc
    except (KeyError, TypeError, ValueError, IndexError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="The configured AI provider returned an unreadable response.") from exc


def demo_analysis(description: str) -> dict:
    """Text-only hint for demos; deliberately makes no claim to inspect pixels."""
    text = description.lower()
    terms = {
        "pothole": ("pothole", "crater", "road damage", "asphalt"),
        "garbage": ("garbage", "overflow", "waste", "rubbish", "trash"),
        "blocked_drain": ("drain", "waterlog", "waterlogging", "standing water", "flood"),
        "streetlight": ("streetlight", "street light", "lamp", "dark at night"),
        "footpath": ("footpath", "sidewalk", "pavement", "paving"),
        "illegal_dumping": ("dump", "debris", "construction waste"),
        "fallen_tree": ("fallen tree", "branch", "tree blocking"),
    }
    category = next((key for key, words in terms.items() if any(word in text for word in words)), None)
    risk_terms = ("live wire", "injury", "ambulance", "school", "deep", "blocking", "traffic")
    severity = "High" if any(term in text for term in risk_terms) else ("Medium" if category else None)
    return {
        "mode": "demo", "provider": "Demo mode · text-only heuristic", "category": category,
        "categoryLabel": CATEGORIES.get(category) if category else None, "severity": severity,
        "confidence": None,
        "explanation": ("Demo mode did not inspect the image. This category hint comes only from matching words in your description; review or replace it."
                        if category else "Demo mode did not inspect the image. No category hint matched the description; choose the issue yourself."),
        "disclaimer": "DEMO ANALYSIS · No vision model ran. Confidence is unavailable. Your photo was not analyzed by AI.",
    }


@app.get("/api/health")
def health() -> dict:
    with connect() as db:
        report_count = db.execute("SELECT COUNT(*) FROM reports WHERE is_sensitive=0").fetchone()[0]
    return {"status": "ok", "database": "sqlite", "demoMode": DEMO_MODE, "aiMode": "configured" if AI_API_KEY else "demo", "reports": report_count}


@app.get("/api/config")
def public_config() -> dict:
    return {"demoMode": DEMO_MODE, "aiMode": "configured" if AI_API_KEY else "demo",
            "emailMode": "configured" if email_is_configured() else "not_configured",
            "firstResponseTargetHours": FIRST_RESPONSE_TARGET_HOURS,
            "categories": [{"id": key, "label": label} for key, label in AI_CATEGORIES.items()],
            "reportCategories": [{"id": key, "label": label} for key, label in CATEGORIES.items()],
            "statuses": sorted(STATUSES)}


@app.post("/api/auth/register", status_code=201)
def register(body: RegisterBody, request: Request) -> dict:
    throttle(request, "register", 6)
    email = str(body.email).lower()
    if email.endswith("@civiclens.demo"):
        raise HTTPException(status_code=400, detail="That email domain is reserved for demonstration accounts.")
    user_id = "usr_" + secrets.token_urlsafe(10)
    try:
        with connect() as db:
            db.execute("INSERT INTO users(id,name,email,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                       (user_id, body.name, email, password_hash(body.password), "citizen", now_iso()))
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="An account with this email already exists.") from exc
    return {"token": issue_token(user), "user": user_payload(user)}


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request) -> dict:
    throttle(request, "login", 10)
    with connect() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (str(body.email).lower(),)).fetchone()
    if (not user or not password_matches(body.password, user["password_hash"])
            or (not DEMO_MODE and str(body.email).lower().endswith("@civiclens.demo"))):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    return {"token": issue_token(user), "user": user_payload(user)}


@app.get("/api/profile")
def profile(user: sqlite3.Row = Depends(signed_in)) -> dict:
    return user_payload(user)


@app.patch("/api/profile")
def update_profile(body: ProfileBody, user: sqlite3.Row = Depends(signed_in)) -> dict:
    with connect() as db:
        if body.password:
            db.execute("UPDATE users SET name=?,password_hash=? WHERE id=?", (" ".join(body.name.split()), password_hash(body.password), user["id"]))
        else:
            db.execute("UPDATE users SET name=? WHERE id=?", (" ".join(body.name.split()), user["id"]))
        updated = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return user_payload(updated)


@app.post("/api/ai/analyze")
async def analyze_photo(request: Request, photo: UploadFile = File(...), description: str = Form(default=""), user: sqlite3.Row = Depends(signed_in)) -> dict:
    throttle(request, "analyze", 15)
    throttle(request, f"analyze-user:{user['id']}", 15, 3600)
    if len(description) > 2000:
        raise HTTPException(status_code=422, detail="Description must be 2,000 characters or fewer.")
    raw = await photo.read(MAX_IMAGE_BYTES + 1)
    extension = validate_image(raw, photo.filename or "photo.jpg")
    perceptual_image_hash(raw)
    safe_photo = await run_in_threadpool(sanitize_photo, raw)
    if AI_API_KEY:
        result = await run_in_threadpool(analyze_with_provider, safe_photo, "jpg", description)
        result["confidencePercent"] = round(result["confidence"] * 100) if result["confidence"] is not None else None
        result["analysisToken"] = sign_analysis_result(raw, description, result)
        return result
    result = demo_analysis(description)
    result["confidencePercent"] = None
    result["analysisToken"] = sign_analysis_result(raw, description, result)
    return result


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def text_similarity(a: str, b: str) -> float:
    left = {word for word in re.findall(r"[a-z0-9]+", a.lower()) if len(word) > 2}
    right = {word for word in re.findall(r"[a-z0-9]+", b.lower()) if len(word) > 2}
    return len(left & right) / len(left | right) if left and right else 0.0


def duplicate_candidates(db: sqlite3.Connection, category: str, title: str, description: str, lat: float, lng: float, exclude_id: Optional[str] = None, photo_hash: str = "") -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = db.execute("SELECT * FROM reports WHERE is_sensitive=0 AND created_at>=? AND duplicate_of IS NULL AND status!='Rejected'", (since,)).fetchall()
    matches = []
    for row in rows:
        if exclude_id and row["id"] == exclude_id:
            continue
        distance = haversine_m(lat, lng, row["lat"], row["lng"])
        similarity = text_similarity(title + " " + description, row["title"] + " " + row["description"])
        visual_similarity = image_similarity(photo_hash, row["photo_hash"])
        same_category = category == row["category"]
        image_match = bool(photo_hash and row["photo_hash"] and visual_similarity >= 0.78)
        if (same_category and distance <= 250) or (distance <= 500 and similarity >= 0.35) or (image_match and distance <= 1000):
            score = min(99, int((35 if same_category else 0) + max(0, 30 * (1 - distance / 1000)) + 15 * similarity + 20 * visual_similarity))
            matches.append({"reportId": row["id"], "title": row["title"], "category": row["category"], "status": row["status"], "location": row["location"], "distanceMeters": round(distance), "similarityScore": score, "imageSimilarityPercent": round(visual_similarity * 100) if image_match else None})
    return sorted(matches, key=lambda item: item["similarityScore"], reverse=True)[:5]


@app.post("/api/reports", status_code=201)
async def create_report(
    request: Request, category: str = Form(...), title: str = Form(...), description: str = Form(...), location: str = Form(...),
    ward: str = Form(default=""), lat: float = Form(...), lng: float = Form(...), severity: str = Form(...),
    ai_analysis_token: str = Form(default=""), photo: Optional[UploadFile] = File(default=None),
    reported_official_role: str = Form(default=""), reported_official_name: str = Form(default=""),
    incident_at: str = Form(default=""), official_attest: str = Form(default=""), submission_key: str = Form(default=""),
    user: sqlite3.Row = Depends(signed_in),
) -> dict:
    throttle(request, f"submit:{user['id']}", 20, 3600)
    title, description, location, ward = title.strip(), description.strip(), location.strip(), ward.strip()
    if category not in CATEGORIES:
        raise HTTPException(status_code=422, detail="Choose a valid issue category.")
    if severity not in SEVERITIES:
        raise HTTPException(status_code=422, detail="Choose a valid severity.")
    is_sensitive = category == "official_conduct"
    allowed_official_roles = {"Government official", "Police officer", "Other public official"}
    if is_sensitive and reported_official_role not in allowed_official_roles:
        raise HTTPException(status_code=422, detail="Choose whether this concern relates to a government official, police officer, or other public official.")
    if is_sensitive and official_attest.lower() not in {"true", "on", "yes", "1"}:
        raise HTTPException(status_code=422, detail="Confirm that the information is submitted in good faith before sending this private report.")
    if is_sensitive and ai_analysis_token:
        raise HTTPException(status_code=422, detail="Image analysis is not available for confidential official-conduct reports.")
    if not is_sensitive and (reported_official_role or reported_official_name or incident_at or official_attest):
        raise HTTPException(status_code=422, detail="Official-conduct details may only be used for a private official-conduct report.")
    reported_official_name = " ".join(reported_official_name.split())
    if len(reported_official_name) > 120:
        raise HTTPException(status_code=422, detail="Official name must be 120 characters or fewer.")
    if incident_at:
        try:
            incident_at = datetime.fromisoformat(incident_at.replace("Z", "+00:00")).isoformat(timespec="minutes")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Enter a valid incident date and time.") from exc
    if submission_key and not re.fullmatch(r"[A-Za-z0-9_-]{12,80}", submission_key):
        raise HTTPException(status_code=422, detail="The offline submission key is invalid.")
    if not (5 <= len(title) <= 120) or not (10 <= len(description) <= 2000) or not (4 <= len(location) <= 240) or len(ward) > 120:
        raise HTTPException(status_code=422, detail="Check the title, description, address, and ward lengths.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(status_code=422, detail="Enter valid coordinates for this location.")
    if submission_key:
        with connect() as db:
            existing = db.execute("SELECT * FROM reports WHERE reporter_id=? AND submission_key=?", (user["id"], submission_key)).fetchone()
            if existing:
                return {"report": report_payload(db, existing, include_private=True), "potentialDuplicates": [], "emailStatus": "already_processed"}
    photo_name = ""
    saved_photo_path: Optional[Path] = None
    photo_hash = ""
    analysis = None
    if photo:
        raw = await photo.read(MAX_IMAGE_BYTES + 1)
        validate_image(raw, photo.filename or "photo.jpg")
        photo_hash = perceptual_image_hash(raw)
        if ai_analysis_token:
            analysis = verify_analysis_result(ai_analysis_token, raw, description)
        safe_photo = await run_in_threadpool(sanitize_photo, raw)
        photo_name = secrets.token_hex(16) + ".jpg"
        saved_photo_path = UPLOAD_DIR / photo_name
        try:
            with saved_photo_path.open("xb") as photo_file:
                photo_file.write(safe_photo)
        except Exception:
            saved_photo_path.unlink(missing_ok=True)
            raise
    elif ai_analysis_token:
        raise HTTPException(status_code=422, detail="Attach the analyzed photo before submitting the report.")
    report_id = "CL-" + datetime.now().strftime("%Y") + "-" + secrets.token_hex(8).upper()
    created = now_iso()
    due_at = (datetime.now(timezone.utc) + timedelta(hours=FIRST_RESPONSE_TARGET_HOURS)).isoformat(timespec="seconds").replace("+00:00", "Z")
    ai_provider = analysis["provider"] if analysis else "none"
    ai_confidence = analysis.get("confidence") if analysis else None
    ai_explanation = analysis.get("explanation", "") if analysis else ""
    ai_category = (analysis.get("category") or "") if analysis else ""
    ai_severity = (analysis.get("severity") or "") if analysis else ""
    try:
        with connect() as db:
            db.execute("""INSERT INTO reports(id,reporter_id,category,title,description,location,ward,lat,lng,severity,status,department,created_at,updated_at,photo_name,photo_hash,ai_provider,ai_confidence,ai_explanation,ai_category,ai_severity,is_sensitive,reported_official_role,reported_official_name,incident_at,first_response_due_at,submission_key)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (report_id, user["id"], category, title, description, location, ward, lat, lng, severity, "Submitted", DEPARTMENTS[category], created, created, photo_name, photo_hash, ai_provider, ai_confidence, ai_explanation, ai_category, ai_severity, int(is_sensitive), reported_official_role if is_sensitive else "", reported_official_name if is_sensitive else "", incident_at if is_sensitive else "", due_at, submission_key))
            add_timeline(db, report_id, "Submitted", user["id"], "Report received and awaiting municipal review.")
            matches = [] if is_sensitive else duplicate_candidates(db, category, title, description, lat, lng, report_id, photo_hash)
            add_audit(db, report_id, "report_submitted", user["id"], {"aiProvider": ai_provider, "potentialDuplicates": len(matches)})
            row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            result = report_payload(db, row, include_private=True)
    except Exception:
        if saved_photo_path:
            saved_photo_path.unlink(missing_ok=True)
        raise
    try:
        email_status = await run_in_threadpool(deliver_report_email, report_id, "report_received")
    except Exception:
        email_status = "failed"
    return {"report": result, "potentialDuplicates": matches, "emailStatus": email_status}


@app.get("/api/reports")
def public_reports(
    category: str = "", severity: str = "", status: str = "", date_from: Optional[str] = Query(default=None, alias="from"),
    date_to: Optional[str] = Query(default=None, alias="to"), search: str = "", open_only: bool = False,
) -> list[dict]:
    validate_report_filters(category, severity, status, date_from, date_to)
    with connect() as db:
        return [report_payload(db, row) for row in report_filters(db, category, severity, status, date_from, date_to, search, open_only)]


@app.get("/api/reports/mine")
def my_reports(user: sqlite3.Row = Depends(signed_in)) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM reports WHERE reporter_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
        return [report_payload(db, row, include_private=True) for row in rows]


@app.get("/api/reports/{report_id}")
def get_report(report_id: str, user: Optional[sqlite3.Row] = Depends(get_user)) -> dict:
    with connect() as db:
        row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Report not found.")
        is_owner = user and row["reporter_id"] == user["id"]
        is_admin = user and user["role"] == "admin"
        if row["is_sensitive"] and not (is_owner or is_admin):
            raise HTTPException(status_code=404, detail="Report not found.")
        if is_admin:
            return report_payload(db, row, include_private=True, include_admin=True)
        result = report_payload(db, row, include_private=bool(is_owner))
        result["isOwner"] = bool(is_owner)
        return result


@app.get("/api/reports/{report_id}/photo")
def report_photo(report_id: str, user: sqlite3.Row = Depends(signed_in)) -> Response:
    with connect() as db:
        row = db.execute("SELECT reporter_id,photo_name,is_sensitive FROM reports WHERE id=?", (report_id,)).fetchone()
    if not row or not row["photo_name"]:
        raise HTTPException(status_code=404, detail="Photo not found.")
    if row["is_sensitive"] and user["role"] != "admin" and row["reporter_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Photo not found.")
    if user["role"] != "admin" and row["reporter_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only the reporter and administrators can view this photo.")
    path = (UPLOAD_DIR / row["photo_name"]).resolve()
    if not path.is_relative_to(UPLOAD_DIR) or not path.is_file():
        raise HTTPException(status_code=404, detail="Photo not found.")
    return FileResponse(path, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@app.post("/api/reports/{report_id}/verify")
def verify_report(report_id: str, request: Request, user: sqlite3.Row = Depends(signed_in)) -> dict:
    throttle(request, f"verify:{user['id']}", 40, 3600)
    with connect() as db:
        row = db.execute("SELECT reporter_id,status,is_sensitive FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Report not found.")
        if row["reporter_id"] == user["id"]:
            raise HTTPException(status_code=400, detail="You cannot verify your own report.")
        if row["is_sensitive"]:
            raise HTTPException(status_code=404, detail="Report not found.")
        if row["status"] in {"Resolved", "Rejected"}:
            raise HTTPException(status_code=400, detail="This report is closed to community verification.")
        try:
            db.execute("INSERT INTO verifications(report_id,user_id,created_at) VALUES(?,?,?)", (report_id, user["id"], now_iso()))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="You have already confirmed this report.") from exc
        return {"verified": True, "count": db.execute("SELECT COUNT(*) FROM verifications WHERE report_id=?", (report_id,)).fetchone()[0]}


@app.post("/api/reports/{report_id}/reopen")
def reopen_report(report_id: str, body: NoteBody, request: Request, user: sqlite3.Row = Depends(signed_in)) -> dict:
    throttle(request, f"reopen:{user['id']}", 8, 3600)
    with connect() as db:
        row = db.execute("SELECT reporter_id,status FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Report not found.")
        if row["reporter_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Only the original reporter can request a reopened review.")
        if row["status"] != "Resolved":
            raise HTTPException(status_code=409, detail="Only a resolved report can be reopened.")
        created = now_iso()
        db.execute("UPDATE reports SET status='Reopened',updated_at=?,resolution_confirmed_at=NULL WHERE id=?", (created, report_id))
        add_timeline(db, report_id, "Reopened", user["id"], "The reporter requested another review of the resolution.")
        add_audit(db, report_id, "report_reopened", user["id"], {"reason": body.note.strip()})
        current = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        return report_payload(db, current, include_private=True)


@app.post("/api/reports/{report_id}/confirm-resolution")
def confirm_resolution(report_id: str, request: Request, user: sqlite3.Row = Depends(signed_in)) -> dict:
    throttle(request, f"confirm-resolution:{user['id']}", 20, 3600)
    with connect() as db:
        row = db.execute("SELECT reporter_id,status,resolution_confirmed_at FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Report not found.")
        if row["reporter_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Only the original reporter can confirm this resolution.")
        if row["status"] != "Resolved":
            raise HTTPException(status_code=409, detail="Only a resolved report can be confirmed.")
        if row["resolution_confirmed_at"]:
            return {"confirmed": True, "confirmedAt": row["resolution_confirmed_at"]}
        confirmed_at = now_iso()
        db.execute("UPDATE reports SET resolution_confirmed_at=? WHERE id=?", (confirmed_at, report_id))
        add_audit(db, report_id, "resolution_confirmed_by_reporter", user["id"], {})
        return {"confirmed": True, "confirmedAt": confirmed_at}


@app.get("/api/duplicates")
def find_duplicates(report_id: str, user: sqlite3.Row = Depends(administrator)) -> list[dict]:
    with connect() as db:
        row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Report not found.")
        if row["is_sensitive"]:
            return []
        return duplicate_candidates(db, row["category"], row["title"], row["description"], row["lat"], row["lng"], report_id, row["photo_hash"])


@app.get("/api/analytics")
def analytics(user: sqlite3.Row = Depends(administrator)) -> dict:
    with connect() as db:
        reports = db.execute("SELECT * FROM reports").fetchall()
        total = len(reports)
        resolved = [r for r in reports if r["status"] == "Resolved"]
        durations = []
        for row in resolved:
            try:
                created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                resolved_at = row["resolved_at"] or row["updated_at"]
                completed = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
                durations.append(max(0, (completed - created).total_seconds() / 3600))
            except ValueError:
                continue
        categories = [{"category": row["category"], "label": CATEGORIES.get(row["category"], "Other"), "count": row["count"]}
                      for row in db.execute("SELECT category,COUNT(*) AS count FROM reports GROUP BY category ORDER BY count DESC")]
        severities = [{"severity": row["severity"], "count": row["count"]}
                      for row in db.execute("SELECT severity,COUNT(*) AS count FROM reports GROUP BY severity")]
        statuses = [{"status": row["status"], "count": row["count"]}
                    for row in db.execute("SELECT status,COUNT(*) AS count FROM reports GROUP BY status")]
        volume = []
        for day in range(29, -1, -1):
            date = (datetime.now(timezone.utc).date() - timedelta(days=day)).isoformat()
            count = db.execute("SELECT COUNT(*) FROM reports WHERE date(created_at)=date(?)", (date,)).fetchone()[0]
            volume.append({"date": date, "count": count})
        hotspots = [{"ward": row["ward"] or "Unspecified", "openReports": row["count"]}
                    for row in db.execute("""SELECT ward,COUNT(*) AS count FROM reports WHERE status NOT IN ('Resolved','Rejected')
                      GROUP BY ward ORDER BY count DESC LIMIT 8""")]
        sla_rows = db.execute("SELECT created_at,first_response_due_at,first_response_at FROM reports").fetchall()
        sla_states = []
        for sla_row in sla_rows:
            due_at = sla_row["first_response_due_at"]
            if not due_at:
                try:
                    due_at = (datetime.fromisoformat(sla_row["created_at"].replace("Z", "+00:00")) + timedelta(hours=FIRST_RESPONSE_TARGET_HOURS)).isoformat(timespec="seconds").replace("+00:00", "Z")
                except ValueError:
                    continue
            sla_states.append(response_sla_state(due_at, sla_row["first_response_at"]))
        return {"source": "stored-records", "demoRecordsIncluded": any(bool(r["demo"]) for r in reports), "total": total,
                "resolutionRate": round(len(resolved) / total * 100) if total else 0,
                "averageResolutionHours": round(sum(durations) / len(durations), 1) if durations else None,
                "categories": categories, "severities": severities, "statuses": statuses, "volume30Days": volume, "hotspots": hotspots,
                "firstResponseSla": {"targetHours": FIRST_RESPONSE_TARGET_HOURS, "onTrack": sla_states.count("on_track"),
                                     "overdue": sla_states.count("overdue"), "met": sla_states.count("met"), "late": sla_states.count("late")}}


@app.get("/api/admin/reports")
def admin_reports(category: str = "", severity: str = "", status: str = "", search: str = "", user: sqlite3.Row = Depends(administrator)) -> list[dict]:
    validate_report_filters(category, severity, status, None, None)
    with connect() as db:
        return [report_payload(db, row, include_private=True, include_admin=True)
                for row in report_filters(db, category, severity, status, None, None, search, include_sensitive=True)]


@app.patch("/api/admin/reports/{report_id}")
def admin_update_report(report_id: str, body: ReportUpdate, user: sqlite3.Row = Depends(administrator)) -> dict:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Choose at least one field to update.")
    if any(value is None for value in changes.values()):
        raise HTTPException(status_code=422, detail="Use a value for each field you choose to update.")
    if "override_reason" in changes and "impact_override" not in changes:
        raise HTTPException(status_code=422, detail="An override reason must be submitted with an ImpactScore value.")
    if "status" in changes and changes["status"] not in STATUSES:
        raise HTTPException(status_code=422, detail="Choose a valid report status.")
    if "severity" in changes and changes["severity"] not in SEVERITIES:
        raise HTTPException(status_code=422, detail="Choose a valid severity.")
    if "impact_override" in changes and changes["impact_override"] is not None and not (changes.get("override_reason") or "").strip():
        raise HTTPException(status_code=422, detail="Add a reason to override the ImpactScore.")
    with connect() as db:
        current = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Report not found.")
        updated_at = now_iso()
        status_changed = changes.get("status", current["status"]) != current["status"]
        for key, value in changes.items():
            if key == "status" and value != current["status"]:
                add_timeline(db, report_id, value, user["id"], "Status updated by municipal administrator.")
                add_audit(db, report_id, "status_changed", user["id"], {"from": current["status"], "to": value})
            elif key == "severity" and value != current["severity"]:
                add_audit(db, report_id, "severity_changed", user["id"], {"from": current["severity"], "to": value})
            elif key in {"department", "assignee"} and value != current[key]:
                add_audit(db, report_id, "assignment_changed", user["id"], {"field": key, "from": current[key], "to": value})
            elif key == "resolution_details" and value != current["resolution_details"]:
                add_audit(db, report_id, "resolution_recorded", user["id"], {"details": value})
            elif key == "impact_override" and value != current["impact_override"]:
                add_audit(db, report_id, "impact_score_overridden", user["id"], {"from": current["impact_override"], "to": value, "reason": changes.get("override_reason")})
            elif key == "impact_override" and value == current["impact_override"] and changes.get("override_reason") != current["override_reason"]:
                add_audit(db, report_id, "impact_override_reason_updated", user["id"], {"score": value, "reason": changes.get("override_reason")})
        if changes.get("status") == "Resolved" and current["status"] != "Resolved":
            changes["resolved_at"] = updated_at
        elif changes.get("status") == "Reopened" and current["status"] != "Reopened":
            changes["resolved_at"] = None
            changes["resolution_confirmed_at"] = None
        first_response = not current["first_response_at"]
        if first_response:
            changes["first_response_at"] = updated_at
            add_audit(db, report_id, "first_response_recorded", user["id"], {"targetHours": FIRST_RESPONSE_TARGET_HOURS})
        sets = [f"{key}=?" for key in changes]
        values = list(changes.values())
        sets.append("updated_at=?"); values.extend([updated_at, report_id])
        db.execute(f"UPDATE reports SET {', '.join(sets)} WHERE id=?", values)
        row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        result = report_payload(db, row, include_private=True, include_admin=True)
        notify = status_changed or first_response
        new_status = row["status"]
    if notify:
        try:
            result["emailStatus"] = deliver_report_email(report_id, "status_update", new_status)
        except Exception:
            result["emailStatus"] = "failed"
    return result


@app.post("/api/admin/reports/{report_id}/notes", status_code=201)
def add_note(report_id: str, body: NoteBody, user: sqlite3.Row = Depends(administrator)) -> dict:
    with connect() as db:
        if not db.execute("SELECT 1 FROM reports WHERE id=?", (report_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Report not found.")
        created = now_iso()
        db.execute("INSERT INTO internal_notes(report_id,author_id,note,created_at) VALUES(?,?,?,?)", (report_id, user["id"], body.note.strip(), created))
        add_audit(db, report_id, "internal_note_added", user["id"], {"noteLength": len(body.note.strip())})
        return {"note": body.note.strip(), "author": user["name"], "createdAt": created}


@app.post("/api/admin/reports/{report_id}/resolution-evidence", status_code=201)
async def add_resolution_evidence(
    report_id: str, photo: UploadFile = File(...), caption: str = Form(default=""),
    user: sqlite3.Row = Depends(administrator),
) -> dict:
    caption = " ".join(caption.split())
    if len(caption) > 500:
        raise HTTPException(status_code=422, detail="Resolution caption must be 500 characters or fewer.")
    with connect() as db:
        report = db.execute("SELECT status,is_sensitive FROM reports WHERE id=?", (report_id,)).fetchone()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    if report["status"] != "Resolved":
        raise HTTPException(status_code=409, detail="Set the report to Resolved before adding completion evidence.")
    raw = await photo.read(MAX_IMAGE_BYTES + 1)
    validate_image(raw, photo.filename or "evidence.jpg")
    cleaned = await run_in_threadpool(sanitize_photo, raw)
    photo_name = "resolution-" + secrets.token_hex(16) + ".jpg"
    path = UPLOAD_DIR / photo_name
    try:
        with path.open("xb") as stored:
            stored.write(cleaned)
        with connect() as db:
            created = now_iso()
            cursor = db.execute("INSERT INTO resolution_evidence(report_id,uploader_id,photo_name,caption,created_at) VALUES(?,?,?,?,?)",
                                (report_id, user["id"], photo_name, caption, created))
            evidence_id = cursor.lastrowid
            add_audit(db, report_id, "resolution_evidence_added", user["id"], {"evidenceId": evidence_id, "captionLength": len(caption)})
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"id": evidence_id, "caption": caption, "createdAt": created}


@app.get("/api/reports/{report_id}/resolution-evidence/{evidence_id}/photo")
def resolution_evidence_photo(
    report_id: str, evidence_id: int, user: Optional[sqlite3.Row] = Depends(get_user),
) -> Response:
    with connect() as db:
        row = db.execute("""SELECT e.photo_name,r.is_sensitive,r.reporter_id FROM resolution_evidence e
          JOIN reports r ON r.id=e.report_id WHERE e.id=? AND e.report_id=?""", (evidence_id, report_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Resolution evidence not found.")
    if row["is_sensitive"] and not (user and (user["role"] == "admin" or user["id"] == row["reporter_id"])):
        raise HTTPException(status_code=404, detail="Resolution evidence not found.")
    path = (UPLOAD_DIR / row["photo_name"]).resolve()
    if not path.is_relative_to(UPLOAD_DIR) or not path.is_file():
        raise HTTPException(status_code=404, detail="Resolution evidence not found.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@app.post("/api/admin/reports/{report_id}/merge")
def merge_duplicate(report_id: str, body: MergeBody, user: sqlite3.Row = Depends(administrator)) -> dict:
    if report_id == body.canonical_report_id:
        raise HTTPException(status_code=400, detail="A report cannot be merged into itself.")
    with connect() as db:
        duplicate = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        canonical = db.execute("SELECT * FROM reports WHERE id=?", (body.canonical_report_id,)).fetchone()
        if not duplicate or not canonical:
            raise HTTPException(status_code=404, detail="Both reports must exist before merging.")
        if bool(duplicate["is_sensitive"]) != bool(canonical["is_sensitive"]):
            raise HTTPException(status_code=409, detail="Private official-conduct concerns can only be merged with another private concern.")
        if duplicate["duplicate_of"]:
            raise HTTPException(status_code=409, detail="This report has already been merged.")
        if canonical["duplicate_of"]:
            raise HTTPException(status_code=409, detail="Choose the original report as the merge target.")
        db.execute("UPDATE reports SET duplicate_of=?,status='Rejected',updated_at=? WHERE id=?", (canonical["id"], now_iso(), report_id))
        add_timeline(db, report_id, "Rejected", user["id"], f"Merged as a duplicate of {canonical['id']}; original history retained.")
        add_audit(db, report_id, "duplicate_merged", user["id"], {"canonicalReportId": canonical["id"]})
        add_audit(db, canonical["id"], "duplicate_linked", user["id"], {"duplicateReportId": report_id})
        result = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        return report_payload(db, result, include_private=True, include_admin=True)


@app.get("/api/admin/export.csv")
def export_csv(user: sqlite3.Row = Depends(administrator)) -> Response:
    with connect() as db:
        rows = db.execute("SELECT id,category,title,description,location,ward,lat,lng,severity,status,department,assignee,created_at,updated_at,resolution_details,duplicate_of,demo FROM reports ORDER BY created_at DESC").fetchall()
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Report ID", "Category", "Title", "Description", "Location", "Ward", "Latitude", "Longitude", "Severity", "Status", "Department", "Assignee", "Created UTC", "Updated UTC", "Resolution", "Duplicate of", "Demo record"])
    for row in rows:
        values = list(row)
        for index, value in enumerate(values):
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                values[index] = "'" + value
        writer.writerow(values)
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=civiclens-reports.csv", "Cache-Control": "no-store"})


@app.get("/api/notifications")
def notifications(user: sqlite3.Row = Depends(signed_in)) -> list[dict]:
    with connect() as db:
        rows = db.execute("""SELECT t.report_id,t.status,t.detail,t.created_at,r.title FROM timeline t
          JOIN reports r ON r.id=t.report_id WHERE r.reporter_id=? ORDER BY t.id DESC LIMIT 30""", (user["id"],)).fetchall()
    return [{"reportId": row["report_id"], "status": row["status"], "detail": row["detail"], "title": row["title"], "createdAt": row["created_at"]} for row in rows]


@app.get("/api/stats")
def public_stats() -> dict:
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM reports WHERE is_sensitive=0").fetchone()[0]
        resolved = db.execute("SELECT COUNT(*) FROM reports WHERE is_sensitive=0 AND status='Resolved'").fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM reports WHERE is_sensitive=0 AND status NOT IN ('Resolved','Rejected')").fetchone()[0]
        verified = db.execute("SELECT COUNT(*) FROM verifications v JOIN reports r ON r.id=v.report_id WHERE r.is_sensitive=0").fetchone()[0]
        return {"total": total, "resolved": resolved, "active": active, "communityConfirmations": verified, "demoRecordsIncluded": db.execute("SELECT COUNT(*) FROM reports WHERE is_sensitive=0 AND demo=1").fetchone()[0] > 0}


@app.get("/api/admin/audit")
def audit_log(report_id: Optional[str] = None, limit: int = Query(default=100, ge=1, le=500), user: sqlite3.Row = Depends(administrator)) -> list[dict]:
    with connect() as db:
        if report_id:
            rows = db.execute("""SELECT a.report_id,a.action,a.payload,a.created_at,u.name AS actor FROM audit_log a
              LEFT JOIN users u ON u.id=a.actor_id WHERE a.report_id=? ORDER BY a.id DESC LIMIT ?""", (report_id, limit)).fetchall()
        else:
            rows = db.execute("""SELECT a.report_id,a.action,a.payload,a.created_at,u.name AS actor FROM audit_log a
              LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()
    return [{"reportId": row["report_id"], "action": row["action"], "payload": json.loads(row["payload"]), "createdAt": row["created_at"], "actor": row["actor"]} for row in rows]


@app.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/{asset_name}")
def frontend_asset(asset_name: str) -> Response:
    if asset_name == "robots.txt":
        return Response("User-agent: *\nDisallow: /api/\n", media_type="text/plain")
    allowed = {"styles.css": "text/css", "app.js": "text/javascript", "locales.js": "text/javascript",
               "service-worker.js": "text/javascript", "manifest.json": "application/manifest+json", "icon.svg": "image/svg+xml"}
    if asset_name not in allowed:
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(WEB_DIR / asset_name, media_type=allowed[asset_name])

