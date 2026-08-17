from datetime import date, datetime, time
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, text

db = SQLAlchemy()

def now_local():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)

class Teacher(db.Model):
    __tablename__ = "teacher"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    email_verified = db.Column(db.Boolean, default=False, nullable=False, index=True)
    email_verification_token_hash = db.Column(db.String(128), nullable=True, index=True)
    email_verification_expires_at = db.Column(db.DateTime, nullable=True)
    password_reset_token_hash = db.Column(db.String(128), nullable=True, index=True)
    password_reset_expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False)
    last_login = db.Column(db.DateTime, nullable=True)

class TeacherVerificationToken(db.Model):
    __tablename__ = "teacher_verification_token"
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey("teacher.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = db.Column(db.String(128), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False)
    teacher = db.relationship("Teacher", backref=db.backref("verification_tokens", lazy=True, cascade="all, delete-orphan"))

class User(db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    telegram_chat_id = db.Column(db.String(80), nullable=True)
    admission_number = db.Column(db.String(50), unique=True, nullable=True, index=True)
    roll_number = db.Column(db.String(30), nullable=True)
    class_name = db.Column(db.String(30), nullable=True, index=True)
    section = db.Column(db.String(10), nullable=True, index=True)
    parent_name = db.Column(db.String(120), nullable=True)
    parent_phone = db.Column(db.String(30), nullable=True)
    parent_email = db.Column(db.String(160), nullable=True)
    parent_email_opt_in = db.Column(db.Boolean, default=True, nullable=False)
    parent_sms_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    parent_telegram_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    notification_preferences_updated_at = db.Column(db.DateTime, nullable=True)
    class_teacher_id = db.Column(db.Integer, db.ForeignKey("teacher.id", ondelete="SET NULL"), nullable=True, index=True)
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    joined_date = db.Column(db.Date, nullable=True)
    face_trained = db.Column(db.Boolean, default=False, nullable=False, index=True)
    training_date = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False)
    attendances = db.relationship("Attendance", backref="user", lazy=True, cascade="all, delete-orphan")
    class_teacher = db.relationship("Teacher", backref=db.backref("students", lazy=True), foreign_keys=[class_teacher_id])

class Attendance(db.Model):
    __tablename__ = "attendance"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, default=lambda: now_local().date(), index=True)
    time_in = db.Column(db.Time, nullable=True)
    time_out = db.Column(db.Time, nullable=True)
    status = db.Column(db.String(20), default="present", nullable=False, index=True)
    archived = db.Column(db.Boolean, default=False, nullable=False, index=True)
    archived_at = db.Column(db.DateTime, nullable=True)
    archived_by = db.Column(db.String(120), nullable=True)
    __table_args__ = (
        Index("ix_attendance_user_date", "user_id", "date"),
        Index("ix_attendance_archived_date", "archived", "date"),
        Index("ux_attendance_active_user_date", "user_id", "date", unique=True, sqlite_where=text("archived = 0"), postgresql_where=text("archived = FALSE")),
    )

class SchoolCalendar(db.Model):
    __tablename__ = "school_calendar"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False, index=True)
    is_working = db.Column(db.Boolean, nullable=False, default=True)
    reason = db.Column(db.String(255), nullable=True)
    created_by = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False)
    updated_at = db.Column(db.DateTime, default=now_local, onupdate=now_local, nullable=False)

class Notification(db.Model):
    __tablename__ = "notification"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False, index=True)
    kind = db.Column(db.String(40), nullable=False, index=True)
    recipient_type = db.Column(db.String(30), nullable=False, index=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey("teacher.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True)
    message = db.Column(db.String(500), nullable=False)
    provider = db.Column(db.String(40), default="system", nullable=False)
    status = db.Column(db.String(30), default="created", nullable=False)
    dedupe_key = db.Column(db.String(180), unique=True, nullable=True)
    read = db.Column(db.Boolean, default=False, nullable=False)
    sent_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.String(500), nullable=True)
    provider_message_id = db.Column(db.String(120), nullable=True, index=True)
    provider_status = db.Column(db.String(40), nullable=True)
    teacher = db.relationship("Teacher", backref=db.backref("notifications", lazy=True))
    user = db.relationship("User", backref=db.backref("notifications", lazy=True))

class Admin(db.Model):
    __tablename__ = "admin"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False)
    last_login = db.Column(db.DateTime, nullable=True)

class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=now_local, nullable=False, index=True)
    admin_username = db.Column(db.String(80), nullable=False)
    action = db.Column(db.String(50), nullable=False, index=True)
    target_type = db.Column(db.String(50), nullable=True)
    target_id = db.Column(db.Integer, nullable=True)
    description = db.Column(db.String(500), nullable=False)


class AppSetting(db.Model):
    __tablename__ = "app_setting"
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=now_local, onupdate=now_local, nullable=False)
