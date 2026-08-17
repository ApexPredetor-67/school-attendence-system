# Student Face Recognition Attendance System

Flask + Python face-recognition attendance system with persistent PostgreSQL/SQLite storage, teacher accounts, teacher-isolated student access, attendance percentage reports, school calendar, and email/SMS/Telegram notifications.

## Core behavior preserved

- Existing face-recognition registration and attendance flow remains intact.
- A student is only created after successful face enrollment.
- Attendance uses multiple webcam frames and conservative matching.
- Attendance percentages use approved school working days.
- The existing GUI structure is preserved; the update only adds navigation, polish, animations, icons, and role-specific pages.

## Database persistence on Render

### Recommended production setup: Supabase PostgreSQL

Set `DATABASE_URL` to the Supabase PostgreSQL connection string. The database is independent of the Render web-service filesystem, so students, teachers, attendance, calendar data, notifications, and audit records survive restarts and deploys.

The Render persistent disk at `/var/data` is still used for face encodings/images and backup files.

### Render Persistent Disk fallback

If `DATABASE_URL` is accidentally omitted, the application now falls back to SQLite under `DATA_DIR`. On the included Render Blueprint, `DATA_DIR=/var/data`, so the SQLite file is placed on the persistent disk instead of the ephemeral application filesystem.

This fallback is suitable for a single web service only. Because Render cron jobs do not share a web service's persistent disk, **use Supabase PostgreSQL for production when scheduled absence alerts are enabled**.

## Teacher accounts

Teachers can now:

- Log in with their own teacher password.
- Use the current administrator password as an administrator override for an active teacher account.
- Register students.
- Automatically assign newly registered students to themselves.
- View only their own students.
- View only their own attendance records.
- View attendance percentages only for their own students.
- Retrain faces only for their own students.
- Receive arrival/absence notifications for their own students.

Teachers cannot:

- View another teacher's students.
- Reassign students to another teacher.
- Permanently delete student accounts.
- Access admin teacher management, settings, backups, audit logs, or destructive attendance controls.

## Admin controls

Admin authentication protects all management APIs and pages, including teacher accounts.

Admins can:

- Create/delete teacher accounts.
- Activate/deactivate teachers.
- Verify teacher email manually.
- Resend verification emails.
- Set/reset teacher passwords.
- Manage all students and attendance.
- View all reports.
- Maintain the school calendar.
- Download database/full backups.

## Local development

Use **64-bit Python 3.12**.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
# Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install --no-deps face-recognition==1.3.0
```

Create `.env` from `.env.example` and configure at minimum:

```text
SECRET_KEY=replace-with-a-long-random-secret
ADMIN_USERNAME=admin
ADMIN_PASSWORD=ChangeMe123!
APP_TIMEZONE=Asia/Kolkata
```

For local SQLite, leave `DATABASE_URL` empty. You can optionally set `DATA_DIR` to a writable folder.

Run:

```bash
python app.py
```

Open `http://127.0.0.1:5000`.

## Render deployment

The included `render.yaml` creates:

1. A Python/Gunicorn web service.
2. A 10 GB persistent disk mounted at `/var/data` for face data and backups.
3. A scheduled absence-alert cron job.

Set these Render environment variables:

- `DATABASE_URL` — **recommended/required for production**, pointing to Supabase PostgreSQL.
- `ADMIN_PASSWORD` — secure administrator password.
- `APP_BASE_URL` — the exact public Render URL.
- `EMAIL_ADDRESS` and `EMAIL_PASSWORD` — Gmail address and App Password if email is enabled.
- `ADMIN_EMAIL` — optional admin notification email.
- Twilio/Telegram variables if those providers are enabled.

Do not commit `.env` or provider secrets.

## Face-recognition build

Render uses Python 3.12, `dlib-bin`, and installs `face-recognition` without dependency resolution so pip does not attempt to compile dlib from source. The web service's build command contains this explicit installation step.

## Security notes

- Passwords are stored as Werkzeug password hashes.
- Teacher verification/reset links use random one-time tokens stored as SHA-256 hashes.
- Teacher access is enforced server-side by `teacher_id` filters; hiding UI controls is not the security boundary.
- The administrator override checks the current admin password hash stored in the database, so changing the admin password also changes the override password.
- The uploaded prototype's `.env` is intentionally excluded from the repaired project. Use `.env.example` instead.

## Notification production notes

Parent notifications now use explicit per-channel preferences. Email notifications require `parent_email_opt_in`; SMS requires `parent_sms_opt_in`; Telegram requires `parent_telegram_opt_in` and a valid chat ID. Existing parent email addresses are treated as opted in on migration to preserve prior email behavior, while SMS/Telegram remain opted out until explicitly enabled.

Provider secrets are kept in Render environment variables rather than the app database so they survive deploys and are not exposed by the admin settings API. The Settings page provides provider health and test actions.

Twilio SMS uses E.164 normalization and a signed status callback endpoint so accepted/delivered/failed states can be recorded. Telegram requires the user to start the bot before the bot can message them.
