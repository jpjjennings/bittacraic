from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import qrcode
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError
from PIL import ImageOps
from pillow_heif import register_heif_opener


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", ROOT / "uploads"))
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "bittacraic.sqlite3"))
SETTINGS_PATH = Path(os.getenv("SETTINGS_FILE", ROOT / "settings.json"))
MAX_UPLOAD_MB = max(1, int(os.getenv("MAX_UPLOAD_MB", "15")))
MAX_PHOTOS_PER_SUBMISSION = max(1, int(os.getenv("MAX_PHOTOS_PER_SUBMISSION", "10")))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_KEY = os.getenv("ADMIN_SESSION_SECRET", ADMIN_PASSWORD).encode("utf-8")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

with SETTINGS_PATH.open(encoding="utf-8") as settings_file:
    SETTINGS = json.load(settings_file)
register_heif_opener()

PROMPTS = SETTINGS["prompts"]
TEXT = SETTINGS["text"]
DEFAULT_COUNTDOWN_HOURS = int(os.getenv("GALLERY_COUNTDOWN_HOURS", SETTINGS.get("countdown_hours", 72)))

app = FastAPI(title="Bitta Craic", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed', 'live')),
                go_live_at TEXT,
                countdown_hours INTEGER NOT NULL DEFAULT 72
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guest_name TEXT,
                table_number TEXT,
                challenge TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL,
                approval TEXT NOT NULL DEFAULT 'pending' CHECK (approval IN ('pending', 'approved', 'rejected'))
            );
            CREATE INDEX IF NOT EXISTS photos_submission_idx ON photos(submission_id);
            CREATE INDEX IF NOT EXISTS submissions_challenge_idx ON submissions(challenge);
            """
        )
        db.execute(
            "INSERT OR IGNORE INTO settings (id, state, countdown_hours) VALUES (1, 'open', ?)",
            (DEFAULT_COUNTDOWN_HOURS,),
        )


@app.on_event("startup")
def startup() -> None:
    initialize_database()


def get_app_state(db: sqlite3.Connection) -> tuple[str, datetime | None, int]:
    row = db.execute("SELECT state, go_live_at, countdown_hours FROM settings WHERE id = 1").fetchone()
    state = row["state"]
    go_live_at = datetime.fromisoformat(row["go_live_at"]) if row["go_live_at"] else None
    if state == "closed" and go_live_at and now_utc() >= go_live_at:
        db.execute("UPDATE settings SET state = 'live' WHERE id = 1")
        state = "live"
    return state, go_live_at, row["countdown_hours"]


def session_signature(payload: str) -> str:
    return hmac.new(SESSION_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def is_admin(request: Request) -> bool:
    if not ADMIN_PASSWORD or not SESSION_KEY:
        return False
    token = request.cookies.get("bitta_admin", "")
    try:
        payload, signature = token.rsplit(".", 1)
        timestamp = int(payload.split(":", 1)[0])
    except (ValueError, IndexError):
        return False
    return (
        abs(time.time() - timestamp) < 7 * 24 * 60 * 60
        and hmac.compare_digest(signature, session_signature(payload))
    )


def require_admin(request: Request) -> RedirectResponse | None:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


def page(request: Request, name: str, **context: object) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "request": request, "text": TEXT, "prompts": PROMPTS,
            "max_photos": MAX_PHOTOS_PER_SUBMISSION, "max_upload_mb": MAX_UPLOAD_MB,
            **context,
        },
    )


@app.get("/", response_class=HTMLResponse, name="home")
def home(request: Request) -> HTMLResponse:
    with database() as db:
        state, go_live_at, _ = get_app_state(db)
        photos = db.execute(
            """SELECT photos.id, submissions.challenge
               FROM photos JOIN submissions ON submissions.id = photos.submission_id
               WHERE photos.approval = 'approved' ORDER BY photos.id DESC"""
        ).fetchall() if state == "live" else []
    gallery_groups = {prompt: [] for prompt in PROMPTS}
    for photo in photos:
        gallery_groups.setdefault(photo["challenge"], []).append(photo["id"])
    return page(
        request, "guest.html", state=state,
        go_live_at=iso_utc(go_live_at) if go_live_at else "", gallery_groups=gallery_groups,
        has_gallery=any(gallery_groups.values()),
    )


@app.post("/submit")
async def submit(
    request: Request,
    guest_name: str = Form(default=""),
    table_number: str = Form(default=""),
    challenge: str = Form(...),
    photos: list[UploadFile] = File(...),
) -> Response:
    if len(photos) > MAX_PHOTOS_PER_SUBMISSION:
        return page(request, "guest.html", state="open", go_live_at="", error=f"Please choose no more than {MAX_PHOTOS_PER_SUBMISSION} photos.")
    if challenge not in PROMPTS:
        return page(request, "guest.html", state="open", go_live_at="", error="Please choose a challenge from the list.")
    with database() as db:
        state, _, _ = get_app_state(db)
        if state != "open":
            return RedirectResponse("/", status_code=303)

    validated: list[tuple[bytes, str, str]] = []
    for upload in photos:
        content = await upload.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
        if not content:
            continue
        if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
            return page(request, "guest.html", state="open", go_live_at="", error=f"Each photo must be smaller than {MAX_UPLOAD_MB} MB.")
        try:
            image = Image.open(io.BytesIO(content))
            image.verify()
            image_format = image.format
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            return page(request, "guest.html", state="open", go_live_at="", error="One of those files is not a supported image.")
        if image_format == "HEIF":
            image = ImageOps.exif_transpose(Image.open(io.BytesIO(content))).convert("RGB")
            converted = io.BytesIO()
            image.save(converted, format="JPEG", quality=90)
            content = converted.getvalue()
            image_format = "JPEG"
        extensions = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}
        extension = extensions.get(image_format or "")
        if not extension:
            return page(request, "guest.html", state="open", go_live_at="", error="Please upload JPG, PNG, WEBP, or GIF photos.")
        original_name = Path(upload.filename or "photo").name[:200]
        validated.append((content, extension, original_name))
    if not validated:
        return page(request, "guest.html", state="open", go_live_at="", error="Choose at least one photo to submit.")

    stored: list[str] = []
    try:
        with database() as db:
            state, _, _ = get_app_state(db)
            if state != "open":
                return RedirectResponse("/", status_code=303)
            cursor = db.execute(
                "INSERT INTO submissions (guest_name, table_number, challenge, created_at) VALUES (?, ?, ?, ?)",
                (guest_name.strip()[:100] or None, table_number.strip()[:50] or None, challenge, iso_utc(now_utc())),
            )
            submission_id = cursor.lastrowid
            for content, extension, original_name in validated:
                filename = f"{uuid.uuid4().hex}{extension}"
                (UPLOAD_DIR / filename).write_bytes(content)
                stored.append(filename)
                db.execute(
                    "INSERT INTO photos (submission_id, filename, original_name) VALUES (?, ?, ?)",
                    (submission_id, filename, original_name),
                )
    except Exception:
        for filename in stored:
            (UPLOAD_DIR / filename).unlink(missing_ok=True)
        raise
    return page(request, "thanks.html")


@app.get("/photos/{photo_id}")
def public_photo(photo_id: int) -> Response:
    with database() as db:
        state, _, _ = get_app_state(db)
        photo = db.execute(
            "SELECT filename FROM photos WHERE id = ? AND approval = 'approved'", (photo_id,)
        ).fetchone()
    if state != "live" or not photo:
        return Response(status_code=404)
    path = UPLOAD_DIR / photo["filename"]
    return FileResponse(path) if path.is_file() else Response(status_code=404)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request, error: str = "") -> HTMLResponse:
    if is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    return page(request, "login.html", error=error, password_configured=bool(ADMIN_PASSWORD))


@app.post("/admin/login")
def admin_login_submit(request: Request, password: str = Form(...)) -> Response:
    if ADMIN_PASSWORD and hmac.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
        payload = f"{int(time.time())}:{secrets.token_urlsafe(12)}"
        token = f"{payload}.{session_signature(payload)}"
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            "bitta_admin", token, httponly=True,
            secure=request.url.scheme == "https" or PUBLIC_URL.startswith("https://"),
            samesite="strict", max_age=7 * 24 * 60 * 60, path="/",
        )
        return response
    return page(request, "login.html", error="That password didn't match.", password_configured=bool(ADMIN_PASSWORD))


@app.post("/admin/logout")
def admin_logout() -> Response:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("bitta_admin", path="/")
    return response


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request) -> Response:
    if redirect := require_admin(request):
        return redirect
    with database() as db:
        state, go_live_at, countdown_hours = get_app_state(db)
        total_submissions = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        total_photos = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        pending_count = db.execute("SELECT COUNT(*) FROM photos WHERE approval = 'pending'").fetchone()[0]
        category_counts = {
            row["challenge"]: row["total"]
            for row in db.execute("SELECT challenge, COUNT(*) AS total FROM submissions GROUP BY challenge")
        }
        queue = db.execute(
            """SELECT photos.id, photos.filename, photos.original_name, photos.approval,
                      submissions.guest_name, submissions.table_number, submissions.challenge, submissions.created_at
               FROM photos JOIN submissions ON submissions.id = photos.submission_id
               ORDER BY CASE photos.approval WHEN 'pending' THEN 0 ELSE 1 END, photos.id DESC"""
        ).fetchall()
    guest_url = PUBLIC_URL or str(request.url_for("home"))
    qr = qrcode.make(guest_url)
    qr_buffer = io.BytesIO()
    qr.save(qr_buffer, format="PNG")
    qr_data = "data:image/png;base64," + base64.b64encode(qr_buffer.getvalue()).decode("ascii")
    return page(
        request, "admin.html", state=state, go_live_at=iso_utc(go_live_at) if go_live_at else "",
        countdown_hours=countdown_hours, total_submissions=total_submissions, total_photos=total_photos,
        pending_count=pending_count, category_counts=category_counts, queue=queue,
        qr_data=qr_data, guest_url=guest_url,
    )


@app.post("/admin/state")
def update_state(request: Request, action: str = Form(...), countdown_hours: int = Form(default=72)) -> Response:
    if redirect := require_admin(request):
        return redirect
    with database() as db:
        if action == "close":
            hours = min(24 * 30, max(1, countdown_hours))
            go_live_at = iso_utc(now_utc() + timedelta(hours=hours))
            db.execute("UPDATE settings SET state = 'closed', go_live_at = ?, countdown_hours = ? WHERE id = 1", (go_live_at, hours))
        elif action == "live":
            db.execute("UPDATE settings SET state = 'live', go_live_at = NULL WHERE id = 1")
        elif action == "open":
            db.execute("UPDATE settings SET state = 'open', go_live_at = NULL WHERE id = 1")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/photo/{photo_id}/{decision}")
def moderate_photo(request: Request, photo_id: int, decision: str) -> Response:
    if redirect := require_admin(request):
        return redirect
    if decision in {"approve", "reject", "pending"}:
        approval = {"approve": "approved", "reject": "rejected", "pending": "pending"}[decision]
        with database() as db:
            db.execute("UPDATE photos SET approval = ? WHERE id = ?", (approval, photo_id))
    return RedirectResponse("/admin#moderation", status_code=303)


@app.post("/admin/bulk")
def bulk_moderate(request: Request, decision: str = Form(...)) -> Response:
    if redirect := require_admin(request):
        return redirect
    if decision in {"approve", "reject"}:
        approval = "approved" if decision == "approve" else "rejected"
        with database() as db:
            db.execute("UPDATE photos SET approval = ? WHERE approval = 'pending'", (approval,))
    return RedirectResponse("/admin#moderation", status_code=303)


@app.get("/admin/photo/{photo_id}/download")
def download_photo(request: Request, photo_id: int) -> Response:
    if redirect := require_admin(request):
        return redirect
    with database() as db:
        photo = db.execute("SELECT filename, original_name FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not photo or not (UPLOAD_DIR / photo["filename"]).is_file():
        return Response(status_code=404)
    return FileResponse(UPLOAD_DIR / photo["filename"], filename=photo["original_name"])


@app.get("/admin/photo/{photo_id}")
def admin_photo(request: Request, photo_id: int) -> Response:
    if redirect := require_admin(request):
        return redirect
    with database() as db:
        photo = db.execute("SELECT filename FROM photos WHERE id = ?", (photo_id,)).fetchone()
    path = UPLOAD_DIR / photo["filename"] if photo else None
    return FileResponse(path) if path and path.is_file() else Response(status_code=404)


@app.get("/admin/download-approved")
def download_approved(request: Request) -> Response:
    if redirect := require_admin(request):
        return redirect
    buffer = io.BytesIO()
    with database() as db:
        photos = db.execute(
            "SELECT id, filename, original_name FROM photos WHERE approval = 'approved' ORDER BY id"
        ).fetchall()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for photo in photos:
            path = UPLOAD_DIR / photo["filename"]
            if path.is_file():
                archive.write(path, arcname=f"{photo['id']}-{photo['original_name']}")
    return Response(
        content=buffer.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="bitta-craic-approved-photos.zip"'},
    )


@app.get("/gallery", response_class=HTMLResponse)
def gallery(request: Request) -> Response:
    return RedirectResponse("/", status_code=303)
