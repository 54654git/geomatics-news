"""Collect, score, translate and report geomatics news.

Domestic coverage uses public news lists and official RSS directly. GDELT
provides optional supplementary search. Translation is best-effort and never
prevents a report from being made.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urldefrag, urljoin, urlsplit
from zoneinfo import ZoneInfo
from lxml import html as lxml_html
from lxml.etree import LxmlError

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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
USER_AGENT = "geomatics-news-student-project/0.3 (+https://github.com/54654git/geomatics-news)"
TRANSLATE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_ENDPOINT = "https://api.mymemory.translated.net/get"
GDELT_LAST_REQUEST = 0.0
GDELT_COOLDOWN_UNTIL = 0.0
REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")

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

# These groups turn the user's long-term study question into transparent,
# inspectable rules.  They are deliberately keyword based so that the weekly
# report remains reproducible without an external model or API key.
POLICY_GROUPS: dict[str, tuple[str, ...]] = {
    "中国十五五与海洋强国": (
        "十五五", "第十五个五年", "海洋强国", "海洋经济", "海洋科技",
        "海洋资源", "海洋生态", "海洋安全", "海洋观测", "海岸带",
        "深海", "海岛", "蓝色经济", "海洋战略", "海洋空间规划", "滨海湿地", "陆海统筹",
    ),
    "国际海洋规划与标准": (
        "ocean decade", "un decade of ocean science", "联合国海洋科学十年",
        "iho", "s-100", "s-102", "seabed 2030", "gebco", "emodnet",
        "digital twin ocean", "mission ocean", "blue economy",
        "marine spatial planning", "ocean mapping strategy", "hydrographic strategy",
    ),
}

FUTURE_GROUPS: dict[str, tuple[str, ...]] = {
    "智能无人测量": (
        "无人船", "无人艇", "自主航行", "自主测量", "无人测绘", "auv", "usv", "uuv",
        "autonomous surface vessel", "autonomous underwater vehicle", "robotic survey",
    ),
    "激光与声学测深": (
        "水深激光雷达", "激光雷达测深", "bathymetric lidar", "bathymetry", "水深测量",
        "多波束", "multibeam", "声呐", "sonar", "海底地形", "seafloor mapping",
    ),
    "遥感与多源融合": (
        "遥感", "卫星测深", "remote sensing", "earth observation", "data fusion",
        "多源融合", "激光雷达", "点云", "point cloud", "gnss/ins", "北斗融合",
    ),
    "智能算法与数字孪生": (
        "人工智能", "机器学习", "深度学习", "machine learning", "deep learning",
        "数字孪生", "digital twin", "时空大数据", "水下三维", "智能解译",
    ),
    "数据标准与精度控制": (
        "s-100", "s-102", "数字水深", "海图标准", "质量控制", "精度评定", "误差",
        "不确定度", "标准化", "dataset", "accuracy assessment", "uncertainty",
    ),
    "国产装备与工程化": (
        "国产化", "自主可控", "自主知识产权", "中试", "产业化", "工程化",
        "海洋装备", "测绘装备", "prototype", "commercialization",
    ),
}

RESEARCH_GROUPS: dict[str, tuple[str, ...]] = {
    "可复现实验与数据": (
        "数据集", "数据处理", "算法", "实验", "验证", "精度", "误差", "论文", "专利",
        "dataset", "algorithm", "experiment", "validation", "accuracy", "benchmark",
    ),
    "传感器与系统集成": (
        "传感器", "激光器", "回波", "光学收发", "信号采集", "组合导航", "系统集成",
        "sensor", "laser", "echo", "signal acquisition", "system integration",
    ),
}

POLICY_CONTEXT_TERMS: tuple[str, ...] = (
    "十五五", "第十五个五年", "规划", "纲要", "方案", "行动计划",
    "海洋战略", "海洋强国",
    "政策", "标准", "路线图", "重点任务", "ocean decade", "ocean strategy",
    "strategic plan", "framework", "roadmap", "ocean policy", "standard", "mission ocean",
    "marine spatial planning", "s-100", "s-102",
)

TRUSTED_SOURCE_DOMAINS = (
    "nasa.gov", "noaa.gov", "esa.int", "usgs.gov", "iho.int", "isprs.org", "fig.net",
    "gov.cn", "edu.cn", "edu", "nature.com", "science.org", "xinhuanet.com",
    "people.com.cn", "cctv.com", "cnr.cn", "news.cn", "chinanews.com.cn", "cas.cn",
    "nmdis.org.cn", "gebco.net",
)


class SourceError(RuntimeError):
    """A source could not be read; distinct from a valid empty search."""


def collection_session() -> requests.Session:
    """Retry temporary DNS/connect failures, but never auto-retry HTTP 429."""
    session = requests.Session()
    retries = Retry(total=2, connect=2, read=1, status=1, backoff_factor=1,
                    status_forcelist=(502, 503, 504), allowed_methods=frozenset({"GET"}),
                    respect_retry_after_header=False)
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "application/rss+xml, application/atom+xml, application/json, text/html;q=0.9"})
    return session


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
    policy_relevance: float = 0.0
    future_relevance: float = 0.0
    research_relevance: float = 0.0
    focus_tags: list[str] | None = None
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
            policy_relevance REAL NOT NULL DEFAULT 0,
            future_relevance REAL NOT NULL DEFAULT 0,
            research_relevance REAL NOT NULL DEFAULT 0,
            focus_tags TEXT NOT NULL DEFAULT '[]',
            source_score REAL NOT NULL DEFAULT 0,
            freshness REAL NOT NULL DEFAULT 0,
            collected_at TEXT NOT NULL
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    for name in ("title_zh", "summary_zh", "policy_relevance", "future_relevance", "research_relevance", "focus_tags"):
        if name not in columns:
            column_type = "REAL" if name.endswith("relevance") else "TEXT"
            default = "0" if column_type == "REAL" else "'[]'"
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {column_type} DEFAULT {default}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at)")
    conn.execute("CREATE TABLE IF NOT EXISTS collection_runs (id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, sources_json TEXT NOT NULL)")
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


def normalized_date(value: str | None) -> str | None:
    """Only accept complete dates; publication dates on Chinese sites use UTC+8."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        match = re.search(r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})", value)
        if not match:
            return None
        try:
            dt = datetime(*map(int, match.groups()))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt.astimezone(timezone.utc).isoformat()


def is_recent(article: Article, since_days: int) -> bool:
    value = normalized_date(article.published_at)
    if not value:
        return False
    dt = datetime.fromisoformat(value)
    now = datetime.now(timezone.utc)
    return now - timedelta(days=since_days) <= dt <= now


def decode_page(response: requests.Response) -> str:
    # Requests defaults to Latin-1 for text/html without an HTTP charset.
    # Let the HTML meta declaration determine GB2312/UTF-8 instead.
    content = response.content
    match = re.search(br'charset\s*=\s*["\x27]?([a-zA-Z0-9_-]+)', content[:5000], re.I)
    encoding = match.group(1).decode("ascii") if match else response.encoding or "utf-8"
    if encoding.lower() in ("iso-8859-1", "latin-1"):
        encoding = "utf-8"
    if encoding.lower() in ("gb2312", "gbk"):
        encoding = "gb18030"
    return content.decode(encoding, errors="replace")


def required_match(source: dict[str, Any], text: str) -> bool:
    terms = source.get("required_terms", [])
    return not terms or any(str(term).lower() in text.lower() for term in terms)


def article_body(text: str) -> str:
    tree = lxml_html.fromstring(text)
    nodes = tree.xpath("//*[@id='content1' or @id='detailContent' or starts-with(@id,'vsb_content') "
                       "or contains(concat(' ',normalize-space(@class),' '),' TRS_Editor ') "
                       "or contains(concat(' ',normalize-space(@class),' '),' bodytext ') "
                       "or contains(concat(' ',normalize-space(@class),' '),' entry-content ') "
                       "or contains(concat(' ',normalize-space(@class),' '),' article-content ')]")
    if nodes:
        body = nodes[0]
        for node in body.xpath(".//script | .//style | .//nav | .//iframe"):
            node.drop_tree()
        return clean_text(body.text_content())
    return clean_text(extract_article(text, include_comments=False, include_tables=False) or "") if extract_article else ""


def collect_html(source: dict[str, Any], session: requests.Session, fetch_content: bool,
                 since_days: int) -> tuple[list[Article], list[str]]:
    """Read bounded public news lists and verify dates on their article pages."""
    response = session.get(source["url"], timeout=25)
    response.raise_for_status()
    tree = lxml_html.fromstring(decode_page(response))
    candidates: dict[str, str] = {}
    article_links = 0
    allowed = source.get("allowed_domains", [urlsplit(source["url"]).hostname])
    for link in tree.xpath(source.get("link_xpath", "//a[@href]")):
        url = normalize_url(urljoin(source["url"], link.get("href", "")))
        host = (urlsplit(url).hostname or "").lower()
        if urlsplit(url).scheme not in ("https", "http") or not any(host == d or host.endswith("." + d) for d in allowed):
            continue
        if not re.search(source["article_url_pattern"], url):
            continue
        article_links += 1
        title_nodes = link.xpath(".//strong | .//h5 | .//h4 | .//h2")
        title = clean_text(link.get("title") or (title_nodes[0].text_content() if title_nodes else link.text_content()))
        if len(title) < 6 or not required_match(source, title):
            continue
        candidates.setdefault(url, title)
    if not article_links:
        raise SourceError("页面没有预期的文章链接，可能发生改版或返回了验证页面")
    articles: list[Article] = []
    errors: list[str] = []
    for url, title in list(candidates.items())[:int(source.get("max_items", 12))]:
        time.sleep(float(source.get("request_interval", 1)))
        try:
            response = session.get(url, timeout=20)
            response.raise_for_status()
            text = decode_page(response)
            detail = lxml_html.fromstring(text)
            meta = {(node.get("name") or node.get("property") or "").lower(): node.get("content", "")
                    for node in detail.xpath("//meta[@content]")}
            date = next((normalized_date(meta[key]) for key in
                         ("publishdate", "pubdate", "article:published_time", "date", "dc.date", "dcterms.created")
                         if normalized_date(meta.get(key))), None)
            if not date:
                for node in detail.xpath(source.get("date_xpath", "//time | //*[@class='time'] | //*[@class='info']")):
                    date = normalized_date(node.get("datetime") or node.text_content())
                    if date:
                        break
            # Never substitute a footer year, URL date, or collection time for publication time.
            summary = clean_text(meta.get("description") or meta.get("og:description"))
            article = Article(source["id"], source.get("name", source["id"]), title, url,
                              date, summary, "", list(source.get("tags", [])))
            if not date:
                errors.append(f"无法确认发布日期，已跳过：{url}")
            if not is_recent(article, since_days):
                continue
            if fetch_content:
                article.content = article_body(text)
            article.content = article.content or summary
            articles.append(article)
        except (requests.RequestException, ValueError, LxmlError) as exc:
            errors.append(f"{url}: {exc}")
    return articles, errors


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
        return article_body(decode_page(response))
    except (requests.RequestException, ValueError) as exc:
        logging.warning("正文抓取失败 %s: %s", url, exc)
        return ""


def collect_feed(source: dict[str, Any], session: requests.Session, fetch_content: bool,
                 since_days: int = 8) -> list[Article]:
    try:
        response = session.get(source["url"], timeout=30)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except requests.RequestException as exc:
        raise SourceError(str(exc)) from exc
    if not parsed.entries:
        raise SourceError("响应中没有 RSS 条目，入口可能已失效")
    if getattr(parsed, "bozo", False):
        logging.warning("RSS 解析存在格式问题 %s: %s", source["url"], getattr(parsed, "bozo_exception", ""))
    articles: list[Article] = []
    for entry in parsed.entries[: int(source.get("max_items", 30))]:
        title = clean_text(entry.get("title"))
        url = normalize_url(entry.get("link", ""))
        if not title or not url:
            continue
        summary = clean_text(entry.get("summary") or entry.get("description"))
        if not required_match(source, title + " " + summary):
            continue
        article = Article(source["id"], source.get("name", source["id"]), title, url,
                          parse_date(entry), summary, summary, list(source.get("tags", [])))
        # Do not download full archives simply to discard old articles later.
        if not is_recent(article, since_days):
            continue
        # Some official feeds include the complete article (content:encoded).
        # Prefer that text to an abbreviated webpage or an unnecessary request.
        feed_content = clean_text(" ".join(part.get("value", "") for part in entry.get("content", [])))
        if fetch_content:
            article.content = feed_content or fetch_article_content(url, session) or summary
        articles.append(article)
    return articles


def collect_gdelt(source: dict[str, Any], session: requests.Session, fetch_content: bool,
                  since_days: int = 8) -> list[Article]:
    global GDELT_LAST_REQUEST, GDELT_COOLDOWN_UNTIL
    if time.monotonic() < GDELT_COOLDOWN_UNTIL:
        raise SourceError("GDELT HTTP 429 冷却中，本轮跳过后续查询；直连来源继续采集")
    delay = 10.0 - (time.monotonic() - GDELT_LAST_REQUEST)
    if delay > 0:
        time.sleep(delay)
    params = {
        "query": source["query"], "mode": "artlist", "format": "json",
        "maxrecords": int(source.get("max_items", 20)), "timespan": f"{since_days}d", "sort": "HybridRel",
    }
    for attempt in range(2):
        try:
            response = session.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params, timeout=35)
            GDELT_LAST_REQUEST = time.monotonic()
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "60")
                cooldown = int(retry_after) if retry_after.isdigit() else 60
                GDELT_COOLDOWN_UNTIL = time.monotonic() + max(60, cooldown)
                raise SourceError("GDELT HTTP 429 服务端限流，已进入冷却；直连来源继续采集")
            response.raise_for_status()
            if not response.text.lstrip().startswith("{"):
                raise SourceError("GDELT 返回非 JSON 响应：" + clean_text(response.text)[:160])
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("articles", []), list):
                raise SourceError("GDELT 返回了不支持的数据结构")
            break
        except (requests.RequestException, ValueError) as exc:
            if attempt == 1:
                raise SourceError(str(exc)) from exc
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
    # The policy phrase 向海图强 does not mean nautical chart (海图).
    lowered = text.lower().replace("向海图强", "海洋发展")
    hits = 0
    for word in words:
        term = word.lower()
        if term.isascii():
            hits += bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", lowered))
        else:
            hits += term in lowered
    return hits


def matched_groups(text: str, groups: dict[str, tuple[str, ...]]) -> tuple[int, list[str]]:
    """Return the number of matched groups and human-readable focus labels."""
    hits = 0
    labels: list[str] = []
    for label, words in groups.items():
        if keyword_hits(text, words):
            hits += 1
            labels.append(label)
    return hits, labels


def score_article(article: Article, now: datetime | None = None) -> Article:
    now = now or datetime.now(timezone.utc)
    body = f"{article.title} {article.summary} {article.content}"
    # Long article bodies often contain related-news lists and footer text.
    # Policy classification therefore uses the title and publisher summary,
    # while technical/research classification can use the full body.
    policy_body = f"{article.title} {article.summary}"
    total_hits = sum(keyword_hits(body, words) for words in KEYWORD_GROUPS.values())
    marine_hits = keyword_hits(body, KEYWORD_GROUPS["海洋测绘"])
    policy_hits, policy_labels = matched_groups(policy_body, POLICY_GROUPS)
    # A broad phrase such as “海洋经济合作” is useful context but is not by
    # itself evidence of a national or international plan. Require a planning,
    # strategy, standards, or policy signal before putting an item in that
    # report section.
    if policy_hits and not keyword_hits(policy_body, POLICY_CONTEXT_TERMS):
        policy_hits, policy_labels = 0, []
    future_hits, future_labels = matched_groups(body, FUTURE_GROUPS)
    research_hits, research_labels = matched_groups(body, RESEARCH_GROUPS)
    article.relevance = min(1.0, total_hits / 8)
    # Source tags describe the feed, but must not make every unrelated article
    # look marine-relevant. The score is based on article text only.
    article.marine_relevance = min(1.0, marine_hits / 4)
    article.policy_relevance = min(1.0, policy_hits / 2)
    article.future_relevance = min(1.0, future_hits / 3)
    article.research_relevance = min(1.0, research_hits / 2)
    article.focus_tags = policy_labels + future_labels + research_labels
    host = (urlsplit(article.url).hostname or "").lower()
    article.source_score = 1.0 if any(host == d or host.endswith("." + d) for d in TRUSTED_SOURCE_DOMAINS) else 0.70
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
    return (
        0.10 * article.relevance
        + 0.25 * article.marine_relevance
        + 0.20 * article.policy_relevance
        + 0.25 * article.future_relevance
        + 0.10 * article.research_relevance
        + 0.05 * article.source_score
        + 0.05 * article.freshness
    )


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
             title_zh, summary_zh, tags, relevance, marine_relevance, policy_relevance,
             future_relevance, research_relevance, focus_tags, source_score, freshness, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (article.fingerprint, article.source_id, article.source_name, article.title, article.url,
             article.published_at, article.summary, article.content, article.title_zh, article.summary_zh,
             yaml.safe_dump(article.tags, allow_unicode=True), article.relevance, article.marine_relevance,
             article.policy_relevance, article.future_relevance, article.research_relevance,
             yaml.safe_dump(article.focus_tags or [], allow_unicode=True),
             article.source_score, article.freshness, now),
        )
        count += int(result.rowcount > 0)
    conn.commit()
    return count


def collect(config: Path, db: Path, since_days: int, fetch_content: bool) -> int:
    sources = load_sources(config)
    session = collection_session()
    all_articles: list[Article] = []
    statuses: list[dict[str, Any]] = []
    for source in sources:
        logging.info("采集 %s", source.get("name", source["id"]))
        status = {"id": source["id"], "name": source.get("name", source["id"]), "status": "正常",
                  "candidates": 0, "recent": 0, "detail": ""}
        try:
            kind = source.get("type", "rss")
            if kind == "gdelt":
                items = collect_gdelt(source, session, fetch_content, since_days)
            elif kind == "html":
                items, errors = collect_html(source, session, fetch_content, since_days)
                if errors:
                    status.update(status="部分失败", detail="; ".join(errors[:2])[:250])
                    logging.warning("来源部分失败 %s: %s", source["id"], status["detail"])
            elif kind == "rss":
                items = collect_feed(source, session, fetch_content, since_days)
            else:
                raise SourceError(f"不支持的新闻源类型：{kind}")
            status["candidates"] = len(items)
            status["recent"] = sum(is_recent(a, since_days) for a in items)
            all_articles.extend(items)
        except (SourceError, requests.RequestException, ValueError, LxmlError) as exc:
            status.update(status="失败", detail=clean_text(str(exc))[:250])
            logging.warning("新闻源失败 %s: %s", source["id"], exc)
        statuses.append(status)
        logging.info("来源结果 %s: %s，近期 %d 条", source["id"], status["status"], status["recent"])
    recent: list[Article] = []
    for article in all_articles:
        score_article(article)
        if is_recent(article, since_days):
            article.published_at = normalized_date(article.published_at)
            recent.append(article)
    conn = init_db(db)
    inserted = save_articles(conn, deduplicate(recent))
    conn.execute("INSERT INTO collection_runs (checked_at, sources_json) VALUES (?, ?)",
                 (datetime.now(timezone.utc).isoformat(), json.dumps(statuses, ensure_ascii=False)))
    conn.commit()
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
        """SELECT *, (
             0.10 * relevance + 0.25 * marine_relevance + 0.20 * policy_relevance
             + 0.25 * future_relevance + 0.10 * research_relevance
             + 0.05 * source_score + 0.05 * freshness
           ) AS score
           FROM articles
           WHERE published_at >= ? AND published_at <= ? AND (
             relevance >= 0.15 OR marine_relevance >= 0.15 OR policy_relevance >= 0.15
             OR future_relevance >= 0.15 OR research_relevance >= 0.15
           )
           ORDER BY score DESC, published_at DESC LIMIT ?""",
        (cutoff, datetime.now(timezone.utc).isoformat(), limit),
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
            """UPDATE articles SET relevance = ?, marine_relevance = ?, policy_relevance = ?,
               future_relevance = ?, research_relevance = ?, focus_tags = ?,
               source_score = ?, freshness = ? WHERE id = ?""",
            (article.relevance, article.marine_relevance, article.policy_relevance,
             article.future_relevance, article.research_relevance,
             yaml.safe_dump(article.focus_tags or [], allow_unicode=True),
             article.source_score, article.freshness, row["id"]),
        )
    conn.commit()


def make_report(db: Path, report_dir: Path, since_days: int, limit: int, translate: bool) -> Path:
    conn = init_db(db)
    recalculate_scores(conn)
    rows = rows_for_report(conn, since_days, limit)
    report_dir.mkdir(parents=True, exist_ok=True)
    date_text = datetime.now(REPORT_TIMEZONE).strftime("%Y-%m-%d")
    output = report_dir / f"weekly-{date_text}.md"
    translator = requests.Session()
    translator.headers.update({"User-Agent": USER_AGENT})
    cache: dict[str, str] = {}
    policy = [row for row in rows if row["policy_relevance"] >= 0.25]
    future = [row for row in rows if row["policy_relevance"] < 0.25 and row["future_relevance"] >= 0.25]
    research = [row for row in rows if row["policy_relevance"] < 0.25 and row["future_relevance"] < 0.25 and row["research_relevance"] >= 0.25]
    marine = [row for row in rows if row["policy_relevance"] < 0.25 and row["future_relevance"] < 0.25 and row["research_relevance"] < 0.25 and row["marine_relevance"] >= 0.15]
    other = [row for row in rows if row not in policy + future + research + marine]
    lines = [
        f"# 测绘与海洋测绘新闻周报（{date_text}）", "",
        f"> 最近 {since_days} 天；本周报按海洋测绘、十五五/国际规划、未来技术和科研价值筛选。",
        "> 规划关键词用于发现政策信号，不能替代对原始规划文件的正式解读。", "",
    ]
    last_run = conn.execute("SELECT checked_at, sources_json FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()
    if last_run:
        statuses = json.loads(last_run["sources_json"])
        incomplete = [s["name"] for s in statuses if s["status"] != "正常"]
        lines.extend([f"> 本期入选 {len(rows)} 条；可包含数据库中仍在日期窗口内的已收录新闻。", ""])
        if incomplete:
            lines.extend(["> **采集覆盖不完整：** " + "、".join(incomplete) + "。详情见文末来源状态。", ""])
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
            try:
                focus_tags = ", ".join(yaml.safe_load(row["focus_tags"]) or [])
            except yaml.YAMLError:
                focus_tags = ""
            if not focus_tags:
                focus_tags = "常规测绘新闻"
            if row["policy_relevance"] >= 0.25:
                study_hint = "先保存政策原文，记录目标、时间表、重点工程和对海洋测绘的需求。"
            elif row["future_relevance"] >= 0.25:
                study_hint = "记录传感器、数据源、算法、坐标基准、误差指标和可复现实验入口。"
            elif row["research_relevance"] >= 0.25:
                study_hint = "优先提取论文、数据集、实验方法和精度评价，作为课程项目或论文复现线索。"
            else:
                study_hint = "判断它是否能转化为公开数据分析、算法复现或海洋测量案例。"
            lines.extend([
                f"### {index}. {title_zh}", "",
                f"- 原标题：{row['title']}", f"- 来源：{row['source_name']}",
                f"- 日期：{row['published_at'] or '未知'}",
                f"- 评分：{row['score']:.3f}（海洋测绘 {row['marine_relevance']:.2f}，规划 {row['policy_relevance']:.2f}，未来技术 {row['future_relevance']:.2f}，科研价值 {row['research_relevance']:.2f}）",
                f"- 方向标签：{focus_tags}", f"- 来源标签：{tags or '待分类'}", f"- 原文：[{row['url']}]({row['url']})", "",
                f"**中文摘要：** {summary_zh}", f"**原文摘要：** {summary}", "",
                f"**面向保研的学习提示：** {study_hint}", "",
            ])
        conn.commit()

    append_section("十五五与国际海洋规划信号", policy)
    append_section("未来海洋测绘技术", future)
    append_section("科研与保研高价值线索", research)
    append_section("海洋测绘重点", marine)
    append_section("测绘与前沿技术", other)
    if last_run:
        lines.extend(["## 新闻源采集状态", "", f"采集时间：{last_run['checked_at']}", "",
                      "| 来源 | 状态 | 近期条数（入选前） | 说明 |", "| --- | --- | ---: | --- |"])
        for status in json.loads(last_run["sources_json"]):
            detail = status["detail"].replace("|", "/") or "已读取，仍需通过专业评分和去重"
            lines.append(f"| {status['name']} | {status['status']} | {status['recent']} | {detail} |")
        lines.extend(["", "“正常但 0 条”表示本次没有符合日期、关键词的条目；“失败”表示该来源覆盖不完整。条数不代表周报入选数量。", ""])
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
