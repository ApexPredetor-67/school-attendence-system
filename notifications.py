import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


def _enabled(name, default=False):
    return os.getenv(name, str(default)).strip().lower() == "true"


def _smtp_config():
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() == "true"
    sender = os.getenv("EMAIL_ADDRESS", "").strip()
    password = os.getenv("EMAIL_PASSWORD", "")
    name = os.getenv("EMAIL_SENDER", "Student Attendance").strip() or "Student Attendance"
    return host, port, use_tls, sender, password, name


def _send_mail(recipient, subject, body):
    host, port, use_tls, sender, password, sender_name = _smtp_config()
    if not sender or not password:
        return False, "SMTP email settings are incomplete", None
    if not recipient:
        return False, "Recipient email address is empty", None
    try:
        msg = MIMEMultipart()
        msg["From"] = f"{sender_name} <{sender}>"
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        return True, "Email accepted by SMTP server", None
    except Exception as exc:
        return False, str(exc), None


def send_email_notification(user, attendance):
    if not _enabled("EMAIL_NOTIFICATIONS", False):
        return False, "Email notifications disabled", None
    if not getattr(user, "parent_email_opt_in", True):
        return False, "Parent email notifications are not opted in", None
    recipient = getattr(user, "parent_email", None) or getattr(user, "email", None)
    _, _, _, _, _, sender_name = _smtp_config()
    subject = f"Attendance update for {user.name} — {attendance.date}"
    body = (
        f"Hello,\n\n"
        f"This is an attendance update from the school.\n\n"
        f"Student: {user.name}\n"
        f"Date: {attendance.date}\n"
        f"Time in: {attendance.time_in or '—'}\n"
        f"Status: {(attendance.status or 'present').title()}\n\n"
        f"This is an automated message; please do not reply to this address.\n\n"
        f"Regards,\n{sender_name}"
    )
    return _send_mail(recipient, subject, body)



def send_email_absence_notification(user, target_date):
    if not _enabled("EMAIL_NOTIFICATIONS", False):
        return False, "Email notifications disabled", None
    if not getattr(user, "parent_email_opt_in", True):
        return False, "Parent email notifications are not opted in", None
    recipient = getattr(user, "parent_email", None) or getattr(user, "email", None)
    _, _, _, _, _, sender_name = _smtp_config()
    subject = f"Attendance alert: {user.name} was not marked present — {target_date}"
    body = (f"Hello,\n\nThe school attendance system has not recorded {user.name} as present for {target_date}.\n\n"
            f"Please contact the school if this is unexpected.\n\nThis is an automated message; please do not reply to this address.\n\nRegards,\n{sender_name}")
    return _send_mail(recipient, subject, body)

def send_teacher_verification_email(teacher, verification_url, expires_hours=24):
    _, _, _, _, _, sender_name = _smtp_config()
    body = (f"Hello {teacher.name},\n\nYour Student Attendance teacher account has been created.\n\n"
            f"Verify your email using this link:\n\n{verification_url}\n\n"
            f"This link expires in {expires_hours} hours.\n\nIf you did not expect this account, ignore this email.\n\nRegards,\n{sender_name}")
    return _send_mail((teacher.email or "").strip(), "Verify your Student Attendance teacher account", body)


def send_teacher_password_reset_email(teacher, reset_url, expires_minutes=30):
    _, _, _, _, _, sender_name = _smtp_config()
    body = (f"Hello {teacher.name},\n\nA password reset was requested for your teacher account.\n\n"
            f"Use this secure link to choose a new password:\n\n{reset_url}\n\n"
            f"This link expires in {expires_minutes} minutes and can only be used once.\n\nIf you did not request this, ignore this email.\n\nRegards,\n{sender_name}")
    return _send_mail((teacher.email or "").strip(), "Reset your Student Attendance teacher password", body)


def normalize_e164(phone):
    """Normalize common phone formats to E.164; defaults to India for 10-digit numbers."""
    raw = (phone or "").strip()
    if not raw:
        return None
    try:
        import phonenumbers
        region = os.getenv("DEFAULT_PHONE_REGION", "IN").strip().upper() or "IN"
        parsed = phonenumbers.parse(raw, region)
        if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
            return None
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        digits = re.sub(r"[^0-9+]", "", raw)
        if digits.startswith("00"):
            digits = "+" + digits[2:]
        if digits.startswith("+") and 8 <= len(digits[1:]) <= 15:
            return digits
        if len(digits) == 10 and digits[0] in "6789":
            country = os.getenv("DEFAULT_PHONE_COUNTRY_CODE", "+91").strip() or "+91"
            if not country.startswith("+"):
                country = "+" + country
            return country + digits
        return None


def send_sms_notification(user, message, status_callback_url=None):
    if not _enabled("SMS_NOTIFICATIONS", False):
        return False, "SMS notifications disabled", None
    if not getattr(user, "parent_sms_opt_in", False):
        return False, "Parent SMS notifications are not opted in", None
    to = normalize_e164(getattr(user, "parent_phone", None) or getattr(user, "phone", None))
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_phone = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
    if not all([to, sid, token, from_phone]):
        return False, "SMS provider, E.164 recipient, or phone number is not configured", None
    if len(message) > 1500:
        message = message[:1497] + "..."
    try:
        from twilio.rest import Client
        client = Client(sid, token)
        kwargs = {"body": message, "from_": from_phone, "to": to}
        if status_callback_url:
            kwargs["status_callback"] = status_callback_url
        result = client.messages.create(**kwargs)
        return True, f"SMS accepted by Twilio ({result.status})", getattr(result, "sid", None)
    except Exception as exc:
        return False, str(exc), None


def telegram_bot_url():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    return f"https://api.telegram.org/bot{token}" if token else ""


def send_telegram_message(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        return False, "Telegram bot token or chat ID is not configured", None
    if len(text) > 4096:
        text = text[:4093] + "..."
    payload = json.dumps({"chat_id": str(chat_id), "text": text, "disable_web_page_preview": True}).encode("utf-8")
    request = Request(f"https://api.telegram.org/bot{bot_token}/sendMessage", data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            return False, data.get("description", "Telegram rejected the message"), None
        return True, "Telegram message sent", str((data.get("result") or {}).get("message_id") or "") or None
    except (HTTPError, URLError, TimeoutError) as exc:
        return False, str(exc), None
    except Exception as exc:
        return False, str(exc), None



def send_telegram_absence_notification(user, target_date):
    if not _enabled("TELEGRAM_NOTIFICATIONS", False):
        return False, "Telegram notifications disabled", None
    if not getattr(user, "parent_telegram_opt_in", False):
        return False, "Parent Telegram notifications are not opted in", None
    return send_telegram_message(
        os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        getattr(user, "telegram_chat_id", None),
        f"⚠️ Attendance alert\n\nStudent: {user.name}\nDate: {target_date}\nStatus: Not marked present\n\nPlease contact the school if this is unexpected."
    )

def send_telegram_notification(user, attendance):
    if not _enabled("TELEGRAM_NOTIFICATIONS", False):
        return False, "Telegram notifications disabled", None
    if not getattr(user, "parent_telegram_opt_in", False):
        return False, "Parent Telegram notifications are not opted in", None
    return send_telegram_message(
        os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        getattr(user, "telegram_chat_id", None),
        f"Attendance recorded\n\nStudent: {user.name}\nDate: {attendance.date}\nTime: {attendance.time_in}\nStatus: {(attendance.status or 'present').title()}"
    )
