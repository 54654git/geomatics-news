"""Collect, score, translate and report geomatics news.

The collector combines official RSS feeds with GDELT's public news search so
that Chinese and marine-surveying coverage is not limited to a few foreign RSS
sites. Translation is best-effort and never prevents a report from being made.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import logging
import re
import sqlite3
import sys
import time
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
except ImportError:  # pragma: no cover
    extract_article = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "sources.yaml"
DEFAULT_DB = ROOT / "data" / "news.db"
DEFAULT_REPORT_DIR = ROOT / "reports"
USER_AGENT = "geomatics-news-student-project/0.2 (+https://github.com/54654git/geomatics-news)"
TRANSLATE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_ENDPOINT = "https://api.mymemory.translated.net/get"
GDELT_LAST_REQUEST = 0.0

KEYWORD_GROUPS: dict[str, tuple[str, ...]] = {
    "海洋测绘": (
        "海洋测绘", "海道测量", "水深测量", "水深", "测深", "多波束", "单波束",
        "声呐", "海底地形", "海图", "海洋空间信息", "bathymetry", "hydrographic",
        "multibeam", "single beam", "sonar", "seafloor mapping", "ocean mapping",
        "underwater survey", "nautical chart",
    ),
    "测绘": ("测绘", "地理测量", "geospatial", "geomatics", "surveying", "mapping", "cartography"),
    "遥感": ("遥感", "remote sensing", "earth observation", "satellite", "卫星", "insar"),
    "摄影测量": ("摄影测量", "photogrammetry", "三维重建", "3d reconstruction", "stereo"),
    "GNSS/北斗": ("gnss", "gps", "beidou", "北斗", "rtk", "定位", "navigation"),
    "激光雷达": ("lidar", "laser scanning", "激光雷达", "点云", "point cloud"),
    "GIS/时空": ("gis", "地理信息", "spatiotemporal", "时空", "digital twin", "数字孪生"),
    "人工智能": ("artificial intelligence", "machine learning", "deep learning", "人工智能", "机器学习"),
}

TRUSTED_SOURCE_HINTS = (
    "nasa", "noaa", "esa", "usgs", "iho", "isprs", "fig", "gov", "edu",
    "nature", "science", "xinhuanet", "people.com", "cctv", "mnr.gov.cn",
    "cnr.cn", "toutiao.com", "news.cn", "chinanews.com.cn", "cas.cn",
)


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
    title_zh: str = ""
    summary_zh: str = ""
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
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--no-content", action="store_true", help="不抓取文章正文，适合调试")
    parser.add_argument("--no-translate", action="store_true", help="不调用翻译服务")
    return parser.parse_args()


def load_sources(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [s for s in data.get("sources", []) if s.get("enabled", True) and s.get("url", s.get("query"))]


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
            title_zh TEXT DEFAULT '',
            summary_zh TEXT DEFAULT '',
            tags TEXT NOT NULL,
            relevance REAL NOT NULL DEFAULT 0,
            marine_relevance REAL NOT NULL DEFAULT 0,
            source_score REAL NOT NULL DEFAULT 0,
            freshness REAL NOT NULL DEFAULT 0,
            collected_at TEXT NOT NULL
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    for name in ("title_zh", "summary_zh"):
        if name not in columns:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at)")
    conn.commit()
    return conn


def normalize_url(url: str) -> str:
    return urldefrag((url or "").strip())[0]


def clean_text(value: str | None) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def has_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def parse_date(entry: Any) -> str | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
    for key in ("published", "updated", "created"):
        if entry.get(key):
            return clean_text(entry[key])
    return None


def parse_gdelt_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return value


def fetch_article_content(url: str, session: requests.Session) -> str:
    if extract_article is None:
        return ""
    try:
        response = session.get(url, timeout=20)
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
        logging.warning("RSS 新闻源访问失败 %s: %s", source["url"], exc)
        return []
    if getattr(parsed, "bozo", False):
        logging.warning("RSS 解析存在格式问题 %s: %s", source["url"], getattr(parsed, "bozo_exception", ""))
    articles: list[Article] = []
    for entry in parsed.entries[: int(source.get("max_items", 30))]:
        title = clean_text(entry.get("title"))
        url = normalize_url(entry.get("link", ""))
        if not title or not url:
            continue
        summary = clean_text(entry.get("summary") or entry.get("description"))
        content = fetch_article_content(url, session) if fetch_content else summary
        articles.append(Article(source["id"], source.get("name", source["id"]), title, url,
                                parse_date(entry), summary, content or summary, list(source.get("tags", []))))
    return articles


def collect_gdelt(source: dict[str, Any], session: requests.Session, fetch_content: bool) -> list[Article]:
    global GDELT_LAST_REQUEST
    delay = 5.2 - (time.monotonic() - GDELT_LAST_REQUEST)
    if delay > 0:
        time.sleep(delay)
    params = {
        "query": source["query"], "mode": "artlist", "format": "json",
        "maxrecords": int(source.get("max_items", 20)), "timespan": "8d", "sort": "HybridRel",
    }
    for attempt in range(2):
        try:
            response = session.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params, timeout=35)
            GDELT_LAST_REQUEST = time.monotonic()
            if response.status_code == 429 and attempt == 0:
                time.sleep(6)
                continue
            response.raise_for_status()
            payload = response.json()
            break
        except (requests.RequestException, ValueError) as exc:
            if attempt == 1:
                logging.warning("GDELT 新闻源访问失败 %s", source.get("id"))
                logging.debug("GDELT 错误详情：%s", exc)
                return []
    articles: list[Article] = []
    for index, item in enumerate(payload.get("articles", [])):
        title = clean_text(item.get("title"))
        url = normalize_url(item.get("url", ""))
        if not title or not url:
            continue
        if source.get("language") == "Chinese" and not has_chinese(title):
            continue
        required_terms = [str(term).lower() for term in source.get("required_terms", [])]
        if required_terms and not any(term in title.lower() for term in required_terms):
            continue
        domain = clean_text(item.get("domain"))
        name = f"{source.get('name', source['id'])} · {domain}" if domain else source.get("name", source["id"])
        summary = clean_text(item.get("snippet") or item.get("description") or "")
        content = fetch_article_content(url, session) if fetch_content and index < int(source.get("max_content_items", 5)) else summary
        articles.append(Article(source["id"], name, title, url, parse_gdelt_date(item.get("seendate")),
                                summary, content or summary, list(source.get("tags", []))))
    return articles


def keyword_hits(text: str, words: Iterable[str]) -> int:
    lowered = text.lower()
    return sum(1 for word in words if word.lower() in lowered)


def score_article(article: Article, now: datetime | None = None) -> Article:
    now = now or datetime.now(timezone.utc)
    body = f"{article.title} {article.summary} {article.content}"
    total_hits = sum(keyword_hits(body, words) for words in KEYWORD_GROUPS.values())
    marine_hits = keyword_hits(body, KEYWORD_GROUPS["海洋测绘"])
    article.relevance = min(1.0, total_hits / 8)
    # Source tags describe the feed, but must not make every unrelated article
    # look marine-relevant. The score is based on article text only.
    article.marine_relevance = min(1.0, marine_hits / 4)
    source_text = f"{article.source_id} {article.source_name} {article.url}".lower()
    article.source_score = 1.0 if any(hint in source_text for hint in TRUSTED_SOURCE_HINTS) else 0.70
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
    return 0.25 * article.relevance + 0.45 * article.marine_relevance + 0.15 * article.source_score + 0.15 * article.freshness


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
            (fingerprint, source_id, source_name, title, url, published_at, summary, content,
             title_zh, summary_zh, tags, relevance, marine_relevance, source_score, freshness, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (article.fingerprint, article.source_id, article.source_name, article.title, article.url,
             article.published_at, article.summary, article.content, article.title_zh, article.summary_zh,
             yaml.safe_dump(article.tags, allow_unicode=True), article.relevance, article.marine_relevance,
             article.source_score, article.freshness, now),
        )
        count += int(result.rowcount > 0)
    conn.commit()
    return count


def collect(config: Path, db: Path, since_days: int, fetch_content: bool) -> int:
    sources = load_sources(config)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/json, text/html;q=0.9"})
    all_articles: list[Article] = []
    for source in sources:
        logging.info("采集 %s", source.get("name", source["id"]))
        if source.get("type", "rss") == "gdelt":
            all_articles.extend(collect_gdelt(source, session, fetch_content))
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


def translate_text(text: str, session: requests.Session, cache: dict[str, str]) -> str:
    text = clean_text(text)
    if not text or has_chinese(text) or text in cache:
        return cache.get(text, text)
    errors: list[str] = []
    try:
        response = session.get(TRANSLATE_ENDPOINT, params={"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text[:4500]}, timeout=15)
        response.raise_for_status()
        data = response.json()
        translated = "".join(part[0] for part in data[0] if part and part[0]).strip()
        if translated and translated != text:
            cache[text] = translated
            return translated
    except (requests.RequestException, ValueError, IndexError, TypeError) as exc:
        errors.append(f"Google: {exc}")
    try:
        response = session.get(MYMEMORY_ENDPOINT, params={"q": text[:1200], "langpair": "en|zh-CN"}, timeout=15)
        response.raise_for_status()
        data = response.json()
        translated = clean_text((data.get("responseData") or {}).get("translatedText"))
        if translated and translated.lower() != text.lower():
            cache[text] = translated
            return translated
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        errors.append(f"MyMemory: {exc}")
    logging.warning("翻译服务不可用，保留原文：%s", "; ".join(errors))
    cache[text] = text
    return text


def rows_for_report(conn: sqlite3.Connection, since_days: int, limit: int) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    return list(conn.execute(
        """SELECT *, (0.25 * relevance + 0.45 * marine_relevance + 0.15 * source_score + 0.15 * freshness) AS score
           FROM articles
           WHERE collected_at >= ? AND (relevance >= 0.15 OR marine_relevance >= 0.15)
           ORDER BY score DESC, published_at DESC LIMIT ?""",
        (cutoff, limit),
    ))


def recalculate_scores(conn: sqlite3.Connection) -> None:
    """Re-score old database rows after a scoring-rule upgrade."""
    rows = conn.execute("SELECT id, source_id, source_name, title, url, published_at, summary, content, tags FROM articles").fetchall()
    for row in rows:
        try:
            tags = yaml.safe_load(row["tags"]) or []
        except yaml.YAMLError:
            tags = []
        article = Article(row["source_id"], row["source_name"], row["title"], row["url"],
                          row["published_at"], row["summary"] or "", row["content"] or "", tags)
        score_article(article)
        conn.execute(
            "UPDATE articles SET relevance = ?, marine_relevance = ?, source_score = ?, freshness = ? WHERE id = ?",
            (article.relevance, article.marine_relevance, article.source_score, article.freshness, row["id"]),
        )
    conn.commit()


def make_report(db: Path, report_dir: Path, since_days: int, limit: int, translate: bool) -> Path:
    conn = init_db(db)
    recalculate_scores(conn)
    rows = rows_for_report(conn, since_days, limit)
    report_dir.mkdir(parents=True, exist_ok=True)
    date_text = datetime.now().astimezone().strftime("%Y-%m-%d")
    output = report_dir / f"weekly-{date_text}.md"
    translator = requests.Session()
    translator.headers.update({"User-Agent": USER_AGENT})
    cache: dict[str, str] = {}
    marine = [row for row in rows if row["marine_relevance"] >= 0.15]
    other = [row for row in rows if row["marine_relevance"] < 0.15]
    lines = [f"# 测绘与海洋测绘新闻周报（{date_text}）", "", f"> 最近 {since_days} 天；海洋测绘相关度权重已提高。英文标题和摘要会尽量附中文翻译。", ""]
    if not rows:
        lines.append("本周期没有采集到新闻。可以检查 GDELT/RSS 新闻源是否可访问。\n")

    def append_section(title: str, selected: list[sqlite3.Row]) -> None:
        if not selected:
            return
        lines.extend([f"## {title}", ""])
        for index, row in enumerate(selected, 1):
            cached_title_zh = row["title_zh"]
            if cached_title_zh == row["title"]:
                cached_title_zh = ""
            title_zh = cached_title_zh or (translate_text(row["title"], translator, cache) if translate else row["title"])
            summary = row["summary"] or row["content"] or ""
            if re.fullmatch(r"\d{8}T\d{6}Z", summary):
                summary = ""
            summary = summary or "暂无摘要（可点击原文查看）"
            summary = re.sub(r"\s+", " ", summary).strip()
            if len(summary) > 500:
                summary = summary[:497] + "..."
            cached_summary_zh = row["summary_zh"]
            if re.fullmatch(r"\d{8}T\d{6}Z", cached_summary_zh or ""):
                cached_summary_zh = ""
            if cached_summary_zh == row["summary"]:
                cached_summary_zh = ""
            summary_zh = cached_summary_zh or (translate_text(summary, translator, cache) if translate else summary)
            conn.execute("UPDATE articles SET title_zh = ?, summary_zh = ? WHERE id = ?", (title_zh, summary_zh, row["id"]))
            tags = ", ".join(yaml.safe_load(row["tags"]) or [])
            lines.extend([
                f"### {index}. {title_zh}", "",
                f"- 原标题：{row['title']}", f"- 来源：{row['source_name']}",
                f"- 日期：{row['published_at'] or '未知'}",
                f"- 评分：{row['score']:.3f}（测绘 {row['relevance']:.2f}，海洋测绘 {row['marine_relevance']:.2f}）",
                f"- 标签：{tags or '待分类'}", f"- 原文：[{row['url']}]({row['url']})", "",
                f"**中文摘要：** {summary_zh}", f"**原文摘要：** {summary}", "",
                "**学习提示：** 重点记录数据源、传感器、坐标参考系、误差来源、算法和可复现数据集。", "",
            ])
        conn.commit()

    append_section("海洋测绘重点", marine)
    append_section("测绘与前沿技术", other)
    output.write_text("\n".join(lines), encoding="utf-8")
    conn.close()
    return output


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command in ("collect", "run"):
        collect(args.config, args.db, args.since_days, not args.no_content)
    if args.command in ("report", "run"):
        path = make_report(args.db, args.report_dir, args.since_days, args.limit, not args.no_translate)
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
