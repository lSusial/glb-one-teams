"""본문 보강: 짧은 처리창 재시도·노출 단계 회수·독립 출처 검증."""
import sqlite3
import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import fulltext
import llm_ranker
import main
import quality_report


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((Path(__file__).parents[1] / 'schema.sql').read_text())
        self.conn.executemany(
            'INSERT INTO media_sources(source_id,media_name,primary_country_code,language,tier) '
            'VALUES(?,?,?,?,?)', [(1, 'Publisher A', 'US', 'en', 1),
                                 (2, 'Publisher B', 'US', 'en', 2),
                                 (3, 'Publisher C', 'US', 'en', 1)])
        self.conn.executemany(
            'INSERT INTO media_source_feeds(feed_id,source_id,feed_url,feed_section,last_status) '
            'VALUES(?,?,?,\'top\',200)', [(i, i, f'https://feed/{i}') for i in (1, 2, 3)])
        self.addCleanup(self.conn.close)

    def add(self, aid, source=1, parent=None, body=None, title=None):
        title = title or 'Federal Reserve holds interest rates unchanged after policy meeting'
        self.conn.execute(
            '''INSERT INTO articles_raw(article_id,source_id,feed_id,title,link,summary,
                   published_at,content_hash,filter_decision,llm_prefilter,filter_score,
                   ai_score,duplicate_of,full_text,title_en,summary_en,primary_country)
               VALUES(?,?,?,?,?,?,?,?,'passed','keep',8,70,?,?,?,?,'US')''',
            (aid, source, source, title, f'https://publisher{source}.test/{aid}', title,
             date.today().isoformat(), f'h{aid}', parent, body, title, title))
        self.conn.commit()

    def set_failure(self, aid, reason, hours):
        fulltext.ensure_columns(self.conn)
        self.conn.execute(
            "UPDATE articles_raw SET fulltext_status='extract_failed', "
            "fulltext_failure_reason=?, fulltext_attempted_at=datetime('now',?) WHERE article_id=?",
            (reason, f'-{hours} hours', aid))
        self.conn.commit()

    def test_transient_failure_retries_before_short_window_expires(self):
        self.add(1)
        self.set_failure(1, 'http_503', 7)
        with patch.object(fulltext, '_extract', return_value=('Recovered body', None)):
            result = fulltext.run_fulltext(self.conn, days=2)
        self.assertEqual(result['extracted_ids'], [1])

    def test_access_denied_keeps_long_cooldown(self):
        self.add(1)
        self.set_failure(1, 'http_403', 25)
        with patch.object(fulltext, '_extract') as extract:
            result = fulltext.run_fulltext(self.conn, days=2)
        self.assertEqual(result['total'], 0)
        extract.assert_not_called()

    def test_legacy_empty_failure_retries_next_day(self):
        self.add(1)
        self.set_failure(1, 'empty_or_blocked', 25)
        with patch.object(fulltext, '_extract', return_value=('Recovered body', None)):
            result = fulltext.run_fulltext(self.conn, days=2)
        self.assertEqual(result['extracted_ids'], [1])

    def test_http_failures_and_timeout_are_distinguished(self):
        import requests
        for status in (403, 429, 503):
            response = Mock(status_code=status)
            with self.subTest(status=status), patch('requests.get', return_value=response):
                self.assertEqual(fulltext._extract('https://publisher.test/article'),
                                 (None, f'http_{status}'))
        with patch('requests.get', side_effect=requests.Timeout):
            self.assertEqual(fulltext._extract('https://publisher.test/article'),
                             (None, 'timeout'))

    def test_http_success_extracts_html_and_records_empty_page(self):
        response = Mock(status_code=200, content=b'<html><article>source</article></html>')
        with patch('requests.get', return_value=response), \
                patch('trafilatura.extract', return_value='Extracted news body') as extract:
            self.assertEqual(fulltext._extract('https://publisher.test/article'),
                             ('Extracted news body', None))
        self.assertEqual(extract.call_args.args[0], response.content)
        with patch('requests.get', return_value=response), patch('trafilatura.extract', return_value=None):
            self.assertEqual(fulltext._extract('https://publisher.test/article'), (None, 'empty_body'))

    def test_rate_limit_stops_queued_requests_to_same_host(self):
        for aid in (1, 2, 3):
            self.add(aid)
        self.add(4, source=2)
        def extract(url):
            return (None, 'http_429') if 'publisher1' in url else ('Body', None)
        with patch.object(fulltext, '_extract', side_effect=extract) as call:
            result = fulltext.run_fulltext(self.conn, workers=1)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result['extracted_ids'], [4])
        self.assertEqual(result['deferred'], 2)

    def test_rate_limited_host_is_not_retried_in_later_pipeline_pass(self):
        self.add(1)
        self.add(2)
        self.set_failure(1, 'http_429', 1)
        with patch.object(fulltext, '_extract') as extract:
            result = fulltext.run_fulltext(self.conn, article_ids=[2], direct_only=True)
        extract.assert_not_called()
        self.assertEqual(result['deferred'], 1)
        self.assertIsNone(self.conn.execute(
            'SELECT fulltext_attempted_at FROM articles_raw WHERE article_id=2').fetchone()[0])

    def test_same_host_concurrency_is_bounded(self):
        import threading
        import time
        for aid in range(1, 9):
            self.add(aid)
        lock = threading.Lock()
        active = peak = 0
        def extract(url):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return 'Body', None
        with patch.object(fulltext, '_extract', side_effect=extract), \
                patch.object(fulltext.config, 'FULLTEXT_DOMAIN_WORKERS', 2):
            result = fulltext.run_fulltext(self.conn, workers=8)
        self.assertEqual(result['extracted'], 8)
        self.assertEqual(peak, 2)

    def test_display_recovery_uses_resolved_url_and_reranks_only_success(self):
        self.add(1)
        self.add(2, source=2, title='US Treasury bond yields rise on inflation data')
        self.conn.execute("UPDATE articles_raw SET link='https://news.google.com/rss/articles/1' WHERE article_id=1")
        with patch.object(fulltext, 'decode_serial', return_value={1: 'https://publisher1.test/1'}):
            fulltext.resolve_display_links(self.conn)
        with patch('export_json.visible_article_ids', return_value={1, 2}), \
                patch.object(fulltext, '_extract', side_effect=lambda url:
                             ('Body', None) if url.endswith('/1') else (None, 'http_403')), \
                patch.object(llm_ranker, 'run_rank', return_value={'ranked': 1, 'failed': 0}) as rank:
            main._recover_display_fulltext(self.conn, use_batch=False)
        self.assertEqual(rank.call_args.kwargs['article_ids'], [1])

    def test_display_recovery_respects_budget_and_does_not_decode_again(self):
        self.add(1)
        self.add(2, source=2, title='US Treasury bond yields rise on inflation data')
        with patch('export_json.visible_article_ids', return_value={1, 2}), \
                patch.object(fulltext.config, 'DISPLAY_FULLTEXT_LIMIT', 1), \
                patch.object(fulltext, 'decode_serial') as decode, \
                patch.object(fulltext, '_extract', return_value=('Body', None)) as extract, \
                patch.object(llm_ranker, 'run_rank', return_value={'ranked': 1}):
            main._recover_display_fulltext(self.conn, use_batch=False)
        self.assertEqual(extract.call_count, 1)
        decode.assert_not_called()

    def test_sibling_body_is_used_without_copying_into_representative(self):
        self.add(1)
        self.add(2, source=2, parent=1)
        self.conn.execute("UPDATE articles_raw SET llm_prefilter=NULL, ai_score=NULL WHERE article_id=2")
        self.set_failure(1, 'http_403', 1)
        with patch('export_json.visible_article_ids', return_value={1}), \
                patch.object(fulltext, '_extract', return_value=('Independent source body', None)), \
                patch.object(llm_ranker, 'run_rank', return_value={'ranked': 1, 'failed': 0}) as rank:
            main._recover_display_fulltext(self.conn, use_batch=False)
        self.assertEqual(rank.call_args.kwargs['article_ids'], [1])
        self.assertIsNone(self.conn.execute('SELECT full_text FROM articles_raw WHERE article_id=1').fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT full_text FROM articles_raw WHERE article_id=2').fetchone()[0],
                         'Independent source body')

    def test_two_source_body_fallback_records_real_provenance(self):
        self.add(1)
        self.add(2, source=2, parent=1, body='INDEPENDENT_BODY Federal Reserve kept US rates unchanged.')
        provider = Mock(model_id='test:local')
        provider.complete_json_batch.return_value = {'0': {
            'ai_score': 70, 'title_en': 'Federal Reserve holds US interest rates',
            'summary_en': 'The Federal Reserve kept US interest rates unchanged.',
            'topics': ['ECONOMY'], 'primary_country': 'US'}}
        result = llm_ranker.run_rank(self.conn, provider=provider, article_ids=[1])
        self.assertEqual(result['synthesized'], 1)
        self.assertIn('INDEPENDENT_BODY', provider.complete_json_batch.call_args.args[0][0][2])
        row = self.conn.execute('SELECT full_text,source_links FROM articles_raw WHERE article_id=1').fetchone()
        self.assertIsNone(row['full_text'])
        self.assertEqual({s['src'] for s in json.loads(row['source_links'])}, {'Publisher A', 'Publisher B'})

    def test_invalid_rerank_preserves_existing_summaries(self):
        self.add(1)
        self.conn.execute("UPDATE articles_raw SET summary_ko='기존 번역', expanded_summary='기존 긴 요약' WHERE article_id=1")
        provider = Mock(model_id='test:local')
        provider.complete_json_batch.return_value = {}
        with patch('export_json.visible_article_ids', return_value={1}), \
                patch.object(fulltext, '_extract', return_value=('New body', None)), \
                patch.object(llm_ranker, 'get_provider', return_value=provider):
            main._recover_display_fulltext(self.conn, use_batch=False)
        row = self.conn.execute('SELECT ai_score,summary_ko,expanded_summary,full_text FROM articles_raw').fetchone()
        self.assertEqual(tuple(row), (70, '기존 번역', '기존 긴 요약', 'New body'))

    def test_sources_prefer_body_and_reject_wrong_event(self):
        self.add(1)
        self.add(2, source=2, parent=1)
        self.add(3, source=2, parent=1, body='Verified body')
        self.add(4, source=3, parent=1, body='Wrong event',
                 title='Fairfax exports Canadian fertilizer to India')
        sources = llm_ranker._cluster_sources(self.conn, 1, 'Publisher A')
        self.assertEqual([s['article_id'] for s in sources], [3])

    def test_report_separates_own_body_from_verified_sibling_body(self):
        self.add(1)
        self.add(2, source=2, parent=1, body='Verified body')
        self.set_failure(1, 'http_403', 1)
        report = quality_report.build_report(self.conn)
        self.assertEqual(report['summary']['active_fulltext'], 0)
        self.assertEqual(report['summary']['active_evidence_fulltext'], 1)
        self.assertEqual(report['summary']['fulltext_missing_reasons'], {'http_403': 1})

    def test_report_does_not_count_same_publisher_or_unrelated_body_as_backup(self):
        self.add(1)
        self.add(2, source=1, parent=1, body='Same publisher body')
        self.add(3, source=2, parent=1, body='Unrelated body',
                 title='Fairfax exports Canadian fertilizer to India')
        self.conn.execute("UPDATE articles_raw SET publisher_name='© Photo Agency, AFP' WHERE article_id=1")
        report = quality_report.build_report(self.conn)
        self.assertEqual(report['summary']['active_evidence_fulltext'], 0)


if __name__ == '__main__':
    unittest.main()
