# Render deployment checklist

## 1. Database

Use Supabase PostgreSQL for the production database. Put its PostgreSQL connection string in Render as `DATABASE_URL`.

The app also supports SQLite fallback under `DATA_DIR`. On Render the Blueprint sets `DATA_DIR=/var/data`, but that fallback should not be used for the cron-based absence-alert workflow because a cron service does not share the web service's persistent disk.

## 2. Persistent disk

The web service mounts a 10 GB Render Persistent Disk at:

```text
/var/data
```

The app stores:

```text
/var/data/face_data/
/var/data/backups/
/var/data/attendance.db   # only when DATABASE_URL is empty
```

## 3. Required environment variables

```text
DATABASE_URL
ADMIN_PASSWORD
APP_BASE_URL
SECRET_KEY
```

For email:

```text
EMAIL_ADDRESS
EMAIL_PASSWORD
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USE_TLS=true
```

Use a Gmail App Password, not the normal Gmail account password.

## 4. Teacher login

A teacher normally signs in with the teacher username/password. The current administrator password also works as an explicit admin override for an active teacher account. Email verification is required for normal login when an email address exists; local-only teacher accounts do not require email verification.

## 5. First admin login

Use the `ADMIN_USERNAME` and `ADMIN_PASSWORD` configured in Render. The initial admin account is created automatically. The first generated account is marked for a password change.


## 8. Parent notification channels

- Email: configure `EMAIL_ADDRESS` and `EMAIL_PASSWORD` (for Gmail, use a Google App Password with 2-Step Verification).
- SMS: configure the Twilio variables and enable SMS in Settings. Store parent phone numbers in E.164 form when possible; the app uses India as the default region for 10-digit numbers.
- Telegram: set `TELEGRAM_BOT_TOKEN`, then have the parent start the bot before sending messages. The Telegram Bot API does not allow a bot to initiate a private conversation with a user who has never started it.
- Parent-level channel opt-ins are stored in the persistent database. SMS and Telegram default to off; email preserves existing parent-email behavior and can be switched off per student.
- Twilio delivery callbacks are exposed at `/api/webhooks/twilio/sms-status` and inbound STOP/START handling at `/api/webhooks/twilio/sms-inbound`; point your Twilio number's messaging webhook to the public URL plus that inbound path.
- The Settings page includes provider test actions. Provider credentials are intentionally not persisted through the browser UI.
