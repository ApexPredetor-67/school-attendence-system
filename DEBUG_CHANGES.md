# Debug changes — notification + teacher hardening

## Completed in this build

### Render persistence
- Production database remains PostgreSQL through `DATABASE_URL` (Supabase recommended).
- Render `/var/data` persistent disk remains the fallback for SQLite and stores face data/backups.
- Application settings that used to be written to the ephemeral project `.env` are now stored in the database.
- Provider secrets stay in Render environment variables instead of the browser/admin UI.
- Cron is explicitly configured with the external database and notification provider environment variables.

### Teacher accounts / teacher UI
- Teacher login supports the teacher password or current admin password as an admin override.
- Teacher student ownership is enforced on the server.
- Teacher attendance and percentage queries are filtered by `class_teacher_id`.
- Teacher management page was rewritten to avoid unsafe inline JSON/onclick interpolation.
- Teacher list now uses event delegation, safer data attributes, loading states, responsive actions, and clearer status badges.
- Refresh, verify, resend, reset, set-password, activate/deactivate, and delete actions now give consistent feedback.

### Parent messaging
- Email, SMS, and Telegram channels have per-student opt-in flags.
- SMS defaults to opt-out; Telegram defaults to opt-out.
- Existing parent email behavior is preserved for records that already have a parent email.
- SMS phone normalization supports E.164 and defaults 10-digit numbers to India.
- SMS delivery callback records provider status and Twilio message ID.
- Twilio inbound STOP/UNSUBSCRIBE/etc. automatically disables SMS for matching parent numbers; START/UNSTOP/etc. re-enables the app preference.
- Telegram messaging handles Telegram's user-start requirement; a bot cannot initiate a private chat with a user who has never started it.
- Email/Telegram/SMS test actions were added to Settings.
- Absence alerts now respect the configured local alert time, while manual admin/teacher checks can run immediately.
- Scheduled absence alerts can use SMS, email and Telegram, subject to channel configuration and consent.
- Duplicate absence notifications use a stable per-student/per-day dedupe key.

### UI / JavaScript glitch fixes
- Teacher page JavaScript was hardened against quotes/special characters in teacher names.
- Student page was rewritten to remove fragile inline JSON `onclick` handlers and added notification preference editing.
- Notifications page was rewritten to avoid invalid nested quotes and uses toast feedback instead of browser alerts.
- Registration capture flow JavaScript was cleaned up and syntax-validated.
- Settings page no longer pretends provider secrets are saved through the browser; secrets are server-side.
- Responsive/mobile behavior was retained and improved rather than changing the core GUI.

## Static validation
- 22 Jinja/HTML templates parsed successfully.
- All embedded JavaScript blocks passed `node --check` after replacing Jinja expressions with placeholders for syntax-only validation.
- Python files passed `py_compile`/AST parsing.
- `render.yaml` parses successfully.

## Important deployment notes
- Do not put real secrets in `.env.example`, Git, or the browser.
- Configure `DATABASE_URL` in Render for Supabase PostgreSQL.
- Configure provider credentials separately on the web service and the cron job.
- Set `APP_BASE_URL` to the exact public Render URL so Twilio can call the status/inbound webhook endpoints.
