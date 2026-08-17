/*
# Create face-recognition attendance system schema

This schema supports the Flask/SQLAlchemy face-recognition attendance app.
The app connects with a service-role / direct Postgres connection string
and manages its own auth (admin + teacher logins with password hashes), so
the tables are NOT tied to Supabase auth.users. RLS is disabled on all
tables because the app authenticates at the application layer and uses a
privileged connection.

1. New Tables
- admin: administrator login accounts (username + password hash)
- teacher: teacher accounts with email verification + password reset
- teacher_verification_token: email verification tokens (multiple valid until used/expired)
- "user": registered students with face-training metadata
- attendance: per-student daily attendance records (active + archived)
- school_calendar: per-date working/non-working overrides
- notification: arrival/absence notifications to teachers/admins
- audit_log: append-only admin action audit trail

2. Security
- RLS is intentionally NOT enabled: the Flask app authenticates at the
  application layer and connects with a privileged/service-role Postgres
  connection. The Supabase anon key is never used by this app.

3. Notes
- Identifiers are quoted where needed (user, teacher, attendance, etc.).
- Partial unique index on attendance(user_id, date) WHERE archived = FALSE
  prevents duplicate active records per student per day.
*/

CREATE TABLE IF NOT EXISTS "admin" (
  id SERIAL PRIMARY KEY,
  username VARCHAR(80) UNIQUE NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMP DEFAULT now() NOT NULL,
  last_login TIMESTAMP
);

CREATE TABLE IF NOT EXISTS "teacher" (
  id SERIAL PRIMARY KEY,
  name VARCHAR(120) NOT NULL,
  email VARCHAR(160),
  phone VARCHAR(30),
  username VARCHAR(80) UNIQUE NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  email_verified BOOLEAN NOT NULL DEFAULT FALSE,
  email_verification_token_hash VARCHAR(128),
  email_verification_expires_at TIMESTAMP,
  password_reset_token_hash VARCHAR(128),
  password_reset_expires_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT now() NOT NULL,
  last_login TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_teacher_username ON "teacher"(username);
CREATE INDEX IF NOT EXISTS ix_teacher_active ON "teacher"(active);
CREATE INDEX IF NOT EXISTS ix_teacher_email_verified ON "teacher"(email_verified);

CREATE TABLE IF NOT EXISTS teacher_verification_token (
  id SERIAL PRIMARY KEY,
  teacher_id INTEGER NOT NULL REFERENCES "teacher"(id) ON DELETE CASCADE,
  token_hash VARCHAR(128) UNIQUE NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  used_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tvt_teacher_id ON teacher_verification_token(teacher_id);
CREATE INDEX IF NOT EXISTS ix_tvt_token_hash ON teacher_verification_token(token_hash);
CREATE INDEX IF NOT EXISTS ix_tvt_expires_at ON teacher_verification_token(expires_at);

CREATE TABLE IF NOT EXISTS "user" (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(120) UNIQUE NOT NULL,
  phone VARCHAR(30),
  telegram_chat_id VARCHAR(80),
  admission_number VARCHAR(50) UNIQUE,
  roll_number VARCHAR(30),
  class_name VARCHAR(30),
  section VARCHAR(10),
  parent_name VARCHAR(120),
  parent_phone VARCHAR(30),
  parent_email VARCHAR(160),
  parent_email_opt_in BOOLEAN NOT NULL DEFAULT TRUE,
  parent_sms_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
  parent_telegram_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
  notification_preferences_updated_at TIMESTAMP,
  class_teacher_id INTEGER REFERENCES "teacher"(id) ON DELETE SET NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  joined_date DATE,
  face_trained BOOLEAN NOT NULL DEFAULT FALSE,
  training_date TIMESTAMP,
  created_at TIMESTAMP DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_user_name ON "user"(name);
CREATE INDEX IF NOT EXISTS ix_user_admission_number ON "user"(admission_number);
CREATE INDEX IF NOT EXISTS ix_user_class_name ON "user"(class_name);
CREATE INDEX IF NOT EXISTS ix_user_section ON "user"(section);
CREATE INDEX IF NOT EXISTS ix_user_class_teacher_id ON "user"(class_teacher_id);
CREATE INDEX IF NOT EXISTS ix_user_active ON "user"(active);
CREATE INDEX IF NOT EXISTS ix_user_face_trained ON "user"(face_trained);

CREATE TABLE IF NOT EXISTS attendance (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  date DATE NOT NULL,
  time_in TIME,
  time_out TIME,
  status VARCHAR(20) NOT NULL DEFAULT 'present',
  archived BOOLEAN NOT NULL DEFAULT FALSE,
  archived_at TIMESTAMP,
  archived_by VARCHAR(120)
);
CREATE INDEX IF NOT EXISTS ix_attendance_user_id ON attendance(user_id);
CREATE INDEX IF NOT EXISTS ix_attendance_date ON attendance(date);
CREATE INDEX IF NOT EXISTS ix_attendance_status ON attendance(status);
CREATE INDEX IF NOT EXISTS ix_attendance_archived ON attendance(archived);
CREATE INDEX IF NOT EXISTS ix_attendance_user_date ON attendance(user_id, date);
CREATE INDEX IF NOT EXISTS ix_attendance_archived_date ON attendance(archived, date);

-- Partial unique index: only one active record per student per day
CREATE UNIQUE INDEX IF NOT EXISTS ux_attendance_active_user_date
  ON attendance (user_id, date) WHERE archived = FALSE;

CREATE TABLE IF NOT EXISTS school_calendar (
  id SERIAL PRIMARY KEY,
  date DATE UNIQUE NOT NULL,
  is_working BOOLEAN NOT NULL DEFAULT TRUE,
  reason VARCHAR(255),
  created_by VARCHAR(120),
  created_at TIMESTAMP DEFAULT now() NOT NULL,
  updated_at TIMESTAMP DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_school_calendar_date ON school_calendar(date);

CREATE TABLE IF NOT EXISTS notification (
  id SERIAL PRIMARY KEY,
  created_at TIMESTAMP DEFAULT now() NOT NULL,
  kind VARCHAR(40) NOT NULL,
  recipient_type VARCHAR(30) NOT NULL,
  teacher_id INTEGER REFERENCES "teacher"(id) ON DELETE SET NULL,
  user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE,
  message VARCHAR(500) NOT NULL,
  provider VARCHAR(40) NOT NULL DEFAULT 'system',
  status VARCHAR(30) NOT NULL DEFAULT 'created',
  dedupe_key VARCHAR(180) UNIQUE,
  read BOOLEAN NOT NULL DEFAULT FALSE,
  sent_at TIMESTAMP,
  error_message VARCHAR(500),
  provider_message_id VARCHAR(120),
  provider_status VARCHAR(40)
);
CREATE INDEX IF NOT EXISTS ix_notification_created_at ON notification(created_at);
CREATE INDEX IF NOT EXISTS ix_notification_kind ON notification(kind);
CREATE INDEX IF NOT EXISTS ix_notification_recipient_type ON notification(recipient_type);
CREATE INDEX IF NOT EXISTS ix_notification_teacher_id ON notification(teacher_id);
CREATE INDEX IF NOT EXISTS ix_notification_user_id ON notification(user_id);
CREATE INDEX IF NOT EXISTS ix_notification_read ON notification(read);

CREATE TABLE IF NOT EXISTS audit_log (
  id SERIAL PRIMARY KEY,
  timestamp TIMESTAMP DEFAULT now() NOT NULL,
  admin_username VARCHAR(80) NOT NULL,
  action VARCHAR(50) NOT NULL,
  target_type VARCHAR(50),
  target_id INTEGER,
  description VARCHAR(500) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_log_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS ix_audit_log_action ON audit_log(action);

CREATE TABLE IF NOT EXISTS app_setting (
  key VARCHAR(100) PRIMARY KEY,
  value TEXT,
  updated_at TIMESTAMP DEFAULT now() NOT NULL
);
