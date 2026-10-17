"""Regression tests for direct sources, date checks, and degraded coverage."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import yaml

from src import geomatics_news as g


def response(text, encoding="utf-8", status=200):
    r = requests.Response()
    r.status_code = status
    r._content = text.encode(encoding)
    r.encoding = "ISO-8859-1"  # Simulate a site without an HTTP charset.
    return r


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.today = datetime.now(timezone.utc).date().isoformat()
        self.source = dict(id="test", name="测试来源", type="html", url="https://school.edu.cn/news/",
                           article_url_pattern=r"/info/\d+\.htm", required_terms=["水深", "测绘"],
                           max_items=5, request_interval=0, tags=["海洋测绘"])

    def article(self, date):
        return g.Article("test", "测试来源", "水深测量与海洋测绘", "https://school.edu.cn/info/1.htm",
                         date, "多波束测深", "", [])

    def test_direct_html_extracts_dates_and_ignores_other_domains(self):
        listing = '<meta charset="utf-8"><a href="/info/1.htm">水深测量装备</a><a href="https://other.cn/info/2.htm">测绘新闻</a>'
        detail = f'<meta charset="utf-8"><meta name="PubDate" content="{self.today}"><meta name="description" content="多波束测深新装备">'
        session = Mock()
        session.get.side_effect = [response(listing), response(detail)]
        articles, errors = g.collect_html(self.source, session, False, 8)
        self.assertEqual(len(articles), 1)
        self.assertEqual(errors, [])
        self.assertEqual(articles[0].summary, "多波束测深新装备")
        self.assertEqual(session.get.call_count, 2)

    def test_old_article_cannot_use_recent_footer_or_url(self):
        self.source["date_xpath"] = "//span[contains(text(),'发布时间')]"
        session = Mock()
        session.get.side_effect = [response('<a href="/info/1.htm">水深测量激光雷达</a>'),
            response(f'<meta charset="utf-8"><span>发布时间：2025-10-25</span><footer>{self.today}</footer>')]
        articles, errors = g.collect_html(self.source, session, False, 8)
        self.assertEqual(articles, [])
        self.assertEqual(errors, [])

    def test_unknown_date_is_reported_and_skipped(self):
        session = Mock()
        session.get.side_effect = [response('<a href="/info/1.htm">水深测量装备</a>'), response('<footer>Copyright 2026</footer>')]
        articles, errors = g.collect_html(self.source, session, False, 8)
        self.assertEqual(articles, [])
        self.assertIn("发布日期", errors[0])

    def test_one_failed_article_does_not_discard_other_articles(self):
        session = Mock()
        session.get.side_effect = [response('<a href="/info/1.htm">水深测量装备</a><a href="/info/2.htm">海洋测绘新技术</a>'),
            requests.Timeout("timeout"), response(f'<meta name="PubDate" content="{self.today}">')]
        articles, errors = g.collect_html(self.source, session, False, 8)
        self.assertEqual(len(articles), 1)
        self.assertEqual(len(errors), 1)

    def test_verification_page_is_failure_not_empty_success(self):
        session = Mock()
        session.get.return_value = response("<html>Verification required</html>")
        with self.assertRaises(g.SourceError):
            g.collect_html(self.source, session, False, 8)

    def test_chinese_legacy_encoding(self):
        r = response('<meta charset="gb2312"><p>水深测量</p>', "gb18030")
        self.assertIn("水深测量", g.decode_page(r))

    def test_future_and_missing_dates_are_excluded(self):
        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        for date in (None, "unknown", future, "2025-10-25"):
            self.assertFalse(g.is_recent(self.article(date), 8))

    def test_rate_limit_honors_long_retry_after_without_immediate_retry(self):
        session = Mock()
        r = response("Too many requests", status=429)
        r.headers["Retry-After"] = "120"
        session.get.return_value = r
        with patch.object(g, "GDELT_LAST_REQUEST", 0), patch.object(g.time, "sleep") as sleep:
            with self.assertRaisesRegex(g.SourceError, "429"):
                g.collect_gdelt(dict(id="gdelt", query="bathymetry"), session, False)
            sleep.assert_not_called()
        self.assertEqual(session.get.call_count, 1)

    def test_non_json_gdelt_error_is_actionable(self):
        session = Mock()
        session.get.return_value = response("Your query is too long")
        with patch.object(g, "GDELT_LAST_REQUEST", 0):
            with self.assertRaisesRegex(g.SourceError, "query is too long"):
                g.collect_gdelt(dict(id="gdelt", query="bathymetry"), session, False)

    def test_report_filters_by_publication_not_collection_date(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = g.init_db(Path(folder) / "news.db")
            g.save_articles(conn, [g.score_article(self.article("2025-10-25T00:00:00+00:00"))])
            self.assertEqual(g.rows_for_report(conn, 8, 30), [])
            conn.close()

    def test_source_health_is_persisted_even_when_other_source_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "sources.yaml"
            config.write_text(yaml.safe_dump({"sources": [self.source, dict(id="broken", type="rss", url="https://broken.invalid/")]}))
            with patch.object(g, "collect_html", return_value=([self.article(self.today)], [])), \
                    patch.object(g, "collect_feed", side_effect=g.SourceError("HTTP 503")):
                g.collect(config, root / "news.db", 8, False)
            conn = g.init_db(root / "news.db")
            statuses = json.loads(conn.execute("SELECT sources_json FROM collection_runs").fetchone()[0])
            self.assertEqual([s["status"] for s in statuses], ["正常", "失败"])
            self.assertEqual(statuses[0]["recent"], 1)
            conn.close()
            report = g.make_report(root / "news.db", root / "reports", 8, 30, False).read_text(encoding="utf-8")
            self.assertIn("新闻源采集状态", report)
            self.assertIn("HTTP 503", report)

    def test_platform_label_cannot_make_an_arbitrary_article_trusted(self):
        article = self.article(self.today)
        article.source_name = "NASA 央广网"
        article.url = "https://toutiao.com/article/123"
        self.assertEqual(g.score_article(article).source_score, .7)

    def test_navigation_does_not_leak_into_body_or_topic_score(self):
        body = g.article_body('<nav>水深测量 海图 多波束</nav><div id="content1"><p>北斗卫星定位测试</p></div>')
        self.assertEqual(body, "北斗卫星定位测试")
        article = self.article(self.today)
        article.title, article.summary, article.content = "通信验证", "", body
        self.assertEqual(g.score_article(article).marine_relevance, 0)

    def test_policy_phrase_and_english_substrings_are_not_technical_terms(self):
        self.assertEqual(g.keyword_hits("向海图强", ["海图"]), 0)
        self.assertEqual(g.keyword_hits("registration", ["gis"]), 0)
        self.assertEqual(g.keyword_hits("GIS and GNSS", ["gis", "gnss"]), 2)

    def test_policy_and_future_direction_scores_are_separate(self):
        article = self.article(self.today)
        article.title = "十五五海洋强国与S-100数字水深规划"
        article.summary = "无人船多波束测深和数字孪生海洋数据标准"
        g.score_article(article)
        self.assertGreaterEqual(article.policy_relevance, 0.5)
        self.assertGreaterEqual(article.future_relevance, 0.5)
        self.assertIn("中国十五五与海洋强国", article.focus_tags)
        self.assertIn("智能无人测量", article.focus_tags)

    def test_broad_ocean_economy_news_needs_policy_context(self):
        article = self.article(self.today)
        article.title = "滨海城市深化海洋经济合作"
        article.summary = "港口和临港产业交流取得新进展"
        g.score_article(article)
        self.assertEqual(article.policy_relevance, 0)
        self.assertNotIn("中国十五五与海洋强国", article.focus_tags)

    def test_report_contains_policy_and_future_sections(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            conn = g.init_db(root / "news.db")
            policy = self.article(self.today)
            policy.title = "十五五海洋强国规划发布"
            policy.summary = "海洋科技与海洋观测重点任务"
            future = self.article(self.today)
            future.title = "无人船多波束水深测量新技术"
            future.summary = "点云算法和精度验证"
            g.save_articles(conn, [g.score_article(policy), g.score_article(future)])
            conn.close()
            report = g.make_report(root / "news.db", root / "reports", 8, 30, False).read_text(encoding="utf-8")
            self.assertIn("十五五与国际海洋规划信号", report)
            self.assertIn("未来海洋测绘技术", report)
            self.assertIn("规划", report)
            self.assertIn("未来技术", report)


if __name__ == "__main__":
    unittest.main()
