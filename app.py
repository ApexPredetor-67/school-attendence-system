from datetime import datetime, timedelta
from functools import wraps
import base64
import hashlib
import os
import secrets
import shutil
from pathlib import Path

import cv2
import numpy as np
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from urllib.parse import unquote
from zoneinfo import ZoneInfo
import json
import zipfile
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import func, text, inspect, or_
from sqlalchemy.exc import IntegrityError

from models import db, User, Attendance, Admin, AuditLog, Teacher, TeacherVerificationToken, SchoolCalendar, Notification, AppSetting
from face_utils import get_face_encodings, recognize_faces, best_match_for_encoding, train_folder, image_quality
from notifications import (
    send_email_notification,
    send_telegram_notification,
    send_sms_notification,
    send_teacher_verification_email,
    send_teacher_password_reset_email,
    send_email_absence_notification,
    send_telegram_absence_notification,
    normalize_e164,
)

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)

LOCAL_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Kolkata"))

def now_local():
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)

def today_local():
    return now_local().date()

# Persistent storage:
# - Production: DATABASE_URL should point to Supabase/PostgreSQL.
# - Render fallback: if DATABASE_URL is missing, SQLite is placed on DATA_DIR
#   (normally /var/data), so a Render Persistent Disk can still preserve it.
# - Local: DATA_DIR defaults to the project folder.
DATA_ROOT = Path(os.getenv("DATA_DIR", str(BASE_DIR))).resolve()
FACE_DATA = DATA_ROOT / "face_data"
BACKUP_DIR = DATA_ROOT / "backups"
SQLITE_DB_PATH = DATA_ROOT / "attendance.db"

raw_db_url = os.getenv("DATABASE_URL", "").strip()
if raw_db_url.startswith("postgres://"):
    raw_db_url = "postgresql://" + raw_db_url[len("postgres://"):]

DATABASE_URL = raw_db_url or f"sqlite:///{SQLITE_DB_PATH.as_posix()}"
FACE_DATA.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
    SQLALCHEMY_DATABASE_URI=DATABASE_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={
        "pool_pre_ping": True,
        "pool_recycle": 300,
    },
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)

db.init_app(app)


def get_env_bool(name, default=False):
    return os.getenv(name, str(default)).lower() == "true"


def setting_get(key, default=None):
    row = db.session.get(AppSetting, key)
    return row.value if row is not None else default


def setting_set(key, value):
    row = db.session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    return row


def setting_bool(key, env_name, default=False):
    raw = setting_get(key, None)
    if raw is None:
        return get_env_bool(env_name, default)
    return str(raw).strip().lower() == "true"


def setting_value(key, env_name, default):
    return setting_get(key, os.getenv(env_name, default))


def get_working_days_setting():
    try:
        return 5 if int(setting_value("working_days", "WORKING_DAYS", "6")) == 5 else 6
    except (TypeError, ValueError):
        return 6


def weekly_default_is_working(day):
    return day.weekday() < get_working_days_setting()


def is_working_day(day):
    override = SchoolCalendar.query.filter_by(date=day).first()
    if override is not None:
        return bool(override.is_working)
    return weekly_default_is_working(day)


def working_days_between(start_date, end_date):
    if end_date < start_date:
        return []
    return [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)
            if is_working_day(start_date + timedelta(days=i))]


def current_teacher():
    tid = session.get("teacher_id")
    return db.session.get(Teacher, tid) if tid else None


def is_admin_session():
    return bool(session.get("admin_id"))


def teacher_can_access_student(user):
    if is_admin_session():
        return True
    teacher = current_teacher()
    return bool(teacher and user and user.class_teacher_id == teacher.id)


def pending_registration_owned_by_current_staff(info):
    if not info:
        return False
    if is_admin_session():
        return True
    teacher = current_teacher()
    return bool(teacher and int(info.get("class_teacher_id") or 0) == teacher.id)


def admin_password_matches(password):
    """Check the current admin password from the database."""
    if not password:
        return False
    try:
        return any(check_password_hash(a.password_hash, password) for a in Admin.query.all())
    except Exception:
        return False


def teacher_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        teacher = current_teacher()
        if not teacher or not teacher.active:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Teacher authentication required"}), 401
            return redirect(url_for("teacher_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("admin_id"):
            return view(*args, **kwargs)
        teacher = current_teacher()
        if teacher and teacher.active:
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("teacher_login", next=request.path))
    return wrapped


def create_notification(kind, message, teacher_id=None, user_id=None, dedupe_key=None, provider="system", status="created"):
    if dedupe_key and Notification.query.filter_by(dedupe_key=dedupe_key).first():
        return None
    n = Notification(kind=kind, message=message, teacher_id=teacher_id, user_id=user_id,
                     recipient_type="teacher" if teacher_id else "admin", dedupe_key=dedupe_key,
                     provider=provider, status=status, sent_at=now_local() if status == "sent" else None)
    db.session.add(n)
    return n


def notify_teacher_arrival(user, attendance):
    teacher = user.class_teacher
    if not teacher:
        return None
    message = f"{user.name} ({user.class_name or 'Class'} {user.section or ''}) entered school at {attendance.time_in}."
    return create_notification("arrival", message, teacher_id=teacher.id, user_id=user.id,
                               dedupe_key=f"arrival:{user.id}:{attendance.date}")


def absence_alert_time_reached(force=False):
    if force:
        return True
    raw = str(setting_value("absence_alert_time", "ABSENCE_ALERT_TIME", "10:00"))
    try:
        hour, minute = [int(x) for x in raw.split(":", 1)]
    except Exception:
        hour, minute = 10, 0
    current = now_local().time()
    return (current.hour, current.minute) >= (hour, minute)


def send_absence_alerts(target_date=None, force=False):
    target_date = target_date or now_local().date()
    if not is_working_day(target_date):
        return {"working_day": False, "processed": 0, "sms_sent": 0, "email_sent": 0, "telegram_sent": 0, "skipped_no_consent": 0}
    if not absence_alert_time_reached(force=force):
        return {"working_day": True, "before_alert_time": True, "processed": 0, "sms_sent": 0, "email_sent": 0, "telegram_sent": 0, "skipped_no_consent": 0}
    sync_notification_toggle_env()
    processed = sms_sent = email_sent = telegram_sent = skipped_no_consent = 0
    active_students = User.query.filter_by(active=True).all()
    callback_url = public_url("twilio_sms_status") if os.getenv("APP_BASE_URL") else None
    for user in active_students:
        present = Attendance.query.filter_by(user_id=user.id, date=target_date, archived=False).first()
        if present:
            continue
        teacher = user.class_teacher
        dedupe = f"absence:{user.id}:{target_date}"
        if Notification.query.filter_by(dedupe_key=dedupe).first():
            continue
        msg = f"{user.name} ({user.class_name or 'Class'} {user.section or ''}) has not been marked present on {target_date}."
        channels = []
        sms_ok, sms_msg, sms_sid = send_sms_notification(
            user,
            f"School attendance alert: {user.name} has not been marked present today ({target_date}). Please contact the school if this is unexpected.",
            status_callback_url=callback_url,
        )
        if sms_ok:
            sms_sent += 1; channels.append("SMS")
        elif "not opted in" in sms_msg.lower():
            skipped_no_consent += 1

        email_ok, email_msg, _ = send_email_absence_notification(user, target_date)
        if email_ok:
            email_sent += 1; channels.append("Email")
        elif "not opted in" in email_msg.lower():
            skipped_no_consent += 1

        telegram_ok, telegram_msg, telegram_id = send_telegram_absence_notification(user, target_date)
        if telegram_ok:
            telegram_sent += 1; channels.append("Telegram")
        elif "not opted in" in telegram_msg.lower():
            skipped_no_consent += 1

        provider = ",".join(channels) if channels else "system"
        status = "sent" if channels else "pending"
        note = create_notification("absence", msg, teacher_id=teacher.id if teacher else None, user_id=user.id,
                           dedupe_key=dedupe, provider=provider, status=status)
        if note:
            note.error_message = None if channels else " / ".join(x for x in (sms_msg, email_msg, telegram_msg) if x)[:500]
            note.provider_message_id = sms_sid or telegram_id
            note.provider_status = "accepted" if channels else "not_sent"
        processed += 1
    db.session.commit()
    return {"working_day": True, "processed": processed, "sms_sent": sms_sent, "email_sent": email_sent, "telegram_sent": telegram_sent, "skipped_no_consent": skipped_no_consent}


def audit(action, description, target_type=None, target_id=None):
    actor = session.get("admin_username") or session.get("teacher_username") or "system"
    db.session.add(AuditLog(
        admin_username=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        description=description,
    ))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login", next=request.path))
        if session.get("must_change_password") and request.endpoint != "change_password":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Password change required"}), 403
            return redirect(url_for("change_password"))
        return view(*args, **kwargs)
    return wrapped


def decode_data_url(image_data):
    if not isinstance(image_data, str) or "," not in image_data:
        raise ValueError("Invalid image data")
    raw = base64.b64decode(image_data.split(",", 1)[1], validate=True)
    frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Unable to decode image")
    return frame


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_url(endpoint, **values):
    base = (os.getenv("APP_BASE_URL") or "").strip().rstrip("/")
    path = url_for(endpoint, **values)
    if base:
        return f"{base}{path}"
    return url_for(endpoint, _external=True, **values)


def issue_teacher_verification_token(teacher, hours=24):
    token = secrets.token_urlsafe(32)
    expires_at = now_local() + timedelta(hours=hours)
    db.session.add(TeacherVerificationToken(
        teacher_id=teacher.id,
        token_hash=token_hash(token),
        expires_at=expires_at,
    ))
    teacher.email_verification_token_hash = token_hash(token)
    teacher.email_verification_expires_at = expires_at
    return token


def issue_teacher_reset_token(teacher, minutes=30):
    token = secrets.token_urlsafe(32)
    teacher.password_reset_token_hash = token_hash(token)
    teacher.password_reset_expires_at = now_local() + timedelta(minutes=minutes)
    return token


def migrate_database_schema():
    """Idempotent lightweight migrations for both SQLite and PostgreSQL."""
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    def add_columns(table, additions):
        if table not in tables:
            return
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, definition in additions:
            if name in existing:
                continue
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'))

    add_columns("attendance", [
        ("archived", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("archived_at", "TIMESTAMP"),
        ("archived_by", "VARCHAR(120)"),
        ("time_out", "TIME"),
        ("status", "VARCHAR(20) NOT NULL DEFAULT 'present'"),
    ])
    add_columns("user", [
        ("telegram_chat_id", "VARCHAR(80)"), ("admission_number", "VARCHAR(50)"),
        ("roll_number", "VARCHAR(30)"), ("class_name", "VARCHAR(30)"), ("section", "VARCHAR(10)"),
        ("parent_name", "VARCHAR(120)"), ("parent_phone", "VARCHAR(30)"), ("parent_email", "VARCHAR(160)"),
        ("parent_email_opt_in", "BOOLEAN NOT NULL DEFAULT TRUE"), ("parent_sms_opt_in", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("parent_telegram_opt_in", "BOOLEAN NOT NULL DEFAULT FALSE"), ("notification_preferences_updated_at", "TIMESTAMP"),
        ("class_teacher_id", "INTEGER"), ("active", "BOOLEAN NOT NULL DEFAULT TRUE"),
        ("joined_date", "DATE"), ("face_trained", "BOOLEAN NOT NULL DEFAULT FALSE"), ("training_date", "TIMESTAMP"),
    ])
    add_columns("notification", [
        ("provider_message_id", "VARCHAR(120)"), ("provider_status", "VARCHAR(40)"),
    ])
    add_columns("teacher", [
        ("email_verified", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("email_verification_token_hash", "VARCHAR(128)"),
        ("email_verification_expires_at", "TIMESTAMP"),
        ("password_reset_token_hash", "VARCHAR(128)"),
        ("password_reset_expires_at", "TIMESTAMP"),
    ])
    db.create_all()

    if "user" in tables or "user" in inspect(db.engine).get_table_names():
        try:
            with db.engine.begin() as conn:
                conn.execute(text("UPDATE \"user\" SET parent_email_opt_in = TRUE WHERE parent_email IS NOT NULL AND TRIM(parent_email) <> '' AND parent_email_opt_in IS NULL"))
                conn.execute(text("UPDATE \"user\" SET parent_sms_opt_in = FALSE WHERE parent_sms_opt_in IS NULL"))
                conn.execute(text("UPDATE \"user\" SET parent_telegram_opt_in = FALSE WHERE parent_telegram_opt_in IS NULL"))
        except Exception:
            pass

    if "teacher" in tables or "teacher" in inspect(db.engine).get_table_names():
        try:
            with db.engine.begin() as conn:
                conn.execute(text("UPDATE \"teacher\" SET email_verified = TRUE WHERE email IS NOT NULL AND TRIM(email) <> '' AND COALESCE(email_verified, FALSE) = FALSE"))
        except Exception:
            pass

    try:
        with db.engine.begin() as conn:
            if db.engine.dialect.name == "postgresql":
                conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_attendance_active_user_date ON attendance (user_id, date) WHERE archived = FALSE'))
            elif db.engine.dialect.name == "sqlite":
                conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_attendance_active_user_date ON attendance (user_id, date) WHERE archived = 0'))
    except Exception:
        pass


def ensure_default_admin():
    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD", "ChangeMe123!")
    admin = Admin.query.filter_by(username=username).first()
    if admin is None:
        db.session.add(Admin(username=username, password_hash=generate_password_hash(password), must_change_password=True))
        db.session.commit()


def seed_settings_from_env():
    defaults = {
        "attendance_threshold": os.getenv("ATTENDANCE_THRESHOLD", "75"),
        "working_days": os.getenv("WORKING_DAYS", "6"),
        "face_tolerance": os.getenv("FACE_RECOGNITION_TOLERANCE", "0.48"),
        "min_face_samples": os.getenv("MIN_FACE_SAMPLES", "10"),
        "absence_alert_time": os.getenv("ABSENCE_ALERT_TIME", "10:00"),
        "admin_email": os.getenv("ADMIN_EMAIL", ""),
        "email_notifications": os.getenv("EMAIL_NOTIFICATIONS", "false"),
        "telegram_notifications": os.getenv("TELEGRAM_NOTIFICATIONS", "false"),
        "sms_notifications": os.getenv("SMS_NOTIFICATIONS", "false"),
    }
    changed = False
    for key, value in defaults.items():
        if setting_get(key, None) is None:
            setting_set(key, value)
            changed = True
    if changed:
        db.session.commit()


def sync_notification_toggle_env():
    os.environ["EMAIL_NOTIFICATIONS"] = "true" if setting_bool("email_notifications", "EMAIL_NOTIFICATIONS", False) else "false"
    os.environ["TELEGRAM_NOTIFICATIONS"] = "true" if setting_bool("telegram_notifications", "TELEGRAM_NOTIFICATIONS", False) else "false"
    os.environ["SMS_NOTIFICATIONS"] = "true" if setting_bool("sms_notifications", "SMS_NOTIFICATIONS", False) else "false"


def cleanup_pending_registrations():
    pending_root = FACE_DATA / "_pending"
    pending_root.mkdir(exist_ok=True)
    cutoff = now_local().timestamp() - 24 * 60 * 60
    for folder in pending_root.iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


def pending_info():
    data = session.get("pending_registration")
    if not data or not data.get("token"):
        return None
    return data


@app.before_request
def refresh_session_and_cleanup():
    try:
        seed_settings_from_env()
        sync_notification_toggle_env()
    except Exception:
        db.session.rollback()
    if session.get("admin_id"):
        admin = db.session.get(Admin, session["admin_id"])
        if admin:
            session["must_change_password"] = bool(admin.must_change_password)


@app.context_processor
def inject_nav_counts():
    try:
        teacher = current_teacher()
        if session.get("admin_id"):
            unread = Notification.query.filter_by(read=False).count()
            return {
                "nav_students": User.query.count(),
                "nav_today": Attendance.query.filter_by(date=now_local().date(), archived=False).count(),
                "nav_archived": Attendance.query.filter_by(archived=True).count(),
                "nav_notifications": unread,
            }
        if teacher:
            unread = Notification.query.filter_by(read=False, teacher_id=teacher.id).count()
            return {
                "nav_students": User.query.filter_by(class_teacher_id=teacher.id).count(),
                "nav_today": Attendance.query.join(User).filter(
                    User.class_teacher_id == teacher.id,
                    Attendance.date == now_local().date(),
                    Attendance.archived.is_(False),
                ).count(),
                "nav_notifications": unread,
            }
    except Exception:
        pass
    return {}


@app.route("/")
def index():
    today = now_local().date()
    teacher = current_teacher()

    # Teachers only see their own students and attendance on the home page.
    if teacher and teacher.active and not session.get("admin_id"):
        total_users = User.query.filter_by(
            class_teacher_id=teacher.id,
            active=True,
        ).count()

        today_attendance = (
            Attendance.query
            .join(User)
            .filter(
                User.class_teacher_id == teacher.id,
                User.active.is_(True),
                Attendance.date == today,
                Attendance.archived.is_(False),
            )
            .count()
        )
    else:
        total_users = User.query.filter_by(active=True).count()
        today_attendance = Attendance.query.filter_by(
            date=today,
            archived=False,
        ).count()

    return render_template(
        "index.html",
        total_users=total_users,
        today_attendance=today_attendance,
        logged_in=bool(session.get("admin_id") or session.get("teacher_id")),
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_id"):
        return redirect(url_for("admin_home"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        admin = Admin.query.filter_by(username=username).first()
        if not admin or not check_password_hash(admin.password_hash, password):
            return render_template("login.html", error="Invalid username or password"), 401
        session.clear()
        session.permanent = True
        session["admin_id"] = admin.id
        session["admin_username"] = admin.username
        session["must_change_password"] = bool(admin.must_change_password)
        admin.last_login = now_local()
        audit("login", f"Admin {admin.username} logged in")
        db.session.commit()
        if admin.must_change_password:
            return redirect(url_for("change_password"))
        return redirect(request.args.get("next") or url_for("admin_home"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    username = session.get("admin_username")
    if username:
        audit("logout", f"Admin {username} logged out")
        db.session.commit()
    session.clear()
    return redirect(url_for("index"))


@app.route("/teacher/login", methods=["GET", "POST"])
def teacher_login():
    """Authenticate a teacher by username/email or admin override password."""

    # If a teacher is already authenticated, do not render the login
    # form again. Send them to the teacher dashboard.
    if session.get("teacher_id"):
        teacher = current_teacher()
        if teacher and teacher.active:
            return redirect(url_for("dashboard"))

    if session.get("admin_id"):
        return redirect(url_for("admin_home"))

    next_url = (
        request.form.get("next")
        if request.method == "POST"
        else request.args.get("next")
    )
    if next_url and not next_url.startswith("/"):
        next_url = None

    if request.method == "POST":
        identifier = str(request.form.get("username") or "").strip()
        password = str(request.form.get("password") or "")

        if not identifier:
            return render_template(
                "teacher_login.html",
                error="Enter your teacher username or email address.",
                username=identifier,
                next=next_url,
            ), 400

        if not password:
            return render_template(
                "teacher_login.html",
                error="Enter your teacher password.",
                username=identifier,
                next=next_url,
            ), 400

        teacher = Teacher.query.filter(
            or_(
                func.lower(Teacher.username) == identifier.lower(),
                func.lower(Teacher.email) == identifier.lower(),
            )
        ).first()

        if not teacher:
            return render_template(
                "teacher_login.html",
                error="No teacher account was found for that username or email address.",
                username=identifier,
                next=next_url,
            ), 401

        if not teacher.active:
            return render_template(
                "teacher_login.html",
                error="This teacher account is inactive. Please contact the administrator.",
                username=identifier,
                next=next_url,
            ), 403

        teacher_password_ok = False
        try:
            if teacher.password_hash:
                teacher_password_ok = check_password_hash(
                    teacher.password_hash,
                    password,
                )
        except Exception:
            teacher_password_ok = False

        admin_override = admin_password_matches(password)

        if not teacher_password_ok and not admin_override:
            return render_template(
                "teacher_login.html",
                error="Incorrect teacher password.",
                username=identifier,
                next=next_url,
            ), 401

        if (
            teacher.email
            and not teacher.email_verified
            and not admin_override
        ):
            return render_template(
                "teacher_login.html",
                error=(
                    "Your teacher email has not been verified yet. "
                    "Check your inbox or contact the administrator."
                ),
                username=identifier,
                verification_required=True,
                next=next_url,
            ), 403

        session.clear()
        session.permanent = True
        session["teacher_id"] = teacher.id
        session["teacher_username"] = teacher.username
        session["teacher_admin_override"] = bool(admin_override)

        teacher.last_login = now_local()

        if admin_override:
            audit(
                "teacher_login_admin_override",
                f"Administrator override used to sign in as teacher {teacher.username}",
                "Teacher",
                teacher.id,
            )
        else:
            audit(
                "teacher_login",
                f"Teacher {teacher.username} logged in",
                "Teacher",
                teacher.id,
            )

        db.session.commit()

        return redirect(next_url or url_for("dashboard"))

    return render_template(
        "teacher_login.html",
        next=next_url,
    )

@app.route("/teacher/verify/<token>")
def teacher_verify(token):
    token = unquote(str(token)).strip()
    hashed = token_hash(token)

    verification = TeacherVerificationToken.query.filter_by(token_hash=hashed).first()

    if verification:
        teacher = db.session.get(Teacher, verification.teacher_id)
        if not teacher or not teacher.active:
            return render_template("teacher_verify.html", success=False, error="This teacher account is no longer active."), 400
        if verification.used_at is not None:
            return render_template("teacher_verify.html", success=True, already_verified=True, teacher=teacher, info="This verification link has already been used. Your teacher account is already verified."), 200
        if verification.expires_at < now_local():
            return render_template("teacher_verify.html", success=False, error="This verification link has expired. Please request a new verification email."), 400

        teacher.email_verified = True
        verification.used_at = now_local()
        if teacher.email_verification_token_hash == hashed:
            teacher.email_verification_token_hash = None
            teacher.email_verification_expires_at = None
        db.session.commit()
        return render_template("teacher_verify.html", success=True, teacher=teacher)

    teacher = Teacher.query.filter_by(email_verification_token_hash=hashed).first()
    if teacher:
        if teacher.email_verified:
            return render_template("teacher_verify.html", success=True, already_verified=True, teacher=teacher, info="This teacher account is already verified."), 200
        if teacher.email_verification_expires_at and teacher.email_verification_expires_at < now_local():
            return render_template("teacher_verify.html", success=False, error="This verification link has expired. Please request a new verification email."), 400
        teacher.email_verified = True
        teacher.email_verification_token_hash = None
        teacher.email_verification_expires_at = None
        db.session.commit()
        return render_template("teacher_verify.html", success=True, teacher=teacher)

    return render_template("teacher_verify.html", success=False, resend=True, error="This verification link is invalid, expired, or belongs to an older verification email. Request a new verification link below."), 400


@app.route("/teacher/resend-verification", methods=["GET", "POST"])
def resend_teacher_verification():
    if request.method == "POST":
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            identifier = str(payload.get("identifier") or "").strip()
        else:
            identifier = str(request.form.get("identifier") or "").strip()
    else:
        identifier = str(request.args.get("identifier") or "").strip()

    if not identifier:
        if request.method == "POST":
            return render_template("teacher_verify.html", success=False, resend=True, error="Enter your teacher username or email address."), 400
        return render_template("teacher_verify.html", success=False, resend=True)

    teacher = Teacher.query.filter(
        (func.lower(Teacher.username) == identifier.lower()) |
        (func.lower(Teacher.email) == identifier.lower())
    ).first()

    if not teacher or not teacher.active or not teacher.email:
        return render_template("teacher_verify.html", success=True, info="If an active teacher account matches that information, a verification email has been sent.")

    if teacher.email_verified:
        return render_template("teacher_verify.html", success=True, info="This teacher account is already verified. You can sign in.", already_verified=True)

    token = issue_teacher_verification_token(teacher)
    verification_url = public_url("teacher_verify", token=token)
    ok, message, _ = send_teacher_verification_email(teacher, verification_url)
    db.session.commit()

    if ok:
        return render_template("teacher_verify.html", success=True, info="A new verification email has been sent. Check the teacher's inbox.")

    return render_template("teacher_verify.html", success=True, info="Email could not be sent because the server email account is not configured. For this local installation, use the verification link below.", local_link=verification_url, email_error=message)


@app.route("/teacher/forgot-password", methods=["GET", "POST"])
def teacher_forgot_password():
    if request.method == "GET":
        return render_template("teacher_reset_request.html")

    identifier = str((request.form.get("identifier") or "")).strip()
    if not identifier:
        return render_template("teacher_reset_request.html", error="Enter your teacher username or email address."), 400

    teacher = Teacher.query.filter(
        (func.lower(Teacher.username) == identifier.lower()) |
        (func.lower(Teacher.email) == identifier.lower())
    ).first()

    if not teacher or not teacher.active or not teacher.email:
        return render_template("teacher_reset_request.html", sent=True)

    if not teacher.email_verified:
        token = issue_teacher_verification_token(teacher)
        verification_url = public_url("teacher_verify", token=token)
        ok, message, _ = send_teacher_verification_email(teacher, verification_url)
        db.session.commit()
        if ok:
            return render_template("teacher_reset_request.html", sent=False, verification_required=True, info="Your email is not verified yet. We sent a new verification email first.")
        return render_template("teacher_reset_request.html", sent=False, verification_required=True, local_link=verification_url, email_error=message)

    token = issue_teacher_reset_token(teacher)
    reset_url = public_url("teacher_reset_password", token=token)
    ok, message, _ = send_teacher_password_reset_email(teacher, reset_url)
    db.session.commit()
    if ok:
        return render_template("teacher_reset_request.html", sent=True)
    return render_template("teacher_reset_request.html", sent=False, local_link=reset_url, email_error=message)


@app.route("/teacher/reset-password/<token>", methods=["GET", "POST"])
def teacher_reset_password(token):
    teacher = Teacher.query.filter_by(password_reset_token_hash=token_hash(token)).first()
    if not teacher or not teacher.active:
        return render_template("teacher_reset.html", valid=False, error="This password reset link is invalid or has already been used."), 400
    if not teacher.password_reset_expires_at or teacher.password_reset_expires_at < now_local():
        return render_template("teacher_reset.html", valid=False, error="This password reset link has expired. Request a new one."), 400

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if len(new_password) < 8:
            return render_template("teacher_reset.html", valid=True, teacher=teacher, error="Password must be at least 8 characters."), 400
        if new_password != confirm:
            return render_template("teacher_reset.html", valid=True, teacher=teacher, error="Passwords do not match."), 400
        teacher.password_hash = generate_password_hash(new_password)
        teacher.password_reset_token_hash = None
        teacher.password_reset_expires_at = None
        db.session.commit()
        return render_template("teacher_reset.html", valid=True, success=True, teacher=teacher)

    return render_template("teacher_reset.html", valid=True, teacher=teacher)


@app.route("/teacher/logout")
def teacher_logout():
    session.clear()
    return redirect(url_for("teacher_login"))


@app.route("/calendar")
@staff_required
def calendar_page():
    return render_template("calendar.html", teacher=current_teacher())


@app.route("/teacher/calendar")
@teacher_required
def teacher_calendar():
    return render_template("calendar.html", teacher=current_teacher())


@app.route("/api/school-day")
def school_day_api():
    today = now_local().date()
    override = SchoolCalendar.query.filter_by(date=today).first()
    return jsonify({
        "date": today.isoformat(),
        "is_working": is_working_day(today),
        "reason": override.reason if override else None,
        "override": bool(override),
    })


@app.route("/api/calendar")
@staff_required
def calendar_api():
    year = request.args.get("year", now_local().year, type=int)
    month = request.args.get("month", now_local().month, type=int)
    if not 1 <= month <= 12 or not 1900 <= year <= 2200:
        return jsonify({"error": "Invalid calendar month"}), 400
    start = datetime(year, month, 1).date()
    end = (datetime(year + 1, 1, 1).date() if month == 12 else datetime(year, month + 1, 1).date()) - timedelta(days=1)
    rows = []
    day = start
    while day <= end:
        override = SchoolCalendar.query.filter_by(date=day).first()
        rows.append({"date": day.isoformat(), "is_working": is_working_day(day), "reason": override.reason if override else "", "override": bool(override)})
        day += timedelta(days=1)
    return jsonify(rows)


@app.route("/api/calendar", methods=["POST"])
@admin_required
def calendar_set():
    data = request.get_json() or {}
    raw = data.get("date")
    try:
        day = datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return jsonify({"error": "Valid date is required"}), 400
    row = SchoolCalendar.query.filter_by(date=day).first()
    if row is None:
        row = SchoolCalendar(date=day)
        db.session.add(row)
    row.is_working = bool(data.get("is_working"))
    row.reason = (data.get("reason") or "").strip()[:255]
    row.created_by = session.get("admin_username", "admin")
    audit("calendar_update", f"Set {day} as {'working' if row.is_working else 'non-working'}: {row.reason}", "SchoolCalendar", row.id)
    db.session.commit()
    return jsonify({"message": "Calendar updated"})


@app.route("/api/calendar/reset", methods=["POST"])
@admin_required
def calendar_reset():
    data = request.get_json(silent=True) or {}
    raw = data.get("date")
    try:
        day = datetime.strptime(str(raw), "%Y-%m-%d").date()
    except Exception:
        return jsonify({"error": "Valid date is required"}), 400
    row = SchoolCalendar.query.filter_by(date=day).first()
    if row:
        row_id = row.id
        db.session.delete(row)
        audit("calendar_reset", f"Reset calendar override for {day}", "SchoolCalendar", row_id)
        db.session.commit()
    return jsonify({"message": "Calendar reset to weekly default", "date": day.isoformat(), "is_working": weekly_default_is_working(day)})


@app.route("/api/calendar/bulk", methods=["POST"])
@admin_required
def calendar_bulk():
    data = request.get_json(silent=True) or {}
    raw_dates = data.get("dates") or []
    if not isinstance(raw_dates, list) or not raw_dates or len(raw_dates) > 366:
        return jsonify({"error": "Provide between 1 and 366 dates"}), 400
    is_working = bool(data.get("is_working"))
    reason = str(data.get("reason") or "").strip()[:255]
    changed = 0
    try:
        for raw in raw_dates:
            day = datetime.strptime(str(raw), "%Y-%m-%d").date()
            row = SchoolCalendar.query.filter_by(date=day).first()
            if row is None:
                row = SchoolCalendar(date=day)
                db.session.add(row)
            row.is_working = is_working
            row.reason = reason or None
            row.created_by = session.get("admin_username", "admin")
            changed += 1
        audit("calendar_bulk_update", f"Set {changed} calendar date(s) as {'working' if is_working else 'non-working'}: {reason}")
        db.session.commit()
    except ValueError:
        db.session.rollback()
        return jsonify({"error": "Every date must use YYYY-MM-DD"}), 400
    return jsonify({"message": f"Updated {changed} calendar date(s)"})


@app.route("/api/teachers")
@admin_required
def teachers_api():
    return jsonify([
        {
            "id": t.id,
            "name": t.name,
            "email": t.email or "",
            "phone": t.phone or "",
            "username": t.username,
            "active": t.active,
            "email_verified": bool(t.email_verified),
        }
        for t in Teacher.query.order_by(Teacher.name).all()
    ])


@app.route("/api/teachers", methods=["POST"])
@admin_required
def teacher_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    email = str(data.get("email") or "").strip().lower() or None
    phone = str(data.get("phone") or "").strip() or None
    auto_verify = bool(data.get("auto_verify", False))

    if not name or not username or len(password) < 8:
        return jsonify({"error": "Name, username and an 8+ character password are required"}), 400
    if Teacher.query.filter(func.lower(Teacher.username) == username.lower()).first():
        return jsonify({"error": "Teacher username already exists"}), 409
    if email and Teacher.query.filter(func.lower(Teacher.email) == email.lower()).first():
        return jsonify({"error": "A teacher account already uses this email address"}), 409

    email_verified = auto_verify or not email
    t = Teacher(
        name=name,
        username=username,
        email=email,
        phone=phone,
        password_hash=generate_password_hash(password),
        email_verified=email_verified,
    )
    db.session.add(t)
    db.session.flush()

    email_ok, email_msg, verification_url = False, None, None
    if email and not email_verified:
        token = issue_teacher_verification_token(t)
        verification_url = public_url("teacher_verify", token=token)
        email_ok, email_msg, _ = send_teacher_verification_email(t, verification_url)

    audit("teacher_create", f"Created teacher account {t.name}{'; email verification required' if email and not email_verified else ''}", "Teacher", t.id)
    db.session.commit()

    return jsonify({
        "message": "Teacher account created. The teacher can sign in now." if email_verified else "Teacher account created. Email verification is required before the teacher can sign in.",
        "id": t.id,
        "email_sent": email_ok,
        "email_message": email_msg,
        "verification_link": verification_url if not email_ok and verification_url else None,
    }), 201


@app.route("/api/teachers/<int:teacher_id>/resend-verification", methods=["POST"])
@admin_required
def teacher_resend_verification(teacher_id):
    teacher = db.session.get(Teacher, teacher_id)
    if not teacher:
        return jsonify({"error": "Teacher not found"}), 404
    if not teacher.email:
        return jsonify({"error": "Teacher has no email address"}), 400
    if teacher.email_verified:
        return jsonify({"message": "Teacher email is already verified"}), 200

    token = issue_teacher_verification_token(teacher)
    verification_url = public_url("teacher_verify", token=token)
    email_ok, email_msg, _ = send_teacher_verification_email(teacher, verification_url)
    audit("teacher_verification_resend", f"Resent verification email for {teacher.name}", "Teacher", teacher.id)
    db.session.commit()
    return jsonify({
        "message": "Verification email sent" if email_ok else "Email could not be sent; use the local verification link",
        "email_sent": email_ok,
        "email_message": email_msg,
        "verification_link": verification_url if not email_ok else None,
    })


@app.route("/api/teachers/<int:teacher_id>/reset-password", methods=["POST"])
@admin_required
def teacher_admin_reset_password(teacher_id):
    teacher = db.session.get(Teacher, teacher_id)
    if not teacher:
        return jsonify({"error": "Teacher not found"}), 404
    if not teacher.email:
        return jsonify({"error": "Teacher has no email address. Add an email before resetting the password."}), 400
    if not teacher.email_verified:
        return jsonify({"error": "Teacher email is not verified. Verify the account first."}), 400

    token = issue_teacher_reset_token(teacher)
    reset_url = public_url("teacher_reset_password", token=token)
    email_ok, email_msg, _ = send_teacher_password_reset_email(teacher, reset_url)
    audit("teacher_password_reset", f"Issued password reset for {teacher.name}", "Teacher", teacher.id)
    db.session.commit()
    return jsonify({
        "message": "Password reset email sent" if email_ok else "Email could not be sent; use the local password-reset link",
        "email_sent": email_ok,
        "email_message": email_msg,
        "reset_link": reset_url if not email_ok else None,
    })


@app.route("/api/teachers/<int:teacher_id>/set-password", methods=["POST"])
@admin_required
def teacher_admin_set_password(teacher_id):
    teacher = db.session.get(Teacher, teacher_id)
    if not teacher:
        return jsonify({"error": "Teacher not found"}), 404
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    teacher.password_hash = generate_password_hash(password)
    audit("teacher_password_set", f"Admin set a new password for {teacher.name}", "Teacher", teacher.id)
    db.session.commit()
    return jsonify({"message": "Teacher password updated. The teacher can sign in now."})


@app.route("/api/teachers/<int:teacher_id>/verify", methods=["POST"])
@admin_required
def teacher_admin_verify(teacher_id):
    teacher = db.session.get(Teacher, teacher_id)
    if not teacher:
        return jsonify({"error": "Teacher not found"}), 404
    if teacher.email_verified:
        return jsonify({"message": "Teacher email is already verified"})
    teacher.email_verified = True
    teacher.email_verification_token_hash = None
    teacher.email_verification_expires_at = None
    audit("teacher_verify_admin", f"Admin manually verified {teacher.name}", "Teacher", teacher.id)
    db.session.commit()
    return jsonify({"message": "Teacher email verified. The teacher can sign in now."})


@app.route("/api/teachers/<int:teacher_id>", methods=["PUT"])
@admin_required
def teacher_update(teacher_id):
    teacher = db.session.get(Teacher, teacher_id)
    if not teacher:
        return jsonify({"error": "Teacher not found"}), 404
    data = request.get_json(silent=True) or {}
    for field in ["name", "phone"]:
        if field in data:
            setattr(teacher, field, (data.get(field) or "").strip() or None)
    if "email" in data:
        new_email = str(data.get("email") or "").strip().lower() or None
        if new_email and Teacher.query.filter(func.lower(Teacher.email) == new_email.lower(), Teacher.id != teacher.id).first():
            return jsonify({"error": "Another teacher already uses this email"}), 409
        teacher.email = new_email
    if "active" in data:
        teacher.active = bool(data["active"])
    audit("teacher_update", f"Updated teacher {teacher.name}", "Teacher", teacher.id)
    db.session.commit()
    return jsonify({"message": "Teacher updated"})


@app.route("/api/teachers/<int:teacher_id>", methods=["DELETE"])
@admin_required
def teacher_delete(teacher_id):
    t = db.session.get(Teacher, teacher_id)
    if not t:
        return jsonify({"error": "Teacher not found"}), 404
    User.query.filter_by(class_teacher_id=t.id).update({"class_teacher_id": None}, synchronize_session=False)
    Notification.query.filter_by(teacher_id=t.id).update({"teacher_id": None, "recipient_type": "admin"}, synchronize_session=False)
    audit("teacher_delete", f"Disabled/deleted teacher account {t.name}", "Teacher", t.id)
    db.session.delete(t)
    db.session.commit()
    return jsonify({"message": "Teacher account deleted; student records were retained and notifications were kept"})


@app.route("/teacher/notifications")
@staff_required
def teacher_notifications():
    return render_template("notifications.html", teacher=current_teacher())


@app.route("/api/notifications")
@staff_required
def notifications_api():
    teacher = current_teacher()
    q = Notification.query.order_by(Notification.created_at.desc())
    if teacher:
        q = q.filter_by(teacher_id=teacher.id)
    rows = q.limit(100).all()
    return jsonify([{"id": n.id, "kind": n.kind, "message": n.message, "created_at": n.created_at.isoformat(sep=" ", timespec="minutes"), "status": n.status, "read": n.read} for n in rows])


@app.route("/api/notifications/<int:notification_id>/read", methods=["POST"])
@staff_required
def notification_read(notification_id):
    n = db.session.get(Notification, notification_id)
    if not n:
        return jsonify({"error": "Notification not found"}), 404
    teacher = current_teacher()
    if teacher and n.teacher_id != teacher.id:
        return jsonify({"error": "Not allowed"}), 403
    n.read = True
    db.session.commit()
    return jsonify({"message": "Marked as read"})


@app.route("/api/admin/send-absence-alerts", methods=["POST"])
@admin_required
def admin_send_absence_alerts():
    result = send_absence_alerts(now_local().date(), force=True)
    audit("absence_alerts", f"Processed absence alerts: {result}")
    db.session.commit()
    return jsonify(result)


@app.route("/api/teacher/send-absence-alerts", methods=["POST"])
@teacher_required
def teacher_send_absence_alerts():
    teacher = current_teacher()
    day = now_local().date()
    if not is_working_day(day):
        return jsonify({"working_day": False, "processed": 0, "sms_sent": 0, "email_sent": 0, "telegram_sent": 0})
    # Manual teacher action is intentionally allowed to run immediately for this teacher's class.
    sync_notification_toggle_env()
    processed = sms_sent = email_sent = telegram_sent = skipped_no_consent = 0
    for user in User.query.filter_by(active=True, class_teacher_id=teacher.id).all():
        if Attendance.query.filter_by(user_id=user.id, date=day, archived=False).first():
            continue
        dedupe = f"absence:{user.id}:{day}"
        sms_ok, sms_msg, sms_sid = send_sms_notification(user, f"School attendance alert: {user.name} has not been marked present today ({day}).", status_callback_url=public_url("twilio_sms_status") if os.getenv("APP_BASE_URL") else None)
        email_ok, email_msg, _ = send_email_absence_notification(user, day)
        telegram_ok, telegram_msg, telegram_id = send_telegram_absence_notification(user, day)
        channels=[]
        if sms_ok: sms_sent += 1; channels.append("SMS")
        elif "not opted in" in sms_msg.lower(): skipped_no_consent += 1
        if email_ok: email_sent += 1; channels.append("Email")
        elif "not opted in" in email_msg.lower(): skipped_no_consent += 1
        if telegram_ok: telegram_sent += 1; channels.append("Telegram")
        elif "not opted in" in telegram_msg.lower(): skipped_no_consent += 1
        note=create_notification("absence", f"{user.name} has not been marked present today.", teacher_id=teacher.id, user_id=user.id, dedupe_key=dedupe, provider=",".join(channels) if channels else "system", status="sent" if channels else "pending")
        if note:
            note.error_message = None if channels else " / ".join([x for x in (sms_msg,email_msg,telegram_msg) if x])[:500]
            note.provider_message_id = sms_sid or telegram_id
            note.provider_status = "accepted" if channels else "not_sent"
        processed += 1
    db.session.commit()
    return jsonify({"working_day": True, "processed": processed, "sms_sent": sms_sent, "email_sent": email_sent, "telegram_sent": telegram_sent, "skipped_no_consent": skipped_no_consent})


@app.route("/api/webhooks/twilio/sms-inbound", methods=["POST"])
def twilio_sms_inbound():
    from twilio.request_validator import RequestValidator
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if token:
        signature = request.headers.get("X-Twilio-Signature", "")
        if not RequestValidator(token).validate(request.url, request.form.to_dict(flat=True), signature):
            return jsonify({"error": "Invalid Twilio signature"}), 403
    body = (request.form.get("Body") or "").strip().upper()
    sender = normalize_e164(request.form.get("From") or "")
    if not sender:
        return "<Response></Response>", 200, {"Content-Type": "application/xml"}
    if body in {"STOP", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT"}:
        users = User.query.filter(User.parent_phone.isnot(None)).all()
        changed = 0
        for user in users:
            if normalize_e164(user.parent_phone) == sender and user.parent_sms_opt_in:
                user.parent_sms_opt_in = False
                user.notification_preferences_updated_at = now_local()
                changed += 1
        if changed:
            db.session.commit()
    elif body in {"START", "UNSTOP", "SUBSCRIBE", "OPTIN"}:
        users = User.query.filter(User.parent_phone.isnot(None)).all()
        changed = 0
        for user in users:
            if normalize_e164(user.parent_phone) == sender and not user.parent_sms_opt_in:
                user.parent_sms_opt_in = True
                user.notification_preferences_updated_at = now_local()
                changed += 1
        if changed:
            db.session.commit()
    return "<Response></Response>", 200, {"Content-Type": "application/xml"}


@app.route("/admin")
@admin_required
def admin_home():
    today = now_local().date()
    return render_template("admin.html", total_users=User.query.filter_by(active=True).count(),
                           today_attendance=Attendance.query.filter_by(date=today, archived=False).count(),
                           archived_count=Attendance.query.filter_by(archived=True).count(),
                           working_today=is_working_day(today),
                           unread_notifications=Notification.query.filter_by(read=False).count())


@app.route("/teachers")
@admin_required
def teachers_page():
    return render_template("teachers.html")


@app.route("/students")
@staff_required
def students_page():
    return render_template("students.html", teacher=current_teacher())


@app.route("/api/students")
@staff_required
def students_api():
    teacher = current_teacher()
    query = User.query.order_by(User.name)
    if teacher:
        query = query.filter(User.class_teacher_id == teacher.id)
    users = query.all()
    today = now_local().date()
    return jsonify([{
        "id": u.id, "name": u.name, "email": u.email, "phone": u.phone or "",
        "parent_name": u.parent_name or "", "parent_phone": u.parent_phone or "",
        "parent_email": u.parent_email or "", "parent_email_opt_in": bool(u.parent_email_opt_in),
        "parent_sms_opt_in": bool(u.parent_sms_opt_in), "parent_telegram_opt_in": bool(u.parent_telegram_opt_in),
        "admission_number": u.admission_number or "",
        "roll_number": u.roll_number or "", "class_name": u.class_name or "", "section": u.section or "",
        "class_teacher_id": u.class_teacher_id, "class_teacher": u.class_teacher.name if u.class_teacher else "",
        "active": bool(u.active), "face_trained": bool(u.face_trained),
        "training_date": u.training_date.isoformat(sep=" ", timespec="minutes") if u.training_date else None,
        "today_present": Attendance.query.filter_by(user_id=u.id, date=today, archived=False).first() is not None,
    } for u in users])


@app.route("/api/students/<int:user_id>", methods=["DELETE"])
@staff_required
def delete_student(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "Student not found"}), 404
    if not is_admin_session():
        return jsonify({"error": "Only an administrator can permanently delete student accounts."}), 403
    name = user.name
    audit("student_delete", f"Permanently deleted student {name} and all attendance records", "User", user.id)
    db.session.delete(user)
    db.session.commit()
    shutil.rmtree(FACE_DATA / str(user_id), ignore_errors=True)
    try:
        (FACE_DATA / f"{user_id}_encoding.npy").unlink(missing_ok=True)
    except OSError:
        pass
    return jsonify({"message": f"Student {name} and all related attendance records were permanently deleted"})


@app.route("/api/students/<int:user_id>", methods=["PUT"])
@staff_required
def update_student(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "Student not found"}), 404
    if not teacher_can_access_student(user):
        return jsonify({"error": "You can only manage students assigned to you."}), 403

    data = request.get_json() or {}
    editable_fields = [
        "name", "email", "phone", "admission_number", "roll_number",
        "class_name", "section", "parent_name", "parent_phone", "parent_email",
    ]
    for field in editable_fields:
        if field in data:
            if field in {"parent_email_opt_in", "parent_sms_opt_in", "parent_telegram_opt_in"}:
                setattr(user, field, bool(data.get(field)))
                user.notification_preferences_updated_at = now_local()
                continue
            value = (data.get(field) or "").strip() or None
            if field == "email" and value:
                duplicate = User.query.filter(
                    func.lower(User.email) == value.lower(),
                    User.id != user.id,
                ).first()
                if duplicate:
                    return jsonify({"error": "Another student already uses this email."}), 409
            if field == "admission_number" and value:
                duplicate = User.query.filter(
                    User.admission_number == value,
                    User.id != user.id,
                ).first()
                if duplicate:
                    return jsonify({"error": "Another student already uses this admission number."}), 409
            setattr(user, field, value)

    if is_admin_session() and "class_teacher_id" in data:
        tid = data.get("class_teacher_id")
        teacher = db.session.get(Teacher, int(tid)) if tid else None
        if tid and (not teacher or not teacher.active):
            return jsonify({"error": "Invalid class teacher"}), 400
        user.class_teacher_id = teacher.id if teacher else None

    if is_admin_session() and "active" in data:
        user.active = bool(data["active"])

    audit("student_update", f"Updated student {user.name}", "User", user.id)
    db.session.commit()
    return jsonify({"message": "Student updated"})


@app.route("/change-password", methods=["GET", "POST"])
@admin_required
def change_password():
    admin = db.session.get(Admin, session["admin_id"])
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not check_password_hash(admin.password_hash, current):
            return render_template("change_password.html", error="Current password is incorrect"), 400
        if len(new) < 8 or new == current:
            return render_template("change_password.html", error="New password must be at least 8 characters and different from the current password"), 400
        if new != confirm:
            return render_template("change_password.html", error="New passwords do not match"), 400
        admin.password_hash = generate_password_hash(new)
        admin.must_change_password = False
        session["must_change_password"] = False
        audit("password_change", f"Admin {admin.username} changed their password", "Admin", admin.id)
        db.session.commit()
        return redirect(url_for("admin_home"))
    return render_template("change_password.html")


@app.route("/register", methods=["GET", "POST"])
@staff_required
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip() or None
        telegram_chat_id = request.form.get("telegram_chat_id", "").strip() or None
        admission_number = request.form.get("admission_number", "").strip() or None
        roll_number = request.form.get("roll_number", "").strip() or None
        class_name = request.form.get("class_name", "").strip() or None
        section = request.form.get("section", "").strip() or None
        parent_name = request.form.get("parent_name", "").strip() or None
        parent_phone = request.form.get("parent_phone", "").strip() or None
        parent_email = request.form.get("parent_email", "").strip() or None
        parent_email_opt_in = bool(request.form.get("parent_email_opt_in"))
        parent_sms_opt_in = bool(request.form.get("parent_sms_opt_in"))
        parent_telegram_opt_in = bool(request.form.get("parent_telegram_opt_in"))
        class_teacher_id = request.form.get("class_teacher_id", type=int)
        current = current_teacher()
        if current:
            class_teacher_id = current.id
        if not name or not email or not class_name or not section or not parent_phone or not class_teacher_id:
            return jsonify({"error": "Name, email, class, section, class teacher and parent phone are required"}), 400
        if User.query.filter_by(email=email).first():
            return jsonify({"error": "A student with this email is already registered"}), 400
        if admission_number and User.query.filter_by(admission_number=admission_number).first():
            return jsonify({"error": "Admission number is already registered"}), 400
        teacher = db.session.get(Teacher, class_teacher_id)
        if not teacher or not teacher.active:
            return jsonify({"error": "Select a valid active class teacher"}), 400
        token = secrets.token_urlsafe(24)
        pending_root = FACE_DATA / "_pending" / token
        pending_root.mkdir(parents=True, exist_ok=False)
        session["pending_registration"] = {
            "token": token,
            "name": name,
            "email": email,
            "phone": phone, "telegram_chat_id": telegram_chat_id,
            "admission_number": admission_number, "roll_number": roll_number,
            "class_name": class_name, "section": section, "parent_name": parent_name,
            "parent_phone": parent_phone, "parent_email": parent_email,
            "parent_email_opt_in": parent_email_opt_in, "parent_sms_opt_in": parent_sms_opt_in, "parent_telegram_opt_in": parent_telegram_opt_in,
            "class_teacher_id": class_teacher_id,
        }
        return jsonify({"message": "Student details saved temporarily. Face capture is required to complete registration.", "registration_token": token}), 201
    return render_template(
        "register.html",
        pending=pending_info(),
        teacher=current_teacher(),
        teachers=Teacher.query.filter_by(active=True).order_by(Teacher.name).all() if is_admin_session() else [],
    )


@app.route("/api/register/cancel", methods=["POST"])
@staff_required
def cancel_registration():
    info = pending_info()
    if info:
        shutil.rmtree(FACE_DATA / "_pending" / info["token"], ignore_errors=True)
    session.pop("pending_registration", None)
    return jsonify({"message": "Registration cancelled; no student was created"})


@app.route("/api/capture-frame/<token>", methods=["POST"])
@staff_required
def capture_frame(token):
    info = pending_info()
    if not info or not secrets.compare_digest(str(info.get("token", "")), str(token)) or not pending_registration_owned_by_current_staff(info):
        return jsonify({"error": "Registration session expired or is not assigned to this account."}), 403
    try:
        frame = decode_data_url((request.get_json() or {}).get("image"))
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        from face_utils import face_rec_available
        if not face_rec_available():
            return jsonify({"error": "Face recognition is not installed on this server. Deploy on Render with Python 3.12 to enable face features."}), 503
        from face_utils import _face_locations
        locations = _face_locations(rgb, upsample=1)
        if len(locations) != 1:
            return jsonify({"error": "Exactly one face must be visible. No face, multiple faces, or partial faces are not accepted."}), 400
        quality_ok, quality_message = image_quality(frame, locations[0])
        if not quality_ok:
            return jsonify({"error": quality_message}), 400
        folder = FACE_DATA / "_pending" / token
        folder.mkdir(parents=True, exist_ok=True)
        count = len(list(folder.glob("frame_*.jpg")))
        if count >= 12:
            return jsonify({"error": "All 12 capture slots are already filled"}), 400
        cv2.imwrite(str(folder / f"frame_{count}.jpg"), frame)
        return jsonify({"message": "Good face frame captured", "frame_count": count + 1}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/register/complete/<token>", methods=["POST"])
@staff_required
def complete_registration(token):
    info = pending_info()
    if not info or not secrets.compare_digest(str(info.get("token", "")), str(token)) or not pending_registration_owned_by_current_staff(info):
        return jsonify({"error": "Registration session expired or is not assigned to this account."}), 403
    folder = FACE_DATA / "_pending" / token
    frames = list(folder.glob("frame_*.jpg"))
    minimum = int(setting_value("min_face_samples", "MIN_FACE_SAMPLES", "10"))
    if len(frames) < minimum:
        return jsonify({"error": f"Face capture is required. Capture at least {minimum} valid frames before completing registration."}), 400
    try:
        encodings = get_face_encodings(str(folder))
        if len(encodings) < minimum:
            return jsonify({"error": f"Only {len(encodings)} high-quality face frames could be trained. Retake the rejected frames and try again."}), 400
        if User.query.filter_by(email=info["email"]).first():
            return jsonify({"error": "A student with this email was registered while this form was open."}), 409
        user = User(name=info["name"], email=info["email"], phone=info.get("phone"), telegram_chat_id=info.get("telegram_chat_id"),
                    admission_number=info.get("admission_number"), roll_number=info.get("roll_number"),
                    class_name=info.get("class_name"), section=info.get("section"), parent_name=info.get("parent_name"),
                    parent_phone=info.get("parent_phone"), parent_email=info.get("parent_email"),
                    parent_email_opt_in=bool(info.get("parent_email_opt_in", True)),
                    parent_sms_opt_in=bool(info.get("parent_sms_opt_in", False)),
                    parent_telegram_opt_in=bool(info.get("parent_telegram_opt_in", False)),
                    notification_preferences_updated_at=now_local(),
                    class_teacher_id=info.get("class_teacher_id"), joined_date=now_local().date(),
                    face_trained=True, training_date=now_local())
        db.session.add(user)
        db.session.flush()
        destination = FACE_DATA / str(user.id)
        destination.mkdir(parents=True, exist_ok=True)
        for frame in frames:
            shutil.move(str(frame), str(destination / frame.name))
        np.save(FACE_DATA / f"{user.id}_encoding.npy", np.asarray(encodings, dtype=np.float64))
        shutil.rmtree(folder, ignore_errors=True)
        audit("student_create", f"Registered student {user.name} only after successful face training", "User", user.id)
        db.session.commit()
        session.pop("pending_registration", None)
        return jsonify({"message": f"Registration complete for {user.name}", "user_id": user.id, "valid_frames": len(encodings)}), 201
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 500


@app.route("/capture-face/<int:user_id>")
@staff_required
def capture_face_page(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return "Student not found", 404
    if not teacher_can_access_student(user):
        return "You are not allowed to manage this student.", 403
    token = secrets.token_urlsafe(24)
    session["pending_retrain"] = {"token": token, "user_id": user.id}
    (FACE_DATA / "_pending" / ("retrain_" + token)).mkdir(parents=True, exist_ok=True)
    return render_template("capture_face.html", user=user, token=token)


@app.route("/api/retrain/capture/<token>", methods=["POST"])
@staff_required
def capture_retrain_frame(token):
    pending = session.get("pending_retrain") or {}
    if not pending or not secrets.compare_digest(str(pending.get("token", "")), str(token)):
        return jsonify({"error": "Retraining session expired. Start again."}), 403
    user = db.session.get(User, int(pending.get("user_id") or 0))
    if not teacher_can_access_student(user):
        return jsonify({"error": "You are not allowed to retrain this student's face."}), 403
    try:
        frame = decode_data_url((request.get_json() or {}).get("image"))
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        from face_utils import face_rec_available, _face_locations
        if not face_rec_available():
            return jsonify({"error": "Face recognition is not installed on this server."}), 503
        locations = _face_locations(rgb, upsample=1)
        if len(locations) != 1:
            return jsonify({"error": "Exactly one face must be visible"}), 400
        ok, quality_message = image_quality(frame, locations[0])
        if not ok:
            return jsonify({"error": quality_message}), 400
        folder = FACE_DATA / "_pending" / ("retrain_" + token)
        count = len(list(folder.glob("frame_*.jpg")))
        if count >= 12:
            return jsonify({"error": "Maximum 10 frames captured"}), 400
        cv2.imwrite(str(folder / f"frame_{count}.jpg"), frame)
        return jsonify({"message": "Good frame captured", "frame_count": count + 1})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/retrain/complete/<token>", methods=["POST"])
@staff_required
def complete_retrain(token):
    pending = session.get("pending_retrain") or {}
    if not pending or not secrets.compare_digest(str(pending.get("token", "")), str(token)):
        return jsonify({"error": "Retraining session expired. Start again."}), 403
    user = db.session.get(User, int(pending["user_id"]))
    folder = FACE_DATA / "_pending" / ("retrain_" + token)
    if not user:
        return jsonify({"error": "Student not found"}), 404
    if not teacher_can_access_student(user):
        return jsonify({"error": "You are not allowed to retrain this student's face."}), 403
    minimum = int(setting_value("min_face_samples", "MIN_FACE_SAMPLES", "10"))
    try:
        encodings = get_face_encodings(str(folder))
        if len(encodings) < minimum:
            return jsonify({"error": f"Need at least {minimum} good frames; found {len(encodings)}"}), 400
        np.save(FACE_DATA / f"{user.id}_encoding.npy", np.asarray(encodings, dtype=np.float64))
        user.face_trained = True
        user.training_date = now_local()
        shutil.rmtree(folder, ignore_errors=True)
        session.pop("pending_retrain", None)
        audit("face_train", f"Retrained face model for {user.name}", "User", user.id)
        db.session.commit()
        return jsonify({"message": f"Face model updated for {user.name}", "valid_frames": len(encodings)})
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 500


@app.route("/api/train-face/<int:user_id>", methods=["POST"])
@staff_required
def train_existing_face(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "Student not found"}), 404
    if not teacher_can_access_student(user):
        return jsonify({"error": "You are not allowed to retrain this student's face."}), 403
    try:
        success, count = train_folder(FACE_DATA / str(user_id), FACE_DATA / f"{user_id}_encoding.npy")
        minimum = int(setting_value("min_face_samples", "MIN_FACE_SAMPLES", "10"))
        if not success or count < minimum:
            return jsonify({"error": f"Need at least {minimum} good face samples; found {count}"}), 400
        user.face_trained = True
        user.training_date = now_local()
        audit("face_train", f"Retrained face model for {user.name}", "User", user.id)
        db.session.commit()
        return jsonify({"message": "Face model retrained", "valid_frames": count})
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 500


@app.route("/attendance")
def attendance_page():
    return render_template("attendance.html")


@app.route("/api/mark-attendance", methods=["POST"])
def mark_attendance():
    data = request.get_json() or {}
    images = data.get("images") or ([data.get("image")] if data.get("image") else [])
    images = [x for x in images[:7] if x]
    if not images:
        return jsonify({"error": "No camera image received"}), 400
    try:
        today = now_local().date()
        if not is_working_day(today):
            return jsonify({"error": "Today is a non-working school day. Attendance cannot be marked."}), 409
        tolerance = float(setting_value("face_tolerance", "FACE_RECOGNITION_TOLERANCE", "0.48"))
        known_users = [(u, str(FACE_DATA / f"{u.id}_encoding.npy")) for u in User.query.filter_by(face_trained=True, active=True).all()]
        if not known_users:
            return jsonify({"error": "No trained students are available"}), 400
        votes, details = {}, {}
        valid_frames = 0
        for image_data in images:
            frame = decode_data_url(image_data)
            locations, encodings = recognize_faces(frame, upsample=1)
            if not encodings:
                continue
            if len(encodings) != 1:
                return jsonify({"error": "Only one person may be in front of the scanner"}), 400
            quality_ok, _ = image_quality(frame, locations[0])
            if not quality_ok:
                continue
            valid_frames += 1
            user, distance, second = best_match_for_encoding(encodings[0], known_users, tolerance=tolerance)
            if user is not None:
                votes[user.id] = votes.get(user.id, 0) + 1
                details[user.id] = (user, distance, second)
        if not votes:
            return jsonify({"error": "No confident match. Improve lighting, face the camera, and try again."}), 401
        winner_id, vote_count = max(votes.items(), key=lambda x: x[1])
        required_votes = 1 if len(images) == 1 else max(2, int(np.ceil(max(valid_frames, 1) * 0.60)))
        if vote_count < required_votes:
            return jsonify({"error": "Recognition was inconsistent. Please hold still and scan again.", "votes": vote_count, "required_votes": required_votes}), 401
        user, distance, second = details[winner_id]
        existing = Attendance.query.filter_by(user_id=user.id, date=today, archived=False).first()
        if existing:
            return jsonify({"message": f"Attendance already marked for {user.name}", "user_name": user.name, "already_marked": True, "votes": vote_count}), 200
        attendance = Attendance(user_id=user.id, date=today, time_in=now_local().time().replace(microsecond=0), status="present")
        db.session.add(attendance)
        db.session.flush()
        sync_notification_toggle_env()
        email_ok, email_msg, email_sid = send_email_notification(user, attendance)
        telegram_ok, telegram_msg, telegram_sid = send_telegram_notification(user, attendance)
        teacher_note = notify_teacher_arrival(user, attendance)
        audit("attendance_mark", f"Attendance marked for {user.name}; score={distance:.4f}; votes={vote_count}/{max(valid_frames, 1)}", "Attendance", attendance.id)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = Attendance.query.filter_by(user_id=user.id, date=today, archived=False).first()
            if existing:
                return jsonify({"message": f"Attendance already marked for {user.name}", "user_name": user.name, "already_marked": True}), 200
            raise
        return jsonify({"message": f"Welcome, {user.name}! Attendance marked.", "user_id": user.id, "user_name": user.name, "distance": round(distance, 4), "votes": vote_count, "frames_used": valid_frames, "email_sent": email_ok, "email_message": email_msg, "telegram_sent": telegram_ok, "telegram_message": telegram_msg, "teacher_notified": bool(teacher_note)})
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 500


@app.route("/dashboard")
@staff_required
def dashboard():
    return render_template(
        "dashboard.html",
        threshold=float(setting_value("attendance_threshold", "ATTENDANCE_THRESHOLD", "75")),
        teacher=current_teacher(),
    )


@app.route("/teacher/dashboard")
@teacher_required
def teacher_dashboard():
    return redirect(url_for("dashboard"))


@app.route("/api/all-attendance-stats")
@staff_required
def get_all_attendance_stats():
    today = now_local().date()
    default_start = today.replace(day=1)
    try:
        start_date = datetime.strptime(request.args.get("date_from") or default_start.isoformat(), "%Y-%m-%d").date()
        end_date = datetime.strptime(request.args.get("date_to") or today.isoformat(), "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date range"}), 400
    if end_date < start_date:
        return jsonify({"error": "End date must be on or after start date"}), 400
    report_end = min(end_date, today)
    global_working_dates = working_days_between(start_date, report_end) if start_date <= report_end else []
    threshold = float(setting_value("attendance_threshold", "ATTENDANCE_THRESHOLD", "75"))
    stats = []
    teacher = current_teacher()
    user_query = User.query.order_by(User.name)
    if teacher:
        user_query = user_query.filter(User.class_teacher_id == teacher.id)
    for user in user_query.all():
        # A student should not lose attendance percentage for school days
        # that happened before that student joined the school.
        student_start = max(start_date, user.joined_date) if user.joined_date else start_date
        working_dates = [d for d in global_working_dates if d >= student_start]
        working_days = len(working_dates)
        present = db.session.query(func.count(func.distinct(Attendance.date))).filter(
            Attendance.user_id == user.id,
            Attendance.date >= student_start,
            Attendance.date <= end_date,
            Attendance.archived.is_(False),
            Attendance.status.in_(["present", "late"]),
        ).scalar() or 0
        pct = (present / working_days * 100) if working_days else 0
        stats.append({"user_id": user.id, "name": user.name, "email": user.email, "class_name": user.class_name or "", "section": user.section or "", "class_teacher": user.class_teacher.name if user.class_teacher else "", "present_days": present, "absent_days": max(working_days - present, 0), "working_days": working_days, "attendance_percentage": round(pct, 2), "below_threshold": pct < threshold})
    return jsonify({"date_from": start_date.isoformat(), "date_to": end_date.isoformat(), "working_days": len(global_working_dates), "students": stats})


@app.route("/teacher/attendance")
@teacher_required
def teacher_attendance():
    teacher = current_teacher()
    return render_template(
        "teacher_attendance.html",
        users=User.query.filter_by(class_teacher_id=teacher.id).order_by(User.name).all(),
    )


@app.route("/api/teacher/attendance-records")
@teacher_required
def teacher_attendance_records():
    teacher = current_teacher()
    query = Attendance.query.join(User).filter(
        User.class_teacher_id == teacher.id,
        Attendance.archived.is_(False),
    )
    user_id = request.args.get("user_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    if user_id:
        query = query.filter(Attendance.user_id == user_id)
    if date_from:
        try:
            query = query.filter(Attendance.date >= datetime.strptime(date_from, "%Y-%m-%d").date())
        except ValueError:
            return jsonify({"error": "Invalid from date"}), 400
    if date_to:
        try:
            query = query.filter(Attendance.date <= datetime.strptime(date_to, "%Y-%m-%d").date())
        except ValueError:
            return jsonify({"error": "Invalid to date"}), 400
    rows = query.order_by(Attendance.date.desc(), Attendance.time_in.desc()).all()
    return jsonify([
        {
            "id": r.id,
            "user_name": r.user.name,
            "email": r.user.email,
            "date": r.date.isoformat(),
            "time_in": str(r.time_in) if r.time_in else "",
            "status": r.status,
        }
        for r in rows
    ])


@app.route("/admin/attendance")
@admin_required
def admin_attendance():
    return render_template("admin_attendance.html", users=User.query.order_by(User.name).all())


@app.route("/api/admin/attendance-records")
@admin_required
def admin_attendance_records():
    query = Attendance.query.join(User)
    user_id = request.args.get("user_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    archived = request.args.get("archived", "false").lower() == "true"
    query = query.filter(Attendance.archived.is_(archived))
    if user_id:
        query = query.filter(Attendance.user_id == user_id)
    if date_from:
        query = query.filter(Attendance.date >= datetime.strptime(date_from, "%Y-%m-%d").date())
    if date_to:
        query = query.filter(Attendance.date <= datetime.strptime(date_to, "%Y-%m-%d").date())
    rows = query.order_by(Attendance.date.desc(), Attendance.time_in.desc()).all()
    return jsonify([{"id": r.id, "user_name": r.user.name, "email": r.user.email, "date": r.date.isoformat(), "time_in": str(r.time_in) if r.time_in else "", "status": r.status, "archived": r.archived, "archived_at": r.archived_at.isoformat(sep=" ", timespec="seconds") if r.archived_at else None} for r in rows])


@app.route("/api/admin/attendance/<int:attendance_id>/archive", methods=["POST"])
@admin_required
def archive_attendance(attendance_id):
    record = db.session.get(Attendance, attendance_id)
    if not record:
        return jsonify({"error": "Attendance record not found"}), 404
    if record.archived:
        return jsonify({"message": "Record is already archived"})
    record.archived, record.archived_at, record.archived_by = True, now_local(), session.get("admin_username", "admin")
    audit("attendance_archive", f"Archived attendance #{record.id} for {record.user.name}", "Attendance", record.id)
    db.session.commit()
    return jsonify({"message": "Attendance archived"})


@app.route("/api/admin/attendance/<int:attendance_id>/restore", methods=["POST"])
@admin_required
def restore_attendance(attendance_id):
    record = db.session.get(Attendance, attendance_id)
    if not record:
        return jsonify({"error": "Attendance record not found"}), 404
    conflict = Attendance.query.filter(Attendance.id != record.id, Attendance.user_id == record.user_id, Attendance.date == record.date, Attendance.archived.is_(False)).first()
    if conflict:
        return jsonify({"error": "Cannot restore: an active record already exists for this student and date"}), 409
    record.archived, record.archived_at, record.archived_by = False, None, None
    audit("attendance_restore", f"Restored attendance #{record.id} for {record.user.name}", "Attendance", record.id)
    db.session.commit()
    return jsonify({"message": "Attendance restored"})


@app.route("/api/admin/attendance/<int:attendance_id>", methods=["DELETE"])
@admin_required
def delete_attendance(attendance_id):
    record = db.session.get(Attendance, attendance_id)
    if not record:
        return jsonify({"error": "Attendance record not found"}), 404
    audit("attendance_delete", f"Permanently deleted attendance #{record.id} for {record.user.name} on {record.date}", "Attendance", record.id)
    db.session.delete(record)
    db.session.commit()
    return jsonify({"message": "Attendance permanently deleted"})


@app.route("/api/admin/attendance/bulk-action", methods=["POST"])
@admin_required
def bulk_attendance_action():
    data = request.get_json() or {}
    ids, action = data.get("ids") or [], data.get("action")
    if action not in {"archive", "restore", "delete"}:
        return jsonify({"error": "Invalid action"}), 400
    processed = 0
    for raw_id in ids:
        try:
            record = db.session.get(Attendance, int(raw_id))
        except (TypeError, ValueError):
            record = None
        if not record:
            continue
        if action == "archive":
            record.archived, record.archived_at, record.archived_by = True, now_local(), session.get("admin_username", "admin")
            audit("attendance_archive", f"Bulk archived attendance #{record.id}", "Attendance", record.id)
        elif action == "restore":
            conflict = Attendance.query.filter(Attendance.id != record.id, Attendance.user_id == record.user_id, Attendance.date == record.date, Attendance.archived.is_(False)).first()
            if conflict:
                continue
            record.archived, record.archived_at, record.archived_by = False, None, None
            audit("attendance_restore", f"Bulk restored attendance #{record.id}", "Attendance", record.id)
        else:
            audit("attendance_delete", f"Bulk permanently deleted attendance #{record.id}", "Attendance", record.id)
            db.session.delete(record)
        processed += 1
    db.session.commit()
    return jsonify({"message": f"Processed {processed} record(s)"})


@app.route("/api/admin/archive-old", methods=["POST"])
@admin_required
def archive_old_records():
    days = max(1, min(int((request.get_json() or {}).get("days", 90)), 3650))
    cutoff = now_local().date() - timedelta(days=days)
    records = Attendance.query.filter(Attendance.date < cutoff, Attendance.archived.is_(False)).all()
    for r in records:
        r.archived, r.archived_at, r.archived_by = True, now_local(), session.get("admin_username", "admin")
        audit("attendance_archive", f"Auto-archived attendance #{r.id} older than {days} days", "Attendance", r.id)
    db.session.commit()
    return jsonify({"message": f"Archived {len(records)} record(s) older than {days} days"})


@app.route("/api/admin/delete-archived", methods=["POST"])
@admin_required
def delete_archived_records():
    days = max(1, min(int((request.get_json() or {}).get("days", 365)), 3650))
    cutoff = now_local() - timedelta(days=days)
    records = Attendance.query.filter(Attendance.archived.is_(True), Attendance.archived_at.is_not(None), Attendance.archived_at < cutoff).all()
    for r in records:
        audit("attendance_delete", f"Permanently deleted archived attendance #{r.id}", "Attendance", r.id)
        db.session.delete(r)
    db.session.commit()
    return jsonify({"message": f"Permanently deleted {len(records)} archived record(s)"})


@app.route("/settings")
@admin_required
def settings():
    return render_template("settings.html")


@app.route("/api/get-settings")
@admin_required
def get_settings():
    seed_settings_from_env()
    return jsonify({
        "emailAddress": os.getenv("EMAIL_ADDRESS", ""),
        "senderName": setting_value("email_sender", "EMAIL_SENDER", "Student Attendance"),
        "smtpHost": setting_value("smtp_host", "SMTP_HOST", "smtp.gmail.com"),
        "smtpPort": setting_value("smtp_port", "SMTP_PORT", "587"),
        "emailNotifications": setting_bool("email_notifications", "EMAIL_NOTIFICATIONS", False),
        "telegramEnabled": setting_bool("telegram_notifications", "TELEGRAM_NOTIFICATIONS", False),
        "telegramConfigured": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()),
        "smsEnabled": setting_bool("sms_notifications", "SMS_NOTIFICATIONS", False),
        "smsConfigured": all(os.getenv(k, "").strip() for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")),
        "twilioPhone": os.getenv("TWILIO_PHONE_NUMBER", ""),
        "attendanceThreshold": setting_value("attendance_threshold", "ATTENDANCE_THRESHOLD", "75"),
        "workingDays": setting_value("working_days", "WORKING_DAYS", "6"),
        "faceTolerance": setting_value("face_tolerance", "FACE_RECOGNITION_TOLERANCE", "0.48"),
        "minFaceSamples": setting_value("min_face_samples", "MIN_FACE_SAMPLES", "10"),
        "absenceAlertTime": setting_value("absence_alert_time", "ABSENCE_ALERT_TIME", "10:00"),
        "adminEmail": setting_value("admin_email", "ADMIN_EMAIL", ""),
    })


@app.route("/api/save-settings", methods=["POST"])
@admin_required
def save_settings():
    data = request.get_json(silent=True) or {}
    try:
        email, telegram, sms, system = data.get("email"), data.get("telegram"), data.get("sms"), data.get("system")
        if email:
            # SMTP credentials remain server-side environment variables on Render.
            # Only the provider settings that are safe to persist are stored in DB.
            setting_set("email_sender", email.get("senderName", "Student Attendance").strip() or "Student Attendance")
            setting_set("smtp_host", email.get("smtpHost", "smtp.gmail.com").strip() or "smtp.gmail.com")
            setting_set("smtp_port", str(email.get("smtpPort", 587)))
            setting_set("email_notifications", bool(email.get("notification", False)))
        if telegram:
            setting_set("telegram_notifications", bool(telegram.get("notification", False)))
        if sms:
            setting_set("sms_notifications", bool(sms.get("notification", False)))
        if system:
            threshold = float(system.get("attendanceThreshold", 75))
            working_days = int(system.get("workingDays", 6))
            tolerance = float(system.get("faceTolerance", .48))
            samples = int(system.get("minFaceSamples", 10))
            alert_time = (system.get("absenceAlertTime") or "10:00").strip()
            if not 0 <= threshold <= 100:
                raise ValueError("Attendance threshold must be between 0 and 100")
            if working_days not in (5, 6):
                raise ValueError("Working days must be 5 or 6")
            if not .35 <= tolerance <= .65:
                raise ValueError("Face tolerance must be between 0.35 and 0.65")
            if not 10 <= samples <= 12:
                raise ValueError("Minimum face samples must be between 10 and 12")
            hh, mm = [int(x) for x in alert_time.split(":", 1)]
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError("Invalid absence alert time")
            setting_set("attendance_threshold", threshold)
            setting_set("working_days", working_days)
            setting_set("face_tolerance", tolerance)
            setting_set("min_face_samples", samples)
            setting_set("absence_alert_time", alert_time)
            setting_set("admin_email", system.get("adminEmail", "").strip())
        if email or telegram or sms:
            sync_notification_toggle_env()
        audit("settings_update", "Updated system, notification and recognition settings")
        db.session.commit()
        return jsonify({"message": "Settings saved in the persistent database. Provider secrets stay in the server environment."})
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400


@app.route("/api/admin/data-summary")
@admin_required
def data_summary():
    return jsonify({
        "active_attendance": Attendance.query.filter_by(archived=False).count(),
        "archived_attendance": Attendance.query.filter_by(archived=True).count(),
        "students": User.query.count(),
        "trained_students": User.query.filter_by(face_trained=True, active=True).count(),
        "audit_logs": AuditLog.query.count(),
        "teachers": Teacher.query.count(),
        "calendar_overrides": SchoolCalendar.query.count(),
        "unread_notifications": Notification.query.filter_by(read=False).count(),
        "database_backend": db.engine.dialect.name,
    })


@app.route("/api/admin/delete-attendance-all", methods=["POST"])
@admin_required
def delete_all_attendance():
    data = request.get_json() or {}
    if data.get("confirmation") != "DELETE ATTENDANCE":
        return jsonify({"error": "Type DELETE ATTENDANCE exactly to permanently delete all attendance records"}), 400
    count = Attendance.query.count()
    Attendance.query.delete(synchronize_session=False)
    audit("attendance_delete_all", f"Permanently deleted ALL {count} attendance records from the database")
    db.session.commit()
    return jsonify({"message": f"Permanently deleted {count} attendance record(s) from the database"})


@app.route("/api/admin/delete-archived-all", methods=["POST"])
@admin_required
def delete_all_archived():
    data = request.get_json() or {}
    if data.get("confirmation") != "DELETE ARCHIVED":
        return jsonify({"error": "Type DELETE ARCHIVED exactly to permanently delete archived records"}), 400
    records = Attendance.query.filter_by(archived=True).all()
    count = len(records)
    for r in records:
        audit("attendance_delete", f"Permanently deleted archived attendance #{r.id}", "Attendance", r.id)
        db.session.delete(r)
    db.session.commit()
    return jsonify({"message": f"Permanently deleted {count} archived record(s)"})


def _json_default(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    from datetime import date as _date, time as _time
    if isinstance(value, (_date, _time)):
        return value.isoformat()
    return str(value)


def export_database_payload():
    models = [Admin, Teacher, TeacherVerificationToken, User, Attendance, SchoolCalendar, Notification, AuditLog]
    payload = {"exported_at": now_local().isoformat(), "backend": db.engine.dialect.name, "tables": {}}
    for model in models:
        rows = []
        for row in model.query.all():
            item = {}
            for column in model.__table__.columns:
                item[column.name] = getattr(row, column.name)
            rows.append(item)
        payload["tables"][model.__tablename__] = rows
    return payload


def create_full_backup():
    import sqlite3
    stamp = now_local().strftime("%Y%m%d_%H%M%S")
    archive = BACKUP_DIR / f"student_attendance_full_backup_{stamp}.zip"
    payload = export_database_payload()
    data_file = BACKUP_DIR / f"attendance_database_{stamp}.json"
    data_file.write_text(json.dumps(payload, default=_json_default, indent=2), encoding="utf-8")
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(data_file, arcname="attendance_database.json")
            if FACE_DATA.exists():
                for path in FACE_DATA.rglob("*"):
                    if path.is_file() and "_pending" not in path.parts:
                        z.write(path, arcname=str(Path("face_data") / path.relative_to(FACE_DATA)))
    finally:
        data_file.unlink(missing_ok=True)
    return archive


@app.route("/api/admin/backup")
@admin_required
def backup_database():
    import sqlite3
    stamp = now_local().strftime("%Y%m%d_%H%M%S")
    if db.engine.dialect.name == "sqlite":
        source = SQLITE_DB_PATH
        if not source.exists():
            return jsonify({"error": "SQLite database does not exist"}), 404
        filename = BACKUP_DIR / f"attendance_backup_{stamp}.db"
        src = sqlite3.connect(source)
        dst = sqlite3.connect(filename)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    else:
        filename = BACKUP_DIR / f"attendance_backup_{stamp}.json"
        filename.write_text(json.dumps(export_database_payload(), default=_json_default, indent=2), encoding="utf-8")
    audit("backup", f"Created database backup {filename.name}")
    db.session.commit()
    return send_file(filename, as_attachment=True, download_name=filename.name)


@app.route("/api/admin/backup-full")
@admin_required
def backup_full():
    archive = create_full_backup()
    audit("full_backup", f"Created full database + face-data backup {archive.name}")
    db.session.commit()
    return send_file(archive, as_attachment=True, download_name=archive.name)


@app.route("/api/admin/compact-db", methods=["POST"])
@admin_required
def compact_db():
    import sqlite3
    db.session.commit()
    backend = db.engine.dialect.name
    if backend == "sqlite":
        conn = sqlite3.connect(SQLITE_DB_PATH)
        try:
            conn.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()
        message = "SQLite database compacted."
    elif backend == "postgresql":
        with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("ANALYZE"))
        message = "PostgreSQL database statistics refreshed."
    else:
        message = f"Database maintenance completed for {backend}."
    audit("database_compact", message)
    db.session.commit()
    return jsonify({"message": message})


@app.route("/admin/audit")
@admin_required
def audit_page():
    return render_template("audit.html")


@app.route("/api/admin/audit")
@admin_required
def audit_api():
    logs = AuditLog.query.order_by(AuditLog.timestamp.desc()).limit(500).all()
    return jsonify([{"id": l.id, "timestamp": l.timestamp.isoformat(sep=" ", timespec="seconds"), "admin": l.admin_username, "action": l.action, "description": l.description} for l in logs])


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return render_template("error.html", code=404, message="Page not found"), 404


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    if request.path.startswith("/api/"):
        return jsonify({"error": "Internal server error"}), 500
    return render_template("error.html", code=500, message="Internal server error"), 500


@app.route("/healthz")
def healthz():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "ok", "time": now_local().isoformat()}), 200
    except Exception as exc:
        return jsonify({"status": "error", "database": "unavailable", "error": str(exc)}), 503


def run_absence_job():
    target = today_local()
    result = send_absence_alerts(target, force=False)
    audit("absence_job", f"Processed scheduled absence alerts for {target}: {result}")
    db.session.commit()
    return result


with app.app_context():
    migrate_database_schema()
    db.create_all()
    seed_settings_from_env()
    sync_notification_toggle_env()
    ensure_default_admin()
    cleanup_pending_registrations()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
