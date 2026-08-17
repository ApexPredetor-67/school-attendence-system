# Notification production checklist

## Email

1. Create/choose a dedicated school sender mailbox.
2. For Gmail SMTP, enable 2-Step Verification and use a Google App Password rather than the normal account password.
3. Configure `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `EMAIL_SENDER`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USE_TLS` in Render.
4. Open Settings as admin and send a test email.
5. Keep parent email notifications enabled only where the school has permission to send attendance updates.

## SMS / Twilio

1. Configure `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_PHONE_NUMBER` in Render.
2. Use E.164 numbers such as `+919876543210` for parents whenever possible.
3. Set `APP_BASE_URL` to the public Render URL.
4. Point the Twilio number's incoming-message webhook to:
   `/api/webhooks/twilio/sms-inbound`
5. Twilio delivery callbacks are accepted at:
   `/api/webhooks/twilio/sms-status`
6. Use the Settings test SMS before enabling scheduled alerts.
7. Enable SMS per parent only after the school has obtained the required consent.

## Telegram

1. Create the bot with BotFather and keep the bot token secret.
2. Configure `TELEGRAM_BOT_TOKEN` in Render.
3. Have the parent start the bot before saving a chat ID / enabling Telegram notifications.
4. Test the chat ID from Settings.
5. Enable Telegram per parent only after the parent has agreed to automated attendance messages.

## Scheduled absence alerts

- Render cron runs every 5 minutes.
- The app uses the school's `APP_TIMEZONE` to decide whether the configured local absence-alert time has been reached.
- The job is idempotent per student/day, so repeated cron runs do not resend the same absence alert after a successful/pending notification record is created.
- Manual admin/teacher "check absences" actions can run immediately without waiting for the configured clock time.
