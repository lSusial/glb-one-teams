"""11/4 주간 감사: 금액 약어·비교 기간·주간 번역 필드 누락 회귀 테스트."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import briefing
import numeric_guard as N

ROOT = Path(__file__).resolve().parents[1]


class Provider:
    model_id = 'test:model'

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete_json_batch(self, requests):
        self.requests.extend(requests)
        response = self.responses.pop(0)
        return {r[0]: copy.deepcopy(response) for r in requests}


def weekly_response():
    return {'summary_en': 'The budget raises £1.5 billion.', 'summary_ko': '예산으로 15억 파운드를 확보합니다.',
            'issues_en': ['Bank surcharge doubled.'], 'issues_ko': ['은행 부가세가 인상됐고 손실은 1.9억 파운드입니다.'],
            'outlook_en': 'Banks face uncertainty.', 'outlook_ko': '은행의 불확실성이 커집니다.',
            'keywords_en': ['Banks'], 'keywords_ko': ['은행'],
            'key_stat_en': 'Receipts of £1.5 billion', 'key_stat_ko': '세수 15억 파운드'}


class AmountParsingTests(unittest.TestCase):
    def test_million_abbreviations_work_in_all_currency_and_range_patterns(self):
        self.assertEqual(N.usd_values('USD 547 mln'), [547e6])
        self.assertEqual(N.usd_values('547 mln USD'), [547e6])
        self.assertEqual(N.usd_values('$10–15mln'), [10e6, 15e6])
        self.assertEqual(N.fx_values('£190mln')['GBP'], [190e6])
        self.assertEqual(N.fx_values('RM 3,000 mn')['MYR'], [3e9])
        self.assertEqual(N.inr_values('Rs 15 mln'), [15e6])
        self.assertFalse(N.amount_mismatch('USD 547 mln', '5.47억 달러'))
        self.assertTrue(N.amount_mismatch('USD 547 mln', '54.7억 달러'))
        self.assertEqual(N.usd_values('USD 5-year sukuk'), [])

    def test_weekly_fields_detect_missing_and_swapped_issue_amounts(self):
        en = {'summary': '£1.5 billion receipts', 'issues': ['£190 million loss', '£11 billion gap']}
        ko = {'summary': '15억 파운드 세수', 'issues': ['110억 파운드 손실', '1.9억 파운드 공백']}
        self.assertTrue(N.bilingual_amount_issues(en, ko))
        self.assertTrue(N.bilingual_amount_issues({'outlook': 'No figure.'}, {'outlook': '1.9억 파운드 손실'}))
        self.assertTrue(N.bilingual_amount_issues({'key_stat': '£190 million'}, {'key_stat': '손실 발생'}))
        self.assertTrue(N.bilingual_amount_issues({'summary': '£190 million'}, {'summary': '1.9억 달러'}))
        self.assertTrue(N.bilingual_amount_issues({'issues': ['A', 'B']}, {'issues': ['가']}))
        self.assertEqual(N.bilingual_amount_issues({'issues': ['£190 million loss']}, {'issues': ['1.9억 파운드 손실']}), [])

    def test_bidirectional_validation_handles_dong_particles_and_fx_quotes(self):
        self.assertEqual(N.bilingual_amount_issues(
            {'summary': 'Banks reported VND 311 trillion profit.'}, {'summary': '은행 이익은 311조 동으로 늘었다.'}), [])
        self.assertEqual(N.bilingual_amount_issues(
            {'summary': 'The reference rate is 25,630 VND/USD.'}, {'summary': '기준환율은 1달러당 2만 5,630동이다.'}), [])
        self.assertEqual(N.fx_values('Rp 18,051 per US dollar; fines of Rp2.1 trillion', include_quotes=False), {'IDR': [2.1e12]})
        self.assertEqual(N.fx_values('정부가 동결 방침을 밝혔다'), {})
        self.assertTrue(N.bilingual_amount_issues(
            {'summary': 'Banks reported VND 311 trillion profit.'}, {'summary': '은행 이익은 31.1조 동으로 늘었다.'}))


class WeeklyGenerationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript((ROOT / 'schema.sql').read_text())
        self.conn.execute("INSERT INTO media_sources(source_id,media_name,primary_country_code,language,tier) VALUES(1,'News','GB','en',1)")
        self.conn.execute("INSERT INTO media_source_feeds(feed_id,source_id,feed_url,feed_section) VALUES(1,1,'https://x/rss','main')")
        self.conn.execute("""INSERT INTO articles_raw(article_id,feed_id,source_id,primary_country,title,link,content_hash,
            published_at,ai_score,ai_model,summary_en,summary_ko)
            VALUES(1,1,1,'GB','UK budget','https://x/a','hash','2026-10-29',70,'test:model',
            'The UK budget raises £1.5 billion. Banks risk a £190 million loss and face an £11 billion fiscal gap.','예산 소식')""")
        self.conn.commit()

    def run_weekly(self, provider):
        with patch.object(briefing, '_last_completed_week', return_value=('2026-10-26', '2026-11-01')):
            return briefing.run_briefing(self.conn, provider, briefing_type='weekly', countries=['GB'], briefing_date='2026-11-02')

    def test_issue_mismatch_is_caught_before_save_and_retry_names_bad_field(self):
        bad = weekly_response()
        good = copy.deepcopy(bad)
        good['issues_en'] = ['Bank surcharge doubled, with a £190 million loss risk.']
        p = Provider([bad, good])
        self.assertEqual(self.run_weekly(p)['written'], 1)
        self.assertEqual(len(p.requests), 2)
        self.assertIn('issues[0]', p.requests[1][2])
        self.assertIn('GBP', p.requests[1][2])

    def test_final_retry_translates_only_verified_english_fields(self):
        bad = weekly_response()
        translated = {'summary_ko': '예산으로 15억 파운드를 확보합니다.', 'issues_ko': ['은행 부가세가 인상됐습니다.'],
                      'outlook_ko': '은행의 불확실성이 커집니다.', 'keywords_ko': ['은행'], 'key_stat_ko': '세수 15억 파운드',
                      'summary_en': 'INJECTED £50 billion'}
        p = Provider([bad, bad, translated])
        self.assertEqual(self.run_weekly(p)['written'], 1)
        self.assertEqual(len(p.requests), 3)
        self.assertIn('translation', p.requests[2][1])
        row = self.conn.execute('SELECT summary_en,issues_en,issues FROM country_briefings').fetchone()
        self.assertEqual(row['summary_en'], bad['summary_en'])
        self.assertEqual(json.loads(row['issues_en']), bad['issues_en'])
        self.assertEqual(json.loads(row['issues']), translated['issues_ko'])

    def test_unsupported_english_is_not_used_as_translation_fallback(self):
        bad = weekly_response()
        bad['summary_en'] = 'The budget raises £15 billion.'
        bad['summary_ko'] = '예산으로 150억 파운드를 확보합니다.'
        p = Provider([bad, bad])
        self.assertEqual(self.run_weekly(p)['written'], 0)
        self.assertEqual(len(p.requests), 2)
        self.assertIsNone(self.conn.execute('SELECT 1 FROM country_briefings').fetchone())

    def test_failed_translation_fallback_does_not_overwrite_saved_brief(self):
        bad = weekly_response()
        good = copy.deepcopy(bad)
        good['issues_en'] = ['Bank surcharge doubled, with a £190 million loss risk.']
        self.run_weekly(Provider([good]))
        original = tuple(self.conn.execute('SELECT summary,issues FROM country_briefings').fetchone())
        p = Provider([bad, bad, {'summary_ko': '예산 150억 파운드'}])
        self.assertEqual(self.run_weekly(p)['written'], 0)
        self.assertEqual(tuple(self.conn.execute('SELECT summary,issues FROM country_briefings').fetchone()), original)


class AuditTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('amount_consistency_audit', ROOT / 'eval/display_audit.py')
        self.audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.audit)

    def audit_card(self, title_ko):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            c = sqlite3.connect(folder / 'news.db')
            c.executescript((ROOT / 'schema.sql').read_text())
            c.execute("INSERT INTO media_sources(source_id,media_name,primary_country_code,language,tier) VALUES(1,'News','MY','en',1)")
            c.execute("INSERT INTO media_source_feeds(feed_id,source_id,feed_url,feed_section) VALUES(1,1,'https://x/rss','main')")
            source = 'Monthly subsidies eased to RM3 billion. The annual total is RM40 billion in 2026.'
            c.execute("""INSERT INTO articles_raw(article_id,feed_id,source_id,title,link,content_hash,published_at,summary,full_text)
                VALUES(1,1,1,'Fuel subsidies reach RM40 billion in 2026','https://x/a','hash','2026-10-30',?,?)""", (source, source))
            c.commit(); c.close()
            card = {'t': title_ko, 't_en': 'Fuel subsidies seen reaching RM40 billion in 2026', 'q_en': source,
                    'q': '월 30억 링깃, 연 400억 링깃', 'u': 'https://x/a', 'd': '2026-10-30', 'score': 65, 'rl': []}
            (folder / 'countries.json').write_text(json.dumps({'snapshot_date': '2026-10-31','countries': [],'non_presence': {'articles': [card]}}))
            with patch.object(sys, 'argv', ['audit', '--db', str(folder / 'news.db'), '--export-dir', str(folder), '--strict']), contextlib.redirect_stdout(io.StringIO()):
                return self.audit.main()

    def test_monthly_and_annual_titles_are_checked_against_original_source(self):
        self.assertEqual(self.audit_card('유류보조금 월 30억 링깃'), 0)
        self.assertEqual(self.audit_card('유류보조금 월 300억 링깃'), 1)
        self.assertEqual(self.audit_card('유류보조금 연 30억 링깃'), 1)
        source = 'Monthly fuel subsidies cost RM3 billion; the annual total is RM40 billion. A deal costs $3 billion.'
        self.assertFalse(N.different_period_titles_supported(
            source, 'Annual fuel subsidies reach RM40 billion', '유류보조금 월 30억 달러'))
        self.assertFalse(N.different_period_titles_supported(
            source, 'Fuel subsidies reach RM40 billion', '유류보조금 월 30억 링깃'))

    def test_weekly_audit_detects_amount_only_in_korean_issue(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            c = sqlite3.connect(folder / 'news.db'); c.executescript((ROOT / 'schema.sql').read_text()); c.close()
            (folder / 'countries.json').write_text(json.dumps({'snapshot_date':'2026-11-04','countries':[]}))
            (folder / 'weekly.json').write_text(json.dumps({'countries': [{'cc':'GB','summary':'요약','summary_en':'Summary',
                'issues':['1.9억 파운드 손실'], 'issues_en':['Risk of losses'], 'week_end':'2026-11-01'}]}))
            with patch.object(sys, 'argv', ['audit','--db',str(folder / 'news.db'),'--export-dir',str(folder),'--strict']), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.audit.main(), 1)


if __name__ == '__main__':
    unittest.main()
