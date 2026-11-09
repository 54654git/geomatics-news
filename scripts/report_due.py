"""One report identity for every trigger; bounded retries, no weekday skip."""
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.periods import report_period, content_version


def needs_collection(root: Path, now=None, week_of=None, force=False):
    if force:return True
    period=report_period(now,week_of)
    try:
        path=root/'reports'/f"weekly-{period['id']}.md"
        if not path.is_file() or not path.stat().st_size:return True
        health=json.loads((root/'reports'/'health.json').read_text(encoding='utf-8'))
        if health.get('period_id')!=period['id']:return True
        if health.get('content_version')!=content_version(root):return True
        if health.get('complete') is True:
            return bool(health.get('supplement_degraded') and int(health.get('attempts',0))<2)
        return int(health.get('attempts',0))<3
    except (OSError,ValueError,TypeError,KeyError):return True


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--week-of')
    parser.add_argument('--force',action='store_true')
    args=parser.parse_args()
    print('true' if needs_collection(ROOT,week_of=args.week_of,force=args.force) else 'false')
