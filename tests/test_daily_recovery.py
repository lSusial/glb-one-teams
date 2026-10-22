import sqlite3
import unittest
from unittest.mock import patch
from contextlib import ExitStack, redirect_stdout
from collections import defaultdict
from types import SimpleNamespace
import io

import db
import llm_prefilter
import main


class DailyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE media_sources(
                source_id INTEGER PRIMARY KEY, media_name TEXT,
                primary_country_code TEXT, language TEXT, tier INTEGER);
            INSERT INTO media_sources VALUES
                (1,'Laos News','LA','en',1),(2,'US News','US','en',1);
            CREATE TABLE articles_raw(
                article_id INTEGER PRIMARY KEY, source_id INTEGER, title TEXT, summary TEXT,
                link TEXT, content_hash TEXT, published_at TEXT, filter_decision TEXT,
                filter_score INTEGER, duplicate_of INTEGER, llm_prefilter TEXT,
                llm_reject_reason TEXT, ai_score INTEGER, primary_country TEXT);
        ''')

    def add(self, aid, source, title, day, prefilter=None, score=None, filter_score=7):
        self.db.execute(
            '''INSERT INTO articles_raw(
                   article_id,source_id,title,summary,link,content_hash,published_at,
                   filter_decision,filter_score,duplicate_of,llm_prefilter,llm_reject_reason,
                   ai_score,primary_country)
               VALUES(?,?,?,?,?,?,?,'passed',?,NULL,?,?,?,NULL)''',
            (aid, source, title, title, f'https://example.com/{aid}', f'h{aid}', day,
             filter_score, prefilter, 'initial drop' if prefilter == 'drop' else None, score))
        self.db.commit()

    def test_backlog_window_does_not_expand_daily_briefing(self):
        targets = [
            'llm_prefilter.run_prefilter', 'llm_prefilter.rescue_sparse_countries',
            'fulltext.run_fulltext', 'llm_ranker.run_rank', 'llm_dedup.run_dedup',
            'fulltext.resolve_display_links', 'llm_expand.run_expand',
            'category_guard.fix_ungrounded_geo', 'llm_translate.run_translate',
            'briefing.run_briefing', 'briefing.generate_daily_highlights',
            'briefing.generate_daily_quiz',
            'llm_dedup.refresh_representatives', 'export_json.visible_article_ids',
        ]
        with ExitStack() as stack:
            stack.enter_context(patch.object(main.db, 'open_conn', return_value=self.db))
            stack.enter_context(patch.object(main, '_expanded_ai_days', return_value=7))
            calls = {t: stack.enter_context(patch(t, return_value=defaultdict(int))) for t in targets}
            with redirect_stdout(io.StringIO()):
                main.cmd_ai(SimpleNamespace(days=2, sync=True))
            self.assertEqual(calls['llm_ranker.run_rank'].call_args.kwargs['days'], 7)
            self.assertEqual(calls['briefing.run_briefing'].call_args.kwargs['days'], 1)

    def test_ai_window_expands_to_recent_unprocessed_backlog(self):
        self.add(1, 2, 'Fed policy update in the US', '2026-10-19', 'keep', 60)
        self.add(2, 1, 'Laos investment agreement', '2026-10-16', None, None)
        self.assertEqual(main._expanded_ai_days(self.db, 2), 3)

    def test_ai_window_expansion_is_capped(self):
        self.add(1, 2, 'Fed policy update in the US', '2026-10-19', 'keep', 60)
        self.add(2, 1, 'Laos investment agreement', '2026-09-01', None, None)
        self.assertEqual(main._expanded_ai_days(self.db, 2), 7)

    def test_sparse_country_drop_is_rescued_for_ranking(self):
        self.add(1, 1, 'Laos and Azerbaijan sign an investment agreement', '2026-10-19', 'drop', None, 9)
        self.add(2, 1, 'Laos business forum opens in Vientiane', '2026-10-19', 'drop', None, 7)
        self.add(3, 1, 'Unrelated Singapore flight story', '2026-10-19', 'drop', None, 10)
        self.add(4, 2, 'Federal Reserve decision in the US', '2026-10-19', 'keep', None, 8)
        result = llm_prefilter.rescue_sparse_countries(self.db, days=None)
        self.assertEqual(result['rescued'], 2)
        rescued = {r['article_id'] for r in self.db.execute(
            "SELECT article_id FROM articles_raw WHERE llm_reject_reason='sparse-country rescue'")}
        self.assertEqual(rescued, {1, 2})

    def test_sparse_country_does_not_rescue_promotional_noise(self):
        self.add(1, 1, 'Laos regional trade fair opens', '2026-10-19', 'drop', None, 9)
        self.db.execute(
            "UPDATE articles_raw SET llm_reject_reason='지역 무역박람회 홍보성 기사' WHERE article_id=1")
        self.db.commit()
        result = llm_prefilter.rescue_sparse_countries(self.db, days=None)
        self.assertEqual(result['rescued'], 0)
        row = self.db.execute(
            "SELECT llm_prefilter FROM articles_raw WHERE article_id=1").fetchone()
        self.assertEqual(row['llm_prefilter'], 'drop')

    def test_bad_publisher_credit_falls_back_to_media_name(self):
        self.db.execute("ALTER TABLE articles_raw ADD COLUMN publisher_name TEXT")
        self.add(1, 2, 'US policy', '2026-10-19', 'keep', 60)
        self.db.execute("UPDATE articles_raw SET publisher_name='© Photo Agency, AFP' WHERE article_id=1")
        row = self.db.execute(
            f'''SELECT {db.publisher_expr()} src FROM articles_raw a
                JOIN media_sources m ON m.source_id=a.source_id WHERE article_id=1''').fetchone()
        self.assertEqual(row['src'], 'US News')


if __name__ == '__main__':
    unittest.main()
