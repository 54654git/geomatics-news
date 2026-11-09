"""Report identity and content version; standard library only (pre-install safe)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib

BEIJING = timezone(timedelta(hours=8))


def report_period(now=None, week_of=None):
    now = now or datetime.now(BEIJING)
    day = datetime.fromisoformat(week_of).date() if week_of else now.astimezone(BEIJING).date()
    monday = day - timedelta(days=day.weekday())
    end = datetime.combine(monday, datetime.min.time(), BEIJING)
    start = end - timedelta(days=7)
    return {"id": monday.isoformat(), "start": start.isoformat(), "end": end.isoformat()}


def content_version(root: Path):
    files = [root / 'config' / 'sources.yaml']
    files += sorted((root / 'src').glob('*.py'))
    files += sorted((root / 'scripts').glob('*.py'))
    files += sorted((root / 'config').glob('*.json'))
    digest = hashlib.sha256()
    for path in files:
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes().replace(b'\r\n',b'\n'))
    return digest.hexdigest()


def contains(value, period):
    if not value:
        return False
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return datetime.fromisoformat(period['start']) <= dt < datetime.fromisoformat(period['end'])
    except (ValueError, TypeError):
        return False
