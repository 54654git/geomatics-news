"""Collect, score, translate and report geomatics news.

Domestic coverage uses public news lists and official RSS directly. Google News
RSS provides optional wide-angle search; GDELT is retained as a disabled
fallback. Translation is best-effort and never prevents a report from being made.
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
from urllib.parse import urldefrag, urljoin, urlsplit, parse_qsl, urlencode, urlunsplit
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
    from . import editorial as ed
    from .periods import report_period, contains, content_version
except ImportError:
    import editorial as ed
    from periods import report_period, contains, content_version

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
GOOGLE_NEWS_ENDPOINT = "https://news.google.com/rss/search"
# Ceiling on how many items one source may contribute to a single report.
MAX_ITEMS_PER_SOURCE = 3
# Below this the per-source cap is relaxed; a very quiet week still needs a report.
MIN_REPORT_ITEMS = 8
REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")
ACTIVE_PERIOD = None
DETAIL_CACHE: dict[str, dict] = {}
PUBLISHER_INDEX: dict[str, list] = {}
PUBLISHER_FORMS: dict[str, tuple | None] = {}
PUBLISHER_SEARCHES = 0

KEYWORD_GROUPS: dict[str, tuple[str, ...]] = {
    "海洋测绘": (
        "海洋测绘", "海道测量", "水深测量", "水深", "测深", "多波束", "单波束",
        "声呐", "海底地形", "海图", "海洋空间信息", "bathymetry", "hydrographic",
        "multibeam", "single beam", "sonar", "seafloor mapping", "ocean mapping",
        "underwater survey", "nautical chart",
        "hydrography", "hydrographic survey", "bathymetric", "echo sounder",
        "side-scan sonar", "swath", "seabed mapping", "chart datum",
        "water depth", "depth sounding", "seabed geodetic",
        # Equipment and project wording: without these, a survey vessel contract
        # or a seabed survey campaign scores zero and never reaches the report.
        "测量船", "勘察", "海山", "海洋调查", "水下测绘", "survey vessel",
        "seabed survey", "site survey", "ocean survey",
    ),
    "测绘": ("测绘", "地理测量", "空间地理信息", "geospatial", "geomatics", "surveying", "mapping", "cartography",
             "geodesy", "geodetic", "land survey", "map production", "spatial data"),
    "遥感": ("遥感", "remote sensing", "earth observation", "satellite", "卫星", "insar",
             "satellite altimetry", "ocean colour", "ocean color", "sar imagery"),
    "摄影测量": ("摄影测量", "photogrammetry", "三维重建", "3d reconstruction", "stereo"),
    "GNSS/北斗": ("gnss", "gps", "beidou", "北斗", "rtk", "定位", "navigation",
                  "precise positioning", "ppp", "gnss-r"),
    "激光雷达": ("lidar", "laser scanning", "激光雷达", "点云", "point cloud",
                 "bathymetric lidar", "airborne lidar"),
    "GIS/时空": ("gis", "地理信息", "spatiotemporal", "时空", "digital twin", "数字孪生",
                 "spatial data infrastructure", "geodatabase"),
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
        "eez", "exclusive economic zone", "marine policy", "ocean accounting",
    ),
}

FUTURE_GROUPS: dict[str, tuple[str, ...]] = {
    "智能无人测量": (
        "无人船", "无人艇", "自主航行", "自主测量", "无人测绘", "auv", "usv", "uuv",
        "autonomous surface vessel", "autonomous underwater vehicle", "robotic survey",
        "uncrewed surface vessel", "unmanned survey vessel", "autonomous survey", "rov",
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
        "s-100", "s-102", "s-101", "s-57", "enc", "electronic navigational chart",
        "数字水深", "海图标准", "质量控制", "精度评定", "误差",
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
    "nmdis.org.cn", "gebco.net", "fio.org.cn", "seabed2030.org",
    "pric.org.cn", "sio.org.cn", "csgpc.org", "schmidtocean.org",
    "copernicus.eu", "ec.europa.eu",
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
    content_type: str = "行业资讯"
    canonical_url: str = ""
    publisher_domain: str = ""
    date_basis: str = "来源发布日期"

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
    parser.add_argument("--week-of", help="报告所属周的周一，例如 2026-11-09；默认本周一")
    parser.add_argument("--rolling", action="store_true", help="调试：使用滚动天数而非完整自然周")
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
    for name in ("title_zh", "summary_zh", "policy_relevance", "future_relevance", "research_relevance", "focus_tags", "content_type"):
        if name not in columns:
            column_type = "REAL" if name.endswith("relevance") else "TEXT"
            default = "0" if column_type == "REAL" else "'[]'"
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {column_type} DEFAULT {default}")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    for name in ('canonical_url', 'publisher_domain', 'date_basis', 'body_status', 'eligibility_reason', 'rule_version'):
        if name not in columns:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at)")
    conn.execute("CREATE TABLE IF NOT EXISTS collection_runs (id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, sources_json TEXT NOT NULL)")
    if "config_hash" not in {row[1] for row in conn.execute("PRAGMA table_info(collection_runs)")}:
        conn.execute("ALTER TABLE collection_runs ADD COLUMN config_hash TEXT DEFAULT ''")
    conn.execute("CREATE TABLE IF NOT EXISTS report_items (report_date TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, PRIMARY KEY(report_date, url))")
    conn.commit()
    return conn


def normalize_url(url: str) -> str:
    parts = urlsplit(urldefrag((url or "").strip())[0])
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))


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
    if ACTIVE_PERIOD:
        return contains(value, ACTIVE_PERIOD)
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


def metric(source, key, count=1):
    metrics = source.setdefault('_metrics', {})
    metrics[key] = metrics.get(key, 0) + count


def detail_metadata(text, base):
    tree = lxml_html.fromstring(text)
    meta = {(n.get('name') or n.get('property') or '').lower(): n.get('content','') for n in tree.xpath('//meta[@content]')}
    date = next((normalized_date(meta[k]) for k in ('publishdate','pubdate','article:published_time','date','dc.date','dcterms.created') if normalized_date(meta.get(k))), None)
    if not date:
        for n in tree.xpath('//time[@datetime] | //*[@class="arti_update"] | //*[@class="singlepage-time"]'):
            date = normalized_date(n.get('datetime') or n.text_content())
            if date:break
    # JSON-LD may hold the original date even when Google exposes a refreshed timestamp.
    if not date:
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', text)
        if match:date = normalized_date(match[1])
    canonical = tree.xpath('//link[@rel="canonical"]/@href')
    candidate = urljoin(base, canonical[0]) if canonical else base
    if ed.publisher_domain(candidate) != ed.publisher_domain(base):candidate = base
    return {'date':date, 'canonical':normalize_url(candidate),
            'summary':clean_text(meta.get('description') or meta.get('og:description')), 'tree':tree}


def article_body(text: str) -> str:
    tree = lxml_html.fromstring(text)
    nodes = tree.xpath("//*[@id='content1' or @id='detailContent' or starts-with(@id,'vsb_content') "
                       "or contains(concat(' ',normalize-space(@class),' '),' TRS_Editor ') "
                       "or contains(concat(' ',normalize-space(@class),' '),' bodytext ') "
                       "or contains(concat(' ',normalize-space(@class),' '),' myart ') "
                       "or contains(concat(' ',normalize-space(@class),' '),' entry-content ') "
                       "or contains(concat(' ',normalize-space(@class),' '),' article-content ')]")
    if nodes:
        body = nodes[0]
        for node in body.xpath(".//script | .//style | .//nav | .//iframe | .//aside | "
                               ".//*[contains(@class,'related-post') or contains(@class,'related-news') or contains(@class,'newsletter')]"):
            node.drop_tree()
        resources = [a.get("href", "") for a in body.xpath(".//a[@href]")
                     if ed.RESOURCE.match(a.get("href", ""))]
        return clean_text(body.text_content()) + (" 资源链接：" + " ".join(dict.fromkeys(resources)) if resources else "")
    content = clean_text(extract_article(text, include_comments=False, include_tables=False) or "") if extract_article else ""
    resources = [a.get('href') for a in tree.xpath('//a[@href]') if ed.RESOURCE.match(a.get('href',''))]
    return content + (' 资源链接：' + ' '.join(dict.fromkeys(resources)) if resources else '')


def collect_html(source: dict[str, Any], session: requests.Session, fetch_content: bool,
                 since_days: int) -> tuple[list[Article], list[str]]:
    """Read bounded public news lists and verify dates on their article pages."""
    candidates: dict[str, str] = {}
    article_links = 0
    allowed = source.get("allowed_domains", [urlsplit(source["url"]).hostname])
    list_url = source['url']
    visited = set()
    for page in range(int(source.get('max_pages', 1))):
        if list_url in visited:break
        visited.add(list_url)
        response = session.get(list_url, timeout=25)
        response.raise_for_status()
        tree = lxml_html.fromstring(decode_page(response))
        metric(source,'list_pages')
        for link in tree.xpath(source.get("link_xpath", "//a[@href]")):
            url = normalize_url(urljoin(list_url, link.get("href", "")))
            host = (urlsplit(url).hostname or "").lower()
            if urlsplit(url).scheme not in ("https", "http") or not any(host == d or host.endswith("." + d) for d in allowed):continue
            if not re.search(source["article_url_pattern"], url):continue
            article_links += 1
            title_nodes = link.xpath(source.get("title_xpath", ".//strong | .//h5 | .//h4 | .//h2"))
            title = clean_text(link.get("title") or (title_nodes[0].text_content() if title_nodes else link.text_content()))
            metric(source,'raw_links')
            screening = title + (' ' + clean_text(link.getparent().text_content())[:1200] if source.get('screen_list_summary') else '')
            if len(title) < 6 or not required_match(source, screening):
                metric(source,'topic_rejected');continue
            candidates.setdefault(url, title)
        next_links = tree.xpath(source.get('next_page_xpath', "//a[contains(normalize-space(.),'下一页') or normalize-space(.)='下页' or normalize-space(.)='Next']/@href"))
        if not next_links:break
        next_url = urljoin(list_url,next_links[0])
        if urlsplit(next_url).hostname != urlsplit(source['url']).hostname:break
        list_url = next_url
        if page+1 < int(source.get('max_pages',1)):time.sleep(float(source.get('request_interval',1)))
    if not article_links:
        raise SourceError("页面没有预期的文章链接，可能发生改版或返回了验证页面")
    articles: list[Article] = []
    errors: list[str] = []
    for url, title in list(candidates.items())[:int(source.get("max_items", 12))]:
        time.sleep(float(source.get("request_interval", 1)))
        try:
            response = session.get(url, timeout=20)
            response.raise_for_status()
            metric(source,'detail_read')
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
                metric(source,'date_missing')
            if not is_recent(article, since_days):
                metric(source,'outside_window')
                continue
            metric(source,'in_window')
            if fetch_content:
                article.content = article_body(text)
            article.content = article.content or summary
            article.canonical_url = detail_metadata(text,url)['canonical']
            article.publisher_domain = ed.publisher_domain(article.canonical_url)
            if ed.body_level(title,summary,article.content)=='正文可用':metric(source,'body_available')
            articles.append(article)
        except (requests.RequestException, ValueError, LxmlError) as exc:
            errors.append(f"{url}: {exc}")
    metric(source,'detail_budget_skipped',max(0,len(candidates)-int(source.get('max_items',12))))
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
    try:
        response = session.get(url, timeout=20)
        response.raise_for_status()
        text = decode_page(response)
        metadata = detail_metadata(text,response.url or url)
        metadata.pop('tree',None)
        DETAIL_CACHE[url] = metadata
        return article_body(text)
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
    metric(source,'raw_links',len(parsed.entries))
    for entry in parsed.entries[:int(source.get('scan_items',200))]:
        title = clean_text(entry.get("title"))
        url = normalize_url(entry.get("link", ""))
        if not title or not url:
            continue
        summary = clean_text(entry.get("summary") or entry.get("description"))
        # Index all visible headlines before date filtering, for resolving aggregate links.
        PUBLISHER_INDEX.setdefault(ed.publisher_domain(url), []).append({'title':title,'url':url,'date':parse_date(entry)})
        if not required_match(source, title + " " + summary):
            metric(source,'topic_rejected')
            continue
        article = Article(source["id"], source.get("name", source["id"]), title, url,
                          parse_date(entry), summary, summary, list(source.get("tags", [])))
        # Do not download full archives simply to discard old articles later.
        if not normalized_date(article.published_at):metric(source,'date_missing')
        if not is_recent(article, since_days):
            metric(source,'outside_window')
            continue
        metric(source,'in_window')
        # Some official feeds include the complete article (content:encoded).
        # Prefer that text to an abbreviated webpage or an unnecessary request.
        raw_feed_content = " ".join(part.get("value", "") for part in entry.get("content", []))
        feed_content = clean_text(raw_feed_content)
        feed_resources = [m.group(0) for m in ed.RESOURCE.finditer(raw_feed_content)]
        if feed_resources:feed_content += ' 资源链接：'+' '.join(dict.fromkeys(feed_resources))
        if fetch_content:
            article.content = (fetch_article_content(url,session) or feed_content or summary) if not entry.get('published') else (feed_content or fetch_article_content(url, session) or summary)
        metadata = DETAIL_CACHE.get(url,{})
        article.canonical_url = metadata.get('canonical',url)
        article.publisher_domain = ed.publisher_domain(article.canonical_url)
        if metadata.get('date'):
            article.published_at = metadata['date']
            article.date_basis = '原站发布日期'
            if not is_recent(article,since_days):metric(source,'original_date_outside');continue
        if ed.body_level(title,summary,article.content)=='正文可用':metric(source,'body_available')
        articles.append(article)
        if len(articles)>=int(source.get('max_items',30)):break
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


def search_publisher(article, session):
    """Use only a public search form observed on the publisher's homepage."""
    global PUBLISHER_SEARCHES
    domain=article.publisher_domain
    if not domain or PUBLISHER_SEARCHES>=6:return '',None
    try:
        if domain not in PUBLISHER_FORMS:
            PUBLISHER_FORMS[domain]=None
            home=session.get('https://'+domain+'/',timeout=12);home.raise_for_status()
            tree=lxml_html.fromstring(decode_page(home))
            for form in tree.xpath('//form'):
                action=urljoin(home.url,form.get('action') or home.url)
                names=form.xpath('.//input/@name')
                key=next((n for n in names if n in ('q','query','s','keys','search')),None)
                if key and ed.publisher_domain(action)==domain and form.get('method','get').lower()=='get':
                    PUBLISHER_FORMS[domain]=(action,key);break
        form=PUBLISHER_FORMS.get(domain)
        if not form:return '',None
        PUBLISHER_SEARCHES+=1
        action,key=form
        result=session.get(action,params={key:article.title},timeout=15);result.raise_for_status()
        tree=lxml_html.fromstring(decode_page(result))
        for a in tree.xpath('//a[@href]'):
            target=urljoin(result.url,a.get('href'))
            text=clean_text(a.get('title') or a.text_content())
            if ed.publisher_domain(target)==domain and ratio(text.lower(),article.title.lower())>=85:
                return normalize_url(target),None
    except (requests.RequestException,ValueError,LxmlError):pass
    return '',None


def resolve_publisher_url(article, session):
    domain = article.publisher_domain
    for candidate in PUBLISHER_INDEX.get(domain,[]):
        if ratio(article.title.lower(),candidate['title'].lower()) >= 88:
            return candidate['url'], candidate.get('date')
    # Only ordinary redirects and public article links. Never use private RPCs or bypass a challenge.
    try:
        r = session.get(article.url,timeout=15)
        r.raise_for_status()
        if ed.publisher_domain(r.url)==domain:return normalize_url(r.url),None
        tree = lxml_html.fromstring(decode_page(r))
        for href in tree.xpath('//a[@href]/@href | //link[@rel="canonical"]/@href'):
            target=urljoin(r.url,href)
            if ed.publisher_domain(target)==domain and urlsplit(target).path not in ('','/'):
                return normalize_url(target),None
    except (requests.RequestException, ValueError, LxmlError):pass
    return search_publisher(article,session)


def collect_json_list(source, session, fetch_content, since_days):
    """Read a publisher's public JSON list linked from its own news page."""
    response=session.get(source['url'],timeout=25);response.raise_for_status()
    payload=response.json()
    if not isinstance(payload,list):raise SourceError('公开列表 JSON 结构已改变')
    metric(source,'raw_links',len(payload))
    items=[]
    for entry in payload[:int(source.get('scan_items',200))]:
        title=clean_text(entry.get('TITLE'))
        url=normalize_url(entry.get('URL',''))
        if not url or ed.publisher_domain(url) not in source.get('allowed_domains',[]):continue
        if not required_match(source,title):metric(source,'topic_rejected');continue
        a=Article(source['id'],source['name'],title,url,normalized_date(entry.get('DOCRELPUBTIME')),'','',source.get('tags',[]))
        if not is_recent(a,since_days):metric(source,'outside_window');continue
        metric(source,'in_window')
        a.content=fetch_article_content(url,session) if fetch_content else ''
        a.summary=DETAIL_CACHE.get(url,{}).get('summary','')
        a.canonical_url=DETAIL_CACHE.get(url,{}).get('canonical',url)
        a.publisher_domain=ed.publisher_domain(url)
        if ed.body_level(title,a.summary,a.content)=='正文可用':metric(source,'body_available')
        items.append(a)
        if len(items)>=int(source.get('max_items',8)):break
    return items


def collect_google_news(source: dict[str, Any], session: requests.Session, fetch_content: bool,
                        since_days: int = 8) -> list[Article]:
    """Search Google News RSS.

    Optional discovery channel. A title-only entry is never presented as a read article.
    """
    params = {"q": source["query"], "hl": source.get("hl", "en-US"),
              "gl": source.get("gl", "US"), "ceid": source.get("ceid", "US:en")}
    # Google News ranks by relevance and will happily return headlines from
    # years ago; when: restricts the window server-side. The window tracks
    # --since-days so the feed and the local freshness filter cannot disagree.
    if " when:" not in params["q"]:
        params["q"] = f"({params['q']}) when:{since_days}d"
    # Keep locale syntax readable. Independently verify dates; encoding alone is not freshness proof.
    url = f"{GOOGLE_NEWS_ENDPOINT}?{urlencode(params, safe=':')}"
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except requests.RequestException as exc:
        raise SourceError(str(exc)) from exc
    if not parsed.entries:
        raise SourceError("Google 新闻搜索没有返回条目，查询可能过长或已被限流")
    blocked = [str(domain).lower() for domain in source.get("blocked_domains", [])]
    articles: list[Article] = []
    metric(source,'raw_links',len(parsed.entries))
    for entry in parsed.entries[: int(source.get("scan_items", 100))]:
        title = clean_text(entry.get("title"))
        if not title:
            continue
        publisher = clean_text((entry.get("source") or {}).get("title"))
        # Headlines arrive as "标题 - 媒体名"; the suffix would defeat the
        # title-similarity de-duplication against the publisher's own feed.
        for separator in (" - ", " – ", " — "):
            if publisher and title.endswith(separator + publisher):
                title = title[: -len(separator + publisher)].strip()
                break
        url = normalize_url(entry.get("link", ""))
        if not title or not url:
            continue
        domain = urlsplit((entry.get("source") or {}).get("href") or "").netloc.lower()
        if blocked and any(domain.endswith(entry_domain) for entry_domain in blocked):
            continue
        summary = clean_text(entry.get("summary"))
        if not required_match(source, f"{title} {summary}"):
            metric(source,'topic_rejected')
            continue
        if source.get("language") == "Chinese" and not has_chinese(title):
            continue
        name = f"{source.get('name', source['id'])} · {publisher}" if publisher else source.get("name", source["id"])
        article = Article(source["id"], name, title, url, parse_date(entry), summary, summary,
                          list(source.get("tags", [])))
        if not is_recent(article, since_days):
            metric(source,'outside_window')
            continue
        metric(source,'in_window')
        article.publisher_domain = ed.publisher_domain((entry.get('source') or {}).get('href') or '')
        article.date_basis = '聚合标注日期，原站待核实'
        if fetch_content:
            canonical, original_date = resolve_publisher_url(article,session)
            if canonical:
                article.canonical_url = canonical
                article.content = fetch_article_content(canonical,session) or summary
                metadata = DETAIL_CACHE.get(canonical,{})
                original_date = metadata.get('date') or original_date
                if original_date:
                    article.published_at = normalized_date(original_date)
                    article.date_basis = '原站发布日期'
                    if not is_recent(article,since_days):metric(source,'original_date_outside');continue
                if ed.body_level(title,summary,article.content)=='正文可用':metric(source,'body_available')
            else:metric(source,'original_unresolved')
        articles.append(article)
        if len(articles)>=int(source.get('max_items',15)):break
    return articles


PROPER_NAME = re.compile(r"[《“\"]([^》”\"]{2,20})[》”\"]")


def proper_names(title: str) -> set[str]:
    """Quoted titles and product names, the strongest hint of a shared event."""
    return {name.strip() for name in PROPER_NAME.findall(title) if len(name.strip()) >= 2}


def dedupe_aggregated(items: list[Article], existing: list[Article]) -> list[Article]:
    """Drop aggregated items that restate something already collected.

    A publisher's own headline and a rewritten aggregator headline for the same
    event score as low as 0.20 on string similarity, so the usual 88% threshold
    misses them entirely. A shared quoted name (e.g. 《海山图志》) is reliable,
    and 60% similarity catches near-verbatim rewrites.
    """
    kept: list[Article] = []
    for article in items:
        duplicate = any(ed.same_event(vars(article),vars(other)) for other in existing+kept)
        if not duplicate:
            kept.append(article)
    return kept


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


def classify_content(article: Article) -> str:
    """Conservative discovery labels, never a verification of the underlying claim."""
    lead = f"{article.title} {article.summary}"
    label = ed.genre(article.title, lead + ' ' + article.content[:1000])
    if label in ed.LOW_PRIORITY or label in ('科研成果线索','数据与代码线索'):
        return label
    if keyword_hits(article.title, ("market worth", "market size", "market forecast", "marketsandmarkets",
                                    "市场规模", "市场预测", "复合年增长率", "市场报告")):
        return "商业市场资讯"
    if keyword_hits(lead, ("发表论文", "发表了论文", "论文", "研究成果", "研究进展", "研究方面取得",
                          "published in", "research paper", "peer-reviewed", "journal")):
        return "科研成果线索"
    if keyword_hits(lead, ("数据集", "开源", "数据发布", "dataset", "open-source", "data release")):
        return "数据与代码线索"
    document = keyword_hits(article.title, ("规划", "纲要", "行动计划", "实施方案", "战略", "标准",
                                            "strategic plan", "strategy", "roadmap", "standard", "s-100", "s-102"))
    issued = keyword_hits(article.title, ("发布", "印发", "出台", "批准", "实施", "生效", "颁布",
                                        "launch", "launched", "adopted", "approved", "released", "enters into force"))
    host = (urlsplit(article.url).hostname or "").lower()
    official = any(host == d or host.endswith("." + d) for d in
                   ("gov.cn", "noaa.gov", "iho.int", "unesco.org", "europa.eu", "nmdis.org.cn"))
    if document and issued:
        return "官方政策与标准发布线索" if official else "政策与标准报道"
    if keyword_hits(lead, ("survey", "mapping", "测量", "测绘", "测深", "水深", "海图")):
        return "技术与工程进展"
    return "行业资讯"


def policy_focus(article: Article) -> list[str]:
    if article.content_type not in ("官方政策与标准发布线索", "政策与标准报道"):
        return []
    text = f"{article.title} {article.summary}"
    labels = []
    ocean = keyword_hits(text, POLICY_GROUPS["中国十五五与海洋强国"] + KEYWORD_GROUPS["海洋测绘"])
    if ocean and keyword_hits(text, ("十五五", "第十五个五年")):
        labels.append("中国十五五（原文提及）")
    elif ocean and has_chinese(article.title):
        labels.append("国内海洋政策与标准")
    if keyword_hits(text, POLICY_GROUPS["国际海洋规划与标准"]):
        labels.append("国际海洋规划与标准")
    return labels


def score_article(article: Article, now: datetime | None = None) -> Article:
    now = now or (datetime.fromisoformat(ACTIVE_PERIOD['end']) if ACTIVE_PERIOD else datetime.now(timezone.utc))
    body = f"{article.title} {article.summary} {article.content}"
    # Long article bodies often contain related-news lists and footer text.
    # Policy classification therefore uses the title and publisher summary,
    # while technical/research classification can use the full body.
    policy_body = f"{article.title} {article.summary}"
    total_hits = sum(keyword_hits(body, words) for words in KEYWORD_GROUPS.values())
    marine_hits = keyword_hits(body, KEYWORD_GROUPS["海洋测绘"])
    article.content_type = classify_content(article)
    policy_labels = policy_focus(article)
    policy_hits = len(policy_labels)
    # A broad phrase such as “海洋经济合作” is useful context but is not by
    # itself evidence of a national or international plan. Require a planning,
    # strategy, standards, or policy signal before putting an item in that
    # report section.
    if policy_hits and not keyword_hits(policy_body, POLICY_CONTEXT_TERMS):
        policy_hits, policy_labels = 0, []
    future_hits, future_labels = matched_groups(body, FUTURE_GROUPS)
    research_hits, research_labels = matched_groups(body, RESEARCH_GROUPS)
    if article.content_type == "商业市场资讯":
        research_hits, research_labels = 0, []
    article.relevance = min(1.0, total_hits / 8)
    # Source tags describe the feed, but must not make every unrelated article
    # look marine-relevant. The score is based on article text only.
    article.marine_relevance = min(1.0, max(1,marine_hits) / 4) if ed.marine_context(article.title,article.summary+' '+article.content) else 0
    article.policy_relevance = min(1.0, policy_hits / 2)
    article.future_relevance = min(1.0, future_hits / 3)
    article.research_relevance = min(1.0, research_hits / 2)
    # Research requires a method/data/academic lead, not merely the word "algorithm" in a financing story.
    if article.content_type not in ('科研成果线索','数据与代码线索'):
        article.research_relevance = 0
    if article.content_type in ed.LOW_PRIORITY:
        article.research_relevance = article.future_relevance = article.marine_relevance = 0
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
    score = (
        0.10 * article.relevance
        + 0.25 * article.marine_relevance
        + 0.20 * article.policy_relevance
        + 0.25 * article.future_relevance
        + 0.10 * article.research_relevance
        + 0.05 * article.source_score
        + 0.05 * article.freshness
    )
    return score * (0.05 if article.content_type in ed.LOW_PRIORITY else 1.0)


def deduplicate(articles: list[Article]) -> list[Article]:
    kept: list[Article] = []
    for article in sorted(articles, key=total_score, reverse=True):
        if any(ed.same_event(vars(article),vars(old)) for old in kept):
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
        # A later retry can fill a previously missing body without losing translations.
        conn.execute("UPDATE articles SET content = ? WHERE fingerprint = ? AND length(COALESCE(content, '')) < ?",
                     (article.content, article.fingerprint, len(article.content)))
        conn.execute("UPDATE articles SET canonical_url=?, publisher_domain=?, date_basis=?, body_status=?, rule_version=? WHERE fingerprint=?",
                     (article.canonical_url or article.url, article.publisher_domain or ed.publisher_domain(article.url),
                      article.date_basis, ed.body_level(article.title,article.summary,article.content),ed.VERSION,article.fingerprint))
    conn.commit()
    return count


def collect(config: Path, db: Path, since_days: int, fetch_content: bool) -> int:
    sources = load_sources(config)
    session = collection_session()
    all_articles: list[Article] = []
    statuses: list[dict[str, Any]] = []
    for source in sources:
        source['_metrics'] = {}
        logging.info("采集 %s", source.get("name", source["id"]))
        status = {"id": source["id"], "name": source.get("name", source["id"]), "status": "正常",
                  "candidates": 0, "recent": 0, "detail": "",
                  "optional": bool(source.get("optional", False))}
        try:
            kind = source.get("type", "rss")
            if kind == "gdelt":
                items = collect_gdelt(source, session, fetch_content, since_days)
            elif kind == "google_news":
                items = collect_google_news(source, session, fetch_content, since_days)
            elif kind == "html":
                items, errors = collect_html(source, session, fetch_content, since_days)
                if errors:
                    status.update(status="部分失败", detail="; ".join(errors[:2])[:250])
                    logging.warning("来源部分失败 %s: %s", source["id"], status["detail"])
            elif kind == "rss":
                items = collect_feed(source, session, fetch_content, since_days)
            elif kind == 'json_list':
                items = collect_json_list(source,session,fetch_content,since_days)
            else:
                raise SourceError(f"不支持的新闻源类型：{kind}")
            status["candidates"] = len(items)
            status["recent"] = sum(is_recent(a, since_days) for a in items)
            all_articles.extend(items)
        except (SourceError, requests.RequestException, ValueError, LxmlError) as exc:
            status.update(status="失败", detail=clean_text(str(exc))[:250])
            logging.warning("新闻源失败 %s: %s", source["id"], exc)
        status['metrics'] = source.get('_metrics',{})
        if status['metrics'].get('original_unresolved'):
            status['detail'] += f"；{status['metrics']['original_unresolved']} 条聚合原文未解析，仅作待核实线索"
        statuses.append(status)
        logging.info("来源结果 %s: %s，近期 %d 条", source["id"], status["status"], status["recent"])
    recent: list[Article] = []
    for article in all_articles:
        score_article(article)
        if is_recent(article, since_days):
            article.published_at = normalized_date(article.published_at)
            recent.append(article)
    conn = init_db(db)
    # Keep raw candidates: selection rules can be upgraded without deleting historical evidence.
    inserted = save_articles(conn, recent)
    # Correct previously stored aggregator/update dates when the publisher exposes its original date.
    for observed_url,metadata in DETAIL_CACHE.items():
        if metadata.get('date'):
            conn.execute("UPDATE articles SET published_at=?, date_basis='原站发布日期' WHERE url=? OR canonical_url=?",
                         (metadata['date'],observed_url,metadata.get('canonical',observed_url)))
    conn.execute("INSERT INTO collection_runs (checked_at, sources_json, config_hash) VALUES (?, ?, ?)",
                 (datetime.now(timezone.utc).isoformat(), json.dumps(statuses, ensure_ascii=False),
                  hashlib.sha256(config.read_bytes()).hexdigest()))
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
        if translated and translated != text and has_chinese(translated):
            cache[text] = translated
            return translated
    except (requests.RequestException, ValueError, IndexError, TypeError) as exc:
        errors.append(f"Google: {exc}")
    try:
        # MyMemory's free endpoint limits input bytes, not Python characters.
        short = text.encode("utf-8")[:480].decode("utf-8", errors="ignore")
        response = session.get(MYMEMORY_ENDPOINT, params={"q": short, "langpair": "en|zh-CN"}, timeout=15)
        response.raise_for_status()
        data = response.json()
        translated = clean_text((data.get("responseData") or {}).get("translatedText"))
        if str(data.get("responseStatus")) == "200" and translated and has_chinese(translated) and translated.lower() != text.lower():
            if short != text:
                translated += "（备用翻译仅含原文前段）"
            cache[text] = translated
            return translated
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        errors.append(f"MyMemory: {exc}")
    logging.warning("翻译服务不可用，保留原文：%s", "; ".join(errors))
    cache[text] = text
    return text


def rows_for_report(conn: sqlite3.Connection, since_days: int, limit: int) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    upper = datetime.now(timezone.utc).isoformat()
    if ACTIVE_PERIOD:
        cutoff = datetime.fromisoformat(ACTIVE_PERIOD['start']).astimezone(timezone.utc).isoformat()
        upper = datetime.fromisoformat(ACTIVE_PERIOD['end']).astimezone(timezone.utc).isoformat()
    return list(conn.execute(
        """SELECT *, (
             0.10 * relevance + 0.25 * marine_relevance + 0.20 * policy_relevance
             + 0.25 * future_relevance + 0.10 * research_relevance
             + 0.05 * source_score + 0.05 * freshness
           ) * CASE WHEN content_type = '商业市场资讯' THEN 0.15 ELSE 1 END AS score
           FROM articles
           WHERE published_at >= ? AND published_at < ? AND COALESCE(eligibility_reason,'') = '' AND (
             relevance >= 0.15 OR marine_relevance >= 0.15 OR policy_relevance >= 0.15
             OR future_relevance >= 0.15 OR research_relevance >= 0.15
           )
           ORDER BY score DESC, published_at DESC LIMIT ?""",
        (cutoff, upper, limit),
    ))


def recalculate_scores(conn: sqlite3.Connection, sources=None) -> None:
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
        reason = ''
        if article.content_type in ed.LOW_PRIORITY:reason = article.content_type
        if sources is not None:
            source = sources.get(row['source_id'])
            if source is None:reason = '当前来源已停用'
            elif not required_match(source,article.title+' '+article.summary+(' '+article.content[:1200] if source.get('screen_list_summary') else '')):reason = '不满足当前来源主题规则'
        body_status = ed.body_level(article.title,article.summary,article.content)
        if body_status == '仅有标题，待核实':reason = '仅有标题，未取得足够正文或摘要'
        conn.execute(
            """UPDATE articles SET relevance = ?, marine_relevance = ?, policy_relevance = ?,
               future_relevance = ?, research_relevance = ?, focus_tags = ?,
               source_score = ?, freshness = ?, content_type = ? WHERE id = ?""",
            (article.relevance, article.marine_relevance, article.policy_relevance,
             article.future_relevance, article.research_relevance,
             yaml.safe_dump(article.focus_tags or [], allow_unicode=True),
             article.source_score, article.freshness, article.content_type, row["id"]),
        )
        conn.execute('UPDATE articles SET eligibility_reason=?,body_status=?,rule_version=? WHERE id=?',
                     (reason,body_status,ed.VERSION,row['id']))
    conn.commit()


def report_candidates(conn: sqlite3.Connection, report_dir: Path, since_days: int, limit: int,
                      date_text: str) -> list[sqlite3.Row]:
    # Import existing Markdown reports so the first upgrade also avoids repeats.
    for path in report_dir.glob("weekly-????-??-??.md"):
        report_date = path.stem.removeprefix("weekly-")
        if report_date >= date_text:
            continue
        text = path.read_text(encoding="utf-8")
        for block in text.split("### ")[1:]:
            url = re.search(r"^- 原文：\[([^\]]+)\]", block, re.M)
            title = re.search(r"^- 原标题：(.+)$", block, re.M)
            if url and title:
                conn.execute("INSERT OR IGNORE INTO report_items VALUES (?, ?, ?)",
                             (report_date, normalize_url(url[1]), title[1].strip()))
    day = datetime.fromisoformat(date_text)
    week_start = (day - timedelta(days=day.weekday())).date().isoformat()
    previous = list(conn.execute("SELECT url, title FROM report_items WHERE report_date < ?", (week_start,)))
    # Filter before limiting; otherwise old headlines can occupy all available slots.
    rows = rows_for_report(conn, since_days, -1)
    kept: list[sqlite3.Row] = []
    per_source: dict[str, int] = {}
    general_count = 0
    general_language = set()
    # Marine items first; a small transferable-method section can supplement them.
    rows.sort(key=lambda r:(r['marine_relevance']>0 or r['policy_relevance']>0, r['score']),reverse=True)
    for row in rows:
        if any(normalize_url(row['canonical_url'] or row['url']) == old['url'] or normalize_url(row['url']) == old['url'] for old in previous):
            continue
        if any(ed.same_event(row,old) for old in kept):continue
        # Two prolific trade feeds otherwise fill the whole week: English titles
        # match more keyword groups than short Chinese ones, so without a cap
        # domestic coverage silently disappears from the report.
        publisher = row['publisher_domain'] or ed.publisher_domain(row['canonical_url'] or row['url'])
        if per_source.get(publisher, 0) >= MAX_ITEMS_PER_SOURCE:
            continue
        general = row['marine_relevance']==0 and row['policy_relevance']==0
        if general and general_count>=2:continue
        if general:
            language='zh' if has_chinese(row['title']) else 'other'
            if language in general_language:continue
            general_language.add(language)
        general_count += int(general)
        per_source[publisher] = per_source.get(publisher, 0) + 1
        kept.append(row)
        if len(kept) >= limit:
            return kept
    return kept


def extract_research_evidence(text: str) -> dict[str, list[str]]:
    """Extract only literal leads. Links and metrics are not independently verified."""
    dois = re.findall(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", text)
    urls = re.findall(r"https?://[^\s<>\"']+", text)
    resources = [u.rstrip(".,;，。；)") for u in urls if 'glossary' not in u.lower() and any(k in u.lower() for k in
                 ("github.com/", "zenodo.org/", "figshare.com/", "pangaea.de/", "data.marine.copernicus.eu/", "emodnet.ec.europa.eu/geoviewer", "emodnet.ec.europa.eu/en/seabed-habitats", "gebco.net/data"))]
    metrics = re.findall(r"[^。.!?\n]{0,65}(?:RMSE|MAE|R²|均方根误差|平均绝对误差)[^。!?\n]{0,65}", text, re.I)
    return {"DOI": list(dict.fromkeys(d.rstrip(".,;，。；)") for d in dois))[:3],
            "数据或代码链接": list(dict.fromkeys(resources))[:3], "精度指标原句": metrics[:2]}


def make_report(db: Path, report_dir: Path, since_days: int, limit: int, translate: bool, config=None) -> Path:
    try:
        from .student_report import render
    except ImportError:
        from student_report import render
    return render(sys.modules[__name__], db, report_dir, since_days, limit, translate, config)


def main() -> int:
    global ACTIVE_PERIOD
    args = parse_args()
    ACTIVE_PERIOD = None if args.rolling else report_period(week_of=args.week_of)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command in ("collect", "run"):
        collect(args.config, args.db, args.since_days, not args.no_content)
    if args.command in ("report", "run"):
        path = make_report(args.db, args.report_dir, args.since_days, args.limit, not args.no_translate, args.config)
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
