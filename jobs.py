import sys
from app import app, run_absence_job, seed_settings_from_env, sync_notification_toggle_env

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "absence-alerts"
    if command != "absence-alerts":
        raise SystemExit(f"Unknown job: {command}")
    with app.app_context():
        seed_settings_from_env()
        sync_notification_toggle_env()
        result = run_absence_job()
        print(result)
