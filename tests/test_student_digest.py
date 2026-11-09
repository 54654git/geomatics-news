"""Regressions from the 2026-11-09 audit and fixed-period delivery."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, Mock
from src import geomatics_news as g, editorial as ed
from src.periods import report_period, content_version, contains
from scripts.report_due import needs_collection
from scripts import resolve_mnr as dns


class StudentDigestTests(unittest.TestCase):
    def test_observed_false_positives_are_not_technical_research(self):
        cases=[
          ('Surveying profession backs Monopoly edition to tackle talent visibility gap',
           'The Hydrographic Society supports a board game about underwater survey, hydrography and sensors.'),
          ('Coast Survey Spotlight: Meet Colleen Roche, Northeast Navigation Manager',
           'Meet our staff. Our employees use bathymetric data and S-100 in their careers.'),
          ('Investors back Mach9 with a significant Series A to bring AI to civil engineering',
           'The company has raised money for surveying AI algorithms and validation.')]
        for title,body in cases:
            a=g.score_article(g.Article('test','test',title,'https://example.org/news',datetime.now(timezone.utc).isoformat(),body,body*3,[]))
            self.assertIn(a.content_type,ed.LOW_PRIORITY)
            self.assertEqual((a.research_relevance,a.marine_relevance,a.future_relevance),(0,0,0))

    def test_same_release_different_headlines_is_one_event(self):
        a=dict(title='国家海洋信息中心牵头研发的“海山图志”成果正式发布',url='https://nmdis.org.cn/1',published_at='2026-11-06T00:00:00+00:00',content='海山图志首次发布')
        b=dict(title='我国首个海山数字化空间地理信息成果发布',url='https://sohu.com/2',published_at='2026-11-06T12:00:00+00:00',content='')
        self.assertTrue(ed.same_event(a,b))

    def test_industry_questionnaire_and_proprietary_datasets_are_not_research_releases(self):
        self.assertIn(ed.genre('How is hydrography changing? Take part in the 2027 industry survey','data researchers'),ed.LOW_PRIORITY)
        self.assertNotEqual(ed.genre('Leica targets surveying with Captivate','The controller handles large survey datasets.'),'数据与代码线索')

    def test_long_aggregate_title_is_not_a_real_abstract(self):
        title='NOAA: Nearly all of the ocean floor near the Marianas have been mapped out, valuable and timely for lease sales'
        self.assertEqual(ed.body_level(title,title+' Publisher',title+' Publisher'),'仅有标题，待核实')

    def test_same_product_later_progress_survives(self):
        a=dict(title='“RAVEN”无人潜器正式发布',url='https://lab.org/1',published_at='2026-11-02T00:00:00+00:00',content='')
        b=dict(title='“RAVEN”无人潜器完成海试',url='https://lab.org/2',published_at='2026-11-10T00:00:00+00:00',content='')
        self.assertFalse(ed.same_event(a,b))

    def test_monday_tuesday_and_boundaries(self):
        p=report_period(datetime.fromisoformat('2026-11-09T08:00:00+08:00'))
        self.assertEqual(p,report_period(datetime.fromisoformat('2026-11-10T08:00:00+08:00')))
        self.assertTrue(contains('2026-11-02T00:00:00+08:00',p))
        self.assertFalse(contains('2026-11-09T00:00:00+08:00',p))
        self.assertFalse(contains('2026-11-01T23:59:59+08:00',p))

    def test_due_catches_code_updates_and_bounds_core_and_optional_retries(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'reports').mkdir();(root/'src').mkdir()
            (root/'reports/weekly-2026-11-09.md').write_text('report')
            code=root/'src/parser.py';code.write_text('v1')
            path=root/'reports/health.json'
            health=dict(period_id='2026-11-09',complete=True,content_version=content_version(root),attempts=1)
            def due():
                path.write_text(json.dumps(health));return needs_collection(root,week_of='2026-11-09')
            self.assertFalse(due())
            health.update(supplement_degraded=True);self.assertTrue(due())
            health.update(attempts=2);self.assertFalse(due())
            health.update(complete=False);self.assertTrue(due())
            health.update(attempts=3);self.assertFalse(due())
            self.assertTrue(needs_collection(root,week_of='2026-11-09',force=True))
            code.write_text('v2');self.assertTrue(due())

    def test_content_version_is_portable_across_line_endings(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'src').mkdir();p=root/'src/parser.py'
            p.write_bytes(b'a\nb\n');a=content_version(root)
            p.write_bytes(b'a\r\nb\r\n');self.assertEqual(a,content_version(root))

    def test_publisher_original_date_overrides_refreshed_feed_date(self):
        import requests
        session=Mock();r=requests.Response();r.status_code=200
        recent=datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
        r._content=f'<rss version="2.0"><channel><title>x</title><item><title>Bathymetry study</title><link>https://lab.org/old</link><pubDate>{recent}</pubDate></item></channel></rss>'.encode()
        session.get.return_value=r
        def fetch(url,session):
            g.DETAIL_CACHE[url]={'date':'2019-01-01T00:00:00+00:00'}
            return 'A real but old article.'*30
        with patch.dict(g.DETAIL_CACHE,clear=True),patch.object(g,'fetch_article_content',side_effect=fetch):
            self.assertEqual(g.collect_feed({'id':'lab','url':'https://lab.org/feed'},session,True,8),[])

    def test_public_publisher_search_uses_observed_form_and_same_domain(self):
        import requests
        def response(url,body):
            r=requests.Response();r.status_code=200;r.url=url;r._content=body.encode();r.encoding='utf-8';return r
        session=Mock();session.get.side_effect=[
            response('https://lab.org/','<form action="/search" method="get"><input name="q"></form>'),
            response('https://lab.org/search','<a href="https://unrelated.org/story">Water depth from UAV photos</a><a href="/study">Water depth from UAV photos</a>')]
        article=g.Article('a','a','Water depth from UAV photos','https://news.google.com/a',None,'','',[])
        article.publisher_domain='lab.org'
        with patch.dict(g.PUBLISHER_FORMS,clear=True),patch.object(g,'PUBLISHER_SEARCHES',0):
            self.assertEqual(g.search_publisher(article,session),('https://lab.org/study',None))
        self.assertEqual(session.get.call_args.kwargs['params'],{'q':article.title})

    def test_old_database_rechecked_without_deletion(self):
        with tempfile.TemporaryDirectory() as d:
            conn=g.init_db(Path(d)/'news.db')
            a=g.Article('legacy','source','海洋测绘装备发布','https://lab.org/1',datetime.now(timezone.utc).isoformat(),'测深系统','海洋测绘多波束测深试验。'*30,[])
            g.save_articles(conn,[g.score_article(a)])
            g.recalculate_scores(conn,{'legacy':{'required_terms':['不应命中的词']}})
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0],1)
            self.assertTrue(conn.execute('SELECT eligibility_reason FROM articles').fetchone()[0])
            self.assertEqual(g.rows_for_report(conn,8,30),[])
            conn.close()

    def test_title_only_is_retained_but_not_published(self):
        with tempfile.TemporaryDirectory() as d:
            conn=g.init_db(Path(d)/'news.db')
            a=g.score_article(g.Article('google','aggregate','Water depth from UAV photos','https://news.google.com/a',datetime.now(timezone.utc).isoformat(),'Water depth from UAV photos Hydro International','',[]))
            g.save_articles(conn,[a]);g.recalculate_scores(conn)
            self.assertEqual(g.rows_for_report(conn,8,30),[])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0],1)
            conn.close()

    def test_quiet_week_does_not_relax_publisher_cap(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);conn=g.init_db(root/'news.db')
            titles=['多波束测深系统完成验收','海道测量船正式交付','海图服务数据更新发布','水下测绘机器人完成试验']
            for i,title in enumerate(titles):
                a=g.Article('google' if i==3 else 'direct','pub',title,f'https://hydro.example/{i}',datetime.now(timezone.utc).isoformat(),title,(title+'。')*30,[])
                a.publisher_domain='hydro.example';g.save_articles(conn,[g.score_article(a)])
            g.recalculate_scores(conn)
            selected=g.report_candidates(conn,root,8,15,datetime.now(g.REPORT_TIMEZONE).date().isoformat())
            self.assertEqual(len(selected),3)
            conn.close()

    def test_evidence_preserved_when_no_known_container(self):
        text='<html><body><article><h1>Bathymetry study</h1>'+('<p>We used multibeam bathymetry to map the seafloor and tested independent validation data.</p>'*12)+'<a href="https://doi.org/10.1234/test">paper</a><a href="https://github.com/lab/depth">code</a></article></body></html>'
        body=g.article_body(text)
        self.assertIn('https://doi.org/10.1234/test',body)
        self.assertIn('https://github.com/lab/depth',body)

    @patch.object(dns,'reachable',side_effect=[False,True])
    def test_dns_unreachable_address_falls_back_to_next_resolver(self,reachable):
        session=Mock();a=Mock();b=Mock()
        a.json.return_value={'Status':0,'Answer':[{'name':dns.HOST,'type':1,'data':'8.8.8.8'}]}
        b.json.return_value={'Status':0,'Answer':[{'name':dns.HOST,'type':1,'data':'1.1.1.1'}]}
        session.get.side_effect=[a,b]
        self.assertEqual(dns.resolve_entries(session),[f'1.1.1.1 {dns.HOST}'])


if __name__=='__main__':unittest.main()
