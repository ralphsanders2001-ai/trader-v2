"""
OpenObserve + ntfy alert monitor. Query recent logs and alert on critical events.
"""
import os, sys, json, requests
from datetime import datetime, timedelta, timezone

OO_QUERY_URL = "http://127.0.0.1:5080/api/default/_search"
OO_AUTH = os.environ.get("OO_AUTH", "")  # set via env: "Basic <base64 user:pass>"


def _query(stream: str, query: str, minutes: int = 10) -> list:
    """Run an OpenObserve SQL query against a log stream."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes)

    payload = {
        "query": {
            "sql": f"SELECT * FROM {stream} WHERE {query}",
            "start_time": int(start.timestamp() * 1e6),
            "end_time": int(now.timestamp() * 1e6),
        },
    }

    try:
        resp = requests.post(
            OO_QUERY_URL,
            json=payload,
            headers={
                "Authorization": OO_AUTH,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        data = resp.json()
        return data.get("hits", [])
    except Exception as e:
        return [{"error": str(e)}]


def check_alerts():
    alerts = []

    # ERROR or Traceback in any trainer log
    errors = _query("training_logs", "level='ERROR' OR message LIKE '%Traceback%'")
    if errors:
        alerts.append(f"Trainer errors in last 10m: {len(errors)}")

    # Live order rejected
    rejected = _query("training_logs", "message LIKE '%rejected%'")
    if rejected:
        alerts.append(f"Live order rejections: {len(rejected)}")

    # Crypto bot attempted to run while paused
    crypto = _query("training_logs", "message LIKE '%crypto%' AND level='ERROR'")
    if crypto:
        alerts.append(f"Crypto bot alerts: {len(crypto)}")

    return alerts


def notify_alerts(alerts: list):
    try:
        sys.path.insert(0, "/home/ralph/robinhood-trainer")
        from notify import notify
        body = "\n".join(alerts)
        notify("Trainer alerts", body, priority="high", tags=["warning"])
    except Exception as e:
        print(f"[alert_check] notify failed: {e}")


if __name__ == "__main__":
    alerts = check_alerts()
    if alerts:
        print("ALERTS:")
        for a in alerts:
            print(a)
        notify_alerts(alerts)
        sys.exit(1)
    print("No alerts")
    sys.exit(0)
