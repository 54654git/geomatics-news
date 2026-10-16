"""Collect, score, deduplicate and report geomatics news.

The program intentionally starts with public RSS/Atom feeds. HTML extraction is
available as an opt-in fallback for a configured source, but it should be used
only where the site's terms and robots policy allow it.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urldefrag

import feedparser
import requests
import yaml
from rapidfuzz.fuzz import ratio

try:
    from trafilatura import extract as extract_article
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    extract_article = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "sources.yaml"
DEFAULT_DB = ROOT / "data" / "news.db"
DEFAULT_REPORT_DIR = ROOT / "reports"
USER_AGENT = "geomatics-news-student-project/0.1 (+https://github.com/)"

KEYWORD_GROUPS: dict[str, tuple[str, ...]] = {
    "测绘": ("测绘", "geospatial", "geomatics", "surveying", "mapping", "cartography"),
    "海洋测绘": (
        "海洋测绘", "水深", "海道测量", "bathymetry", "hydrographic", "multibeam",
        "sonar", "声呐", "海底地形", "underwater", "ocean mapping",
    ),
    "遥感": ("遥感", "remote sensing", "earth observation", "satellite", "卫星", "insar"),
    "摄影测量": ("摄影测量", "photogrammetry", "三维重建", "3d reconstruction", "stereo"),
    "GNSS/北斗": ("gnss", "gps", "beidou", "北斗", "rtk", "定位", "navigation"),
    "激光雷达": ("lidar", "laser scanning", "点云", "point cloud"),
    "GIS/时空": ("gis", "地理信息", "spatiotemporal", "时空", "digital twin", "数字孪生"),
    "人工智能": ("artificial intelligence", "machine learning", "deep learning", "ai", "人工智能", "机器学习"),
}

TRUSTED_SOURCE_HINTS = ("nasa", "noaa", "esa", "usgs", "iho", "isprs", "fig", "gov", "edu")


@dataclass
class Article:
    source_id: str
    source_name: str
    title: str
    url: str
    published_at: str | None
    summary: str
    content: str
    tags: list[str]
    relevance: float = 0.0
    marine_relevance: float = 0.0
    source_score: float = 0.0
    freshness: float = 0.0

    @property
    def fingerprint(self) -> str:
        normalized = re.sub(r"\W+", " ", f"{self.title} {self.url}".lower()).strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="测绘与海洋测绘新闻采集器")
    parser.add_argument("command", choices=("collect", "report", "run"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--since-days", type=int, default=8)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--no-content", action="store_true", help="不抓取文章正文，适合调试")
    return parser.parse_args()


def load_sources(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [s for s in data.get("sources", []) if s.get("enabled", True) and s.get("url")]


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY,
            fingerprint TEXT UNIQUE NOT NULL,
            source_id TEXT NOT NULL,
            source_name TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            published_at TEXT,
            summary TEXT,
            content TEXT,
            tags TEXT NOT NULL,
            relevance REAL NOT NULL,
            marine_relevance REAL NOT NULL,
            source_score REAL NOT NULL,
            freshness REAL NOT NULL,
            collected_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at)")
    conn.commit()
    return conn


def normalize_url(url: str) -> str:
    return urldefrag((url or "").strip())[0]


def clean_text(value: str | None) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_date(entry: Any) -> str | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
    for key in ("published", "updated", "created"):
        if entry.get(key):
            return clean_text(entry[key])
    return None


def fetch_article_content(url: str, session: requests.Session) -> str:
    if extract_article is None:
        return ""
    try:
        response = session.get(url, timeout=25)
        response.raise_for_status()
        return clean_text(extract_article(response.text, include_comments=False, include_tables=False) or "")
    except requests.RequestException as exc:
        logging.warning("正文抓取失败 %s: %s", url, exc)
        return ""


def collect_feed(source: dict[str, Any], session: requests.Session, fetch_content: bool) -> list[Article]:
    try:
        response = session.get(source["url"], timeout=30)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except requests.RequestException as exc:
        logging.warning("新闻源访问失败 %s: %s", source["url"], exc)
        return []
    if getattr(parsed, "bozo", False):
        logging.warning("RSS 解析存在格式问题 %s: %s", source["url"], getattr(parsed, "bozo_exception", ""))

    articles: list[Article] = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title"))
        url = normalize_url(entry.get("link", ""))
        if not title or not url:
            continue
        summary = clean_text(entry.get("summary") or entry.get("description"))
        content = summary
        if fetch_content:
            content = fetch_article_content(url, session) or summary
        articles.append(Article(
            source_id=source["id"], source_name=source.get("name", source["id"]),
            title=title, url=url, published_at=parse_date(entry), summary=summary,
            content=content, tags=list(source.get("tags", [])),
        ))
    return articles


def collect_html(source: dict[str, Any], session: requests.Session) -> list[Article]:
    """Lightweight opt-in fallback; use RSS whenever possible."""
    try:
        response = session.get(source["url"], timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        logging.warning("HTML 新闻源访问失败 %s: %s", source["url"], exc)
        return []
    title = clean_text(re.search(r"<title[^>]*>(.*?)</title>", response.text, re.I | re.S).group(1) if re.search(r"<title[^>]*>(.*?)</title>", response.text, re.I | re.S) else source.get("name", source["id"]))
    content = clean_text(response.text)[:5000]
    return [Article(source["id"], source.get("name", source["id"]), title, source["url"], None, content[:500], content, list(source.get("tags", [])))]


def keyword_hits(text: str, words: Iterable[str]) -> int:
    lowered = text.lower()
    return sum(1 for word in words if word.lower() in lowered)


def score_article(article: Article, now: datetime | None = None) -> Article:
    now = now or datetime.now(timezone.utc)
    text = f"{article.title} {article.summary} {article.content} {' '.join(article.tags)}"
    total_hits = sum(keyword_hits(text, words) for words in KEYWORD_GROUPS.values())
    marine_hits = keyword_hits(text, KEYWORD_GROUPS["海洋测绘"])
    article.relevance = min(1.0, total_hits / 8)
    article.marine_relevance = min(1.0, marine_hits / 4)
    source_text = f"{article.source_id} {article.source_name} {article.url}".lower()
    article.source_score = 1.0 if any(hint in source_text for hint in TRUSTED_SOURCE_HINTS) else 0.65
    article.freshness = 0.5
    if article.published_at:
        try:
            dt = datetime.fromisoformat(article.published_at.replace("Z", "+00:00"))
            age = max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds() / 86400)
            article.freshness = max(0.0, min(1.0, 1.0 - age / 14))
        except ValueError:
            pass
    return article


def total_score(article: Article) -> float:
    return 0.35 * article.relevance + 0.25 * article.marine_relevance + 0.20 * article.source_score + 0.20 * article.freshness


def deduplicate(articles: list[Article]) -> list[Article]:
    kept: list[Article] = []
    for article in sorted(articles, key=total_score, reverse=True):
        if any(ratio(article.title.lower(), old.title.lower()) >= 88 for old in kept):
            continue
        kept.append(article)
    return kept


def save_articles(conn: sqlite3.Connection, articles: Iterable[Article]) -> int:
    count = 0
    now = datetime.now(timezone.utc).isoformat()
    for article in articles:
        result = conn.execute(
            """INSERT OR IGNORE INTO articles
            (fingerprint, source_id, source_name, title, url, published_at, summary, content, tags,
             relevance, marine_relevance, source_score, freshness, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (article.fingerprint, article.source_id, article.source_name, article.title, article.url,
             article.published_at, article.summary, article.content, yaml.safe_dump(article.tags, allow_unicode=True),
             article.relevance, article.marine_relevance, article.source_score, article.freshness, now),
        )
        count += int(result.rowcount > 0)
    conn.commit()
    return count


def collect(config: Path, db: Path, since_days: int, fetch_content: bool) -> int:
    sources = load_sources(config)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, text/html;q=0.9"})
    all_articles: list[Article] = []
    for source in sources:
        logging.info("采集 %s", source.get("name", source["id"]))
        if source.get("type", "rss") == "html":
            all_articles.extend(collect_html(source, session))
        else:
            all_articles.extend(collect_feed(source, session, fetch_content))
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    recent: list[Article] = []
    for article in all_articles:
        score_article(article)
        if article.published_at:
            try:
                if datetime.fromisoformat(article.published_at.replace("Z", "+00:00")).astimezone(timezone.utc) < cutoff:
                    continue
            except ValueError:
                pass
        recent.append(article)
    conn = init_db(db)
    inserted = save_articles(conn, deduplicate(recent))
    conn.close()
    logging.info("本次发现 %d 条，新增 %d 条", len(recent), inserted)
    return inserted


def rows_for_report(conn: sqlite3.Connection, since_days: int, limit: int) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    return list(conn.execute(
        """SELECT *, (0.35 * relevance + 0.25 * marine_relevance + 0.20 * source_score + 0.20 * freshness) AS score
           FROM articles WHERE collected_at >= ? ORDER BY score DESC, published_at DESC LIMIT ?""",
        (cutoff, limit),
    ))


def make_report(db: Path, report_dir: Path, since_days: int, limit: int) -> Path:
    conn = init_db(db)
    rows = rows_for_report(conn, since_days, limit)
    report_dir.mkdir(parents=True, exist_ok=True)
    date_text = datetime.now().astimezone().strftime("%Y-%m-%d")
    output = report_dir / f"weekly-{date_text}.md"
    lines = [f"# 测绘与海洋测绘新闻周报（{date_text}）", "", f"> 最近 {since_days} 天；按专业相关度、海洋测绘相关度、来源和新近程度排序。", ""]
    if not rows:
        lines.append("本周期没有采集到符合条件的新文章。可以检查 `config/sources.yaml` 中的新闻源。\n")
    for index, row in enumerate(rows, 1):
        tags = ", ".join(yaml.safe_load(row["tags"]) or [])
        summary = row["summary"] or row["content"] or "暂无摘要"
        summary = re.sub(r"\s+", " ", summary).strip()
        if len(summary) > 400:
            summary = summary[:397] + "..."
        lines.extend([
            f"## {index}. {row['title']}", "",
            f"- 来源：{row['source_name']}", f"- 日期：{row['published_at'] or '未知'}",
            f"- 评分：{row['score']:.3f}（测绘 {row['relevance']:.2f}，海洋测绘 {row['marine_relevance']:.2f}）",
            f"- 标签：{tags or '待分类'}", f"- 原文：[{row['url']}]({row['url']})", "",
            f"**摘要：** {summary}", "",
            "**学习提示：** 阅读原文时，可以记录使用的数据源、传感器、坐标参考系、误差来源和算法关键词。", "",
        ])
    output.write_text("\n".join(lines), encoding="utf-8")
    conn.close()
    return output


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command in ("collect", "run"):
        collect(args.config, args.db, args.since_days, not args.no_content)
    if args.command in ("report", "run"):
        path = make_report(args.db, args.report_dir, args.since_days, args.limit)
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

