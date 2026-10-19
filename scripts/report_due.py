"""Decide whether a scheduled backup must retry; runnable before pip install."""
import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def needs_collection(root: Path, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone(timedelta(hours=8)))
    date = now.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    report = root / "reports" / f"weekly-{date}.md"
    try:
        if not report.is_file() or not report.stat().st_size:
            return True
        health_path = root / "reports" / "health.json"
        health = json.loads(health_path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(health["checked_at"])
        if checked.tzinfo is None or checked.astimezone(timezone(timedelta(hours=8))).date().isoformat() != date:
            return True
        if health.get("report_date") != date or not health.get("sources") or health.get("complete") is not True:
            return True
        # Checkout timestamps do not establish content changes. Compare hashes.
        config_hash = hashlib.sha256((root / "config" / "sources.yaml").read_bytes()).hexdigest()
        return health.get("config_hash") != config_hash
    except (OSError, ValueError, TypeError, KeyError):
        return True


if __name__ == "__main__":
    print("true" if needs_collection(ROOT) else "false")
