"""주간 점검의 브리핑 갱신·근거 부족·금액/기간 오탐 회귀 사례."""
import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import briefing
import export_json as E
import numeric_guard as N
import text_quality as T

ROOT = Path(__file__).resolve().parents[1]


class BriefingFreshnessTests(unittest.TestCase):
    def conn(self):
        c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
        c.executescript((ROOT / 'schema.sql').read_text()); self.addCleanup(c.close)
        return c

    def brief(self, c, date, summary='Verified facts.', cc='US', model='test:model'):
        c.execute("INSERT INTO country_briefings(cc,briefing_date,briefing_type,summary,summary_en,model) VALUES(?,?,'daily',?,?,?)",
                  (cc, date, '검증된 사실입니다.', summary, model))

    def test_previous_day_brief_is_explicitly_delayed(self):
        c = self.conn(); self.brief(c, '2026-11-07')
        b = E._daily_briefs(c, '2026-11-08')['US']
        self.assertEqual(b['date'], '2026-11-07')
        self.assertEqual(b['status'], 'delayed')

    def test_historical_export_does_not_use_future_brief(self):
        c = self.conn(); self.brief(c, '2026-11-07'); self.brief(c, '2026-11-09')
        self.assertEqual(E._daily_briefs(c, '2026-11-08')['US']['date'], '2026-11-07')

    def test_current_and_insufficient_evidence_are_distinct(self):
        c = self.conn(); self.brief(c, '2026-11-08')
        self.brief(c, '2026-11-08', 'No articles met the briefing criteria for this period.', cc='LA', model='rules:insufficient-evidence')
        b = E._daily_briefs(c, '2026-11-08')
        self.assertEqual(b['US']['status'], 'current')
        self.assertEqual(b['LA']['status'], 'insufficient_evidence')

    def test_weak_saved_brief_is_not_presented_as_analysis(self):
        c = self.conn(); self.brief(c, '2026-11-08', 'The available source text does not provide specific figures or policy decisions.')
        b = E._daily_briefs(c, '2026-11-08')['US']
        self.assertEqual(b['status'], 'insufficient_evidence')
        self.assertIn('근거', b['ko'])

    def test_empty_saved_output_is_unavailable_not_a_completed_brief(self):
        c = self.conn(); self.brief(c, '2026-11-08')
        c.execute("UPDATE country_briefings SET summary='',summary_en=''")
        self.assertNotIn('US', E._daily_briefs(c, '2026-11-08'))

    def test_audit_warns_for_stale_daily_even_if_other_checks_pass(self):
        spec = importlib.util.spec_from_file_location('freshness_audit', ROOT / 'eval/display_audit.py')
        audit = importlib.util.module_from_spec(spec); spec.loader.exec_module(audit)
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder); c = sqlite3.connect(p / 'news.db')
            c.executescript((ROOT / 'schema.sql').read_text()); c.close()
            (p / 'countries.json').write_text(json.dumps({'snapshot_date':'2026-11-08','countries':[
                {'cc':'US','articles':[], 'brief':{'ko':'검증된 사실입니다.','en':'Verified facts.','date':'2026-11-07'}},
                {'cc':'CN','articles':[], 'brief':{'status':'unavailable'}},
                {'cc':'LA','articles':[], 'brief':{'date':'2026-11-08','ko':'근거 부족','status':'insufficient_evidence'}}]}))
            output = io.StringIO()
            with patch.object(sys, 'argv', ['audit','--db',str(p/'news.db'),'--export-dir',str(p),'--json']), contextlib.redirect_stdout(output):
                audit.main()
            counts = json.loads(next(s for s in output.getvalue().splitlines() if s.startswith('{')))
            self.assertEqual(counts['STALE_DAILY_BRIEFING'], 1)
            self.assertEqual(counts['DAILY_BRIEFING_MISSING'], 1)

    def test_real_fact_survives_filter_and_uses_one_source_prompt(self):
        c = self.conn()
        c.execute("INSERT INTO media_sources(source_id,media_name,primary_country_code,language,tier) VALUES(1,'News','GB','en',1)")
        c.execute("INSERT INTO media_source_feeds(feed_id,source_id,feed_url,feed_section) VALUES(1,1,'https://x/rss','main')")
        for aid, title, summary in [
            (1, 'Bank of England opening remarks', 'The available source text does not provide specific figures or policy decisions.'),
            (2, 'HSBC raises UK mortgage rate to 4%', 'HSBC raised its UK mortgage rate to 4%.')]:
            c.execute("""INSERT INTO articles_raw(article_id,source_id,feed_id,primary_country,title,title_en,summary_en,
                link,content_hash,published_at,ai_score) VALUES(?,1,1,'GB',?,?,?, ?,?,'2026-11-07',68)""",
                (aid,title,title,summary,f'https://x/{aid}',str(aid)))
        class Provider:
            model_id = 'test:model'
            def complete_json_batch(self, requests):
                self.requests = requests
                return {r[0]:{'summary_en':'HSBC raised its UK mortgage rate to 4%.',
                             'summary_ko':'HSBC가 영국 주택담보대출 금리를 4%로 올렸습니다.'} for r in requests}
        p = Provider()
        briefing.run_briefing(c, p, briefing_type='daily', countries=['GB'], briefing_date='2026-11-08')
        row = c.execute('SELECT article_count,source_articles FROM country_briefings').fetchone()
        self.assertEqual(row['article_count'], 1)
        self.assertEqual(json.loads(row['source_articles']), ['https://x/2'])
        self.assertIn('at most two factual sentences', p.requests[0][1])
        self.assertNotIn('opening remarks', p.requests[0][2])

    def test_title_repetition_and_explicit_no_information_are_not_evidence(self):
        self.assertFalse(T.has_briefing_evidence('Governor opening remarks', 'Governor opening remarks. Governor opening remarks.'))
        self.assertFalse(T.has_briefing_evidence('Governor opening remarks', 'The source text provided is limited to the title and a repeated summary line, so specific figures and policy decisions cannot be confirmed.'))
        self.assertTrue(T.has_briefing_evidence('Bank cuts rate', 'The bank cut its policy rate to 4%. Full details were not provided.'))
        fact = 'Federal Reserve raises interest rates by a quarter point as inflation stays elevated'
        self.assertTrue(T.has_briefing_evidence(fact, fact))
        self.assertFalse(T.has_briefing_evidence('Opening remarks 2026', 'The available source text is limited to the title of the 2026 opening remarks.'))
        self.assertFalse(T.has_briefing_evidence('Policy meeting schedule', 'The source provides only the headline and a brief summary, so the new schedule cannot be confirmed.'))
        self.assertFalse(T.has_briefing_evidence('Market preview', 'The source text supplied is a headline and brief dek only, so specific figures are not reported here.'))

    def test_weak_sources_do_not_trigger_a_model_request(self):
        c = self.conn()
        c.execute("INSERT INTO media_sources(source_id,media_name,primary_country_code,language,tier) VALUES(1,'News','GB','en',1)")
        c.execute("INSERT INTO media_source_feeds(feed_id,source_id,feed_url,feed_section) VALUES(1,1,'https://x/rss','main')")
        c.execute("""INSERT INTO articles_raw(article_id,source_id,feed_id,primary_country,title,title_en,summary_en,summary_ko,link,content_hash,published_at,ai_score)
            VALUES(1,1,1,'GB','Bank of England opening remarks','Bank of England opening remarks',
            'The source text provided is limited to the title and a repeated summary line, so specific figures and policy decisions cannot be confirmed.',
            '제공된 원문으로는 구체적 정책 결정을 확인할 수 없다.','https://x/a','h','2026-11-07',68)""")
        class Provider:
            model_id = 'test:model'
            def complete_json_batch(self, requests):
                raise AssertionError('No model request should be made for informationless sources')
        briefing.run_briefing(c, Provider(), briefing_type='daily', countries=['GB'], briefing_date='2026-11-08')
        row = c.execute('SELECT article_count,model FROM country_briefings').fetchone()
        self.assertEqual(tuple(row), (0, 'rules:insufficient-evidence'))

    def test_dong_copula_is_not_a_missing_amount_or_a_tenfold_exemption(self):
        en = {'summary':'Banks earned VND 311 trillion; the leader earned VND 40.44 trillion.'}
        self.assertEqual(N.bilingual_amount_issues(en, {'summary':'은행 이익은 311조 동이며, 선두 은행은 40조 4,400억 동이다.'}), [])
        self.assertTrue(N.bilingual_amount_issues(en, {'summary':'은행 이익은 31.1조 동이며, 선두 은행은 40조 4,400억 동이다.'}))
        self.assertEqual(N.fx_values('정부가 동결 방침을 밝혔다'), {})

    def test_month_range_requires_both_endpoints(self):
        source = 'Bangladesh exports rise 6% in July-October.'
        self.assertEqual(T.period_issues(source, '7~10월 수출 6% 증가'), [])
        self.assertTrue(T.period_issues(source, '7월 수출 6% 증가'))
        self.assertTrue(T.period_issues(source, '수출 6% 증가'))

    def test_percentage_basis_belongs_to_its_own_number(self):
        source = 'CPI rose 0.2% month-on-month in October and 5.09% year-on-year.'
        self.assertEqual(T.period_issues(source, '10월 CPI 전년 대비 5.09% 상승'), [])
        self.assertTrue(T.period_issues(source, '10월 CPI 5.09% 상승'))
        self.assertTrue(T.period_issues(source, 'CPI rose 0.2% year-on-year and 5.09% month-on-month in October.'))
        self.assertEqual(T.period_issues('The bank held its policy rate at 4% in November while growth rose 3% year-on-year.',
                                        '은행이 기준금리를 4%로 동결했다.'), [])
        self.assertEqual(T.period_issues('Japan September cash earnings rise 4.6% YoY, beating 3.8% forecast',
            'Japan Labor Cash Earnings (YoY) above expectations (3.8%) in September: Actual (4.6%)'), [])


if __name__ == '__main__':
    unittest.main()
