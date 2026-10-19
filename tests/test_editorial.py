"""Editorial regressions for observed errors, report history and backup runs."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from src import geomatics_news as g
from scripts.report_due import needs_collection


class EditorialTests(unittest.TestCase):
    def article(self, title, summary="", url="https://www.noaa.gov/news/1"):
        return g.score_article(g.Article("test", "test", title, url,
                                        datetime.now(timezone.utc).isoformat(), summary, "", []))

    def test_wetland_paper_is_not_a_five_year_plan(self):
        a = self.article("国家海洋信息中心在滨海湿地空间规划与治理研究方面取得新进展",
                         "在Marine Policy发表论文，研究海洋空间规划框架。")
        self.assertEqual(a.content_type, "科研成果线索")
        self.assertEqual(a.policy_relevance, 0)
        self.assertFalse(any("十五五" in tag for tag in a.focus_tags))

    def test_market_forecast_cannot_outrank_real_mapping_project(self):
        market = self.article("AUV Market worth $10.94 billion by 2031 - MarketsandMarkets",
                              "hydrographic bathymetry multibeam sonar algorithm validation dataset")
        mapping = self.article("Mapping Long Island Sound", "hydrographic multibeam bathymetry")
        self.assertEqual(market.content_type, "商业市场资讯")
        self.assertEqual(market.research_relevance, 0)
        self.assertGreater(g.total_score(mapping), g.total_score(market))

    def test_domestic_ocean_plan_does_not_imply_fifteenth_plan(self):
        a = self.article("海洋观测行动计划正式发布", url="https://www.mnr.gov.cn/news/1")
        self.assertGreater(a.policy_relevance, 0)
        self.assertIn("国内海洋政策与标准", a.focus_tags)
        self.assertFalse(any("十五五" in tag for tag in a.focus_tags))

    def test_same_week_rerun_stable_and_previous_week_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            conn = g.init_db(root / "news.db")
            g.save_articles(conn, [self.article("新多波束水深测量工程")])
            g.recalculate_scores(conn)
            today = datetime.now(g.REPORT_TIMEZONE).date()
            date = today.isoformat()
            rows = g.report_candidates(conn, root, 8, 30, date)
            self.assertEqual(len(rows), 1)
            conn.execute("INSERT INTO report_items VALUES (?, ?, ?)", (date, rows[0]["url"], rows[0]["title"]))
            self.assertEqual(len(g.report_candidates(conn, root, 8, 30, date)), 1)
            old = (today - timedelta(days=today.weekday() + 1)).isoformat()
            conn.execute("UPDATE report_items SET report_date = ?", (old,))
            self.assertEqual(g.report_candidates(conn, root, 8, 30, date), [])
            conn.close()

    def test_existing_markdown_history_is_imported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            conn = g.init_db(root / "news.db")
            article = self.article("公开海底水深数据集发布")
            g.save_articles(conn, [article])
            g.recalculate_scores(conn)
            (root / "weekly-2020-01-01.md").write_text(
                f"### 1. 新闻\n- 原标题：{article.title}\n- 原文：[{article.url}]({article.url})\n", encoding="utf-8")
            self.assertEqual(g.report_candidates(conn, root, 8, 30, datetime.now(g.REPORT_TIMEZONE).date().isoformat()), [])
            conn.close()

    def test_backup_retries_partial_stale_or_changed_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            config = root / "config" / "sources.yaml"
            config.write_text("sources: []", encoding="utf-8")
            (root / "reports").mkdir()
            now = datetime.now(g.REPORT_TIMEZONE)
            date = now.date().isoformat()
            (root / "reports" / f"weekly-{date}.md").write_text("report")
            path = root / "reports" / "health.json"
            health = dict(report_date=date, checked_at=now.isoformat(), complete=False,
                          sources=[{"status": "失败"}], config_hash=hashlib.sha256(config.read_bytes()).hexdigest())
            path.write_text(json.dumps(health))
            self.assertTrue(needs_collection(root, now))
            health.update(complete=True, sources=[{"status": "正常"}])
            path.write_text(json.dumps(health))
            self.assertFalse(needs_collection(root, now))
            config.write_text("sources: [new]")
            self.assertTrue(needs_collection(root, now))
            path.write_text("bad json")
            self.assertTrue(needs_collection(root, now))

    def test_evidence_does_not_invent_links_and_preserves_metrics(self):
        empty = g.extract_research_evidence("论文在Marine Policy发表。")
        self.assertTrue(all(not values for values in empty.values()))
        found = g.extract_research_evidence("DOI: 10.1234/example. https://github.com/lab/depth RMSE = 0.8 m。")
        self.assertEqual(found["DOI"], ["10.1234/example"])
        self.assertEqual(found["数据或代码链接"], ["https://github.com/lab/depth"])
        self.assertIn("0.8 m", found["精度指标原句"][0])

    def test_canonical_links_keep_meaningful_query(self):
        self.assertEqual(g.normalize_url("https://example.com/news?id=2&utm_source=x#top"),
                         "https://example.com/news?id=2")

    def test_doi_is_not_mistaken_for_a_data_download(self):
        found = g.extract_research_evidence("https://doi.org/10.1234/example")
        self.assertEqual(found["DOI"], ["10.1234/example"])
        self.assertEqual(found["数据或代码链接"], [])

    def test_retry_can_fill_missing_body_in_existing_record(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = g.init_db(Path(folder) / "news.db")
            article = self.article("多波束水深测量新成果")
            g.save_articles(conn, [article])
            article.content = "补采得到的论文正文和精度指标"
            self.assertEqual(g.save_articles(conn, [article]), 0)
            self.assertEqual(conn.execute("SELECT content FROM articles").fetchone()[0], article.content)
            conn.close()

    def test_translation_service_error_is_not_saved_as_translation(self):
        session = Mock()
        google, memory = Mock(), Mock()
        google.json.return_value = []
        memory.json.return_value = {"responseStatus": 403, "responseData": {"translatedText": "配额已用完"}}
        session.get.side_effect = [google, memory]
        self.assertEqual(g.translate_text("Bathymetric lidar news", session, {}), "Bathymetric lidar news")

    def test_nonmarine_tech_stays_out_of_marine_future_section(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            conn = g.init_db(root / "news.db")
            g.save_articles(conn, [self.article("地理信息产业数字孪生与时空智能", "遥感人工智能")])
            conn.close()
            text = g.make_report(root / "news.db", root / "reports", 8, 30, False).read_text(encoding="utf-8")
            self.assertIn("## 测绘与前沿技术", text)
            self.assertNotIn("## 未来海洋测绘技术", text)


if __name__ == "__main__":
    unittest.main()
