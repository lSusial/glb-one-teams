import json
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

import briefing
import llm_dedup


class Provider:
    model_id = 'test:model'

    def __init__(self, response=None, fail=False):
        self.response = response
        self.fail = fail
        self.requests = []

    def complete_json_batch(self, requests):
        self.requests.extend(requests)
        if self.fail:
            raise RuntimeError('API unavailable')
        return {r[0]: self.response for r in requests}


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE media_sources(source_id INTEGER PRIMARY KEY,
                primary_country_code TEXT, tier INTEGER);
            INSERT INTO media_sources VALUES(1,'US',1),(2,'JP',1),(3,'LA',1);
            CREATE TABLE articles_raw(article_id INTEGER PRIMARY KEY, source_id INTEGER,
                primary_country TEXT, title_ko TEXT, title TEXT, ai_score INTEGER,
                ai_model TEXT, published_at TEXT, duplicate_of INTEGER, dup_by_ai INTEGER,
                summary_ko TEXT, summary_en TEXT, link TEXT, event_type TEXT,
                korean_fi TEXT, personnel_move INTEGER);
        ''')
        self.addCleanup(self.db.close)

    def add(self, aid, source=1, cc='US', score=60, day=None, duplicate=None, ai=0):
        self.db.execute('INSERT INTO articles_raw VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (aid, source, cc, None, f'headline {aid}', score, 'test:model',
                         day or date.today().isoformat(), duplicate, ai,
                         '요약 근거 ' * 60, 'Evidence summary', f'https://example.com/{aid}',
                         '', '', 0))
        self.db.commit()

    def links(self):
        return {r[0]: r[1] for r in self.db.execute('SELECT article_id,duplicate_of FROM articles_raw')}

    def test_scoped_dedup_preserves_other_country_and_old_rows(self):
        self.add(1); self.add(2, duplicate=1, ai=1)
        self.add(3, source=2, cc='JP'); self.add(4, source=2, cc='JP', duplicate=3, ai=1)
        self.add(5, day='2000-01-01'); self.add(6, day='2000-01-01', duplicate=5, ai=1)
        self.add(7, duplicate=1)  # keyword duplicate
        llm_dedup.run_dedup(self.db, Provider({'groups': []}), days=3, only_cc='US')
        self.assertEqual(self.links(), {1: None, 2: None, 3: None, 4: 3, 5: None, 6: 5, 7: 1})

    def test_api_failure_preserves_existing_links(self):
        self.add(1); self.add(2, duplicate=1, ai=1)
        with self.assertRaises(RuntimeError):
            llm_dedup.run_dedup(self.db, Provider(fail=True))
        self.assertEqual(self.links()[2], 1)

    def test_invalid_responses_preserve_existing_links(self):
        self.add(1); self.add(2, duplicate=1, ai=1)
        for response in ({}, None, {'groups': [[1, 999]]}, {'groups': [[1, 2], [2, 1]]},
                         {'groups': 'bad'}, {'groups': [[True, 2]]}):
            with self.subTest(response=response):
                llm_dedup.run_dedup(self.db, Provider(response))
                self.assertEqual(self.links()[2], 1)

    def test_write_failure_rolls_back_country(self):
        self.add(1); self.add(2, duplicate=1, ai=1); self.add(3)
        self.db.execute("""CREATE TRIGGER reject_update BEFORE UPDATE ON articles_raw
                         WHEN NEW.article_id=3 AND NEW.dup_by_ai=1
                         BEGIN SELECT RAISE(ABORT, 'write failed'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            llm_dedup.run_dedup(self.db, Provider({'groups': [[1, 3]]}))
        self.assertEqual(self.links(), {1: None, 2: 1, 3: None})

    def test_partial_response_preserves_failed_country(self):
        self.add(1); self.add(2, duplicate=1, ai=1)
        self.add(3, source=2, cc='JP'); self.add(4, source=2, cc='JP', duplicate=3, ai=1)
        p = Provider()
        p.complete_json_batch = lambda requests: {'US': {'groups': []}}
        stats = llm_dedup.run_dedup(self.db, p)
        self.assertEqual(self.links()[2], None)
        self.assertEqual(self.links()[4], 3)
        self.assertEqual(stats['failed'], 2)

    def test_briefing_ranks_before_limit_and_discovers_subject(self):
        self.add(1, source=2, score=65)
        self.add(2, source=2, score=65)
        for aid in range(3, 8):
            self.add(aid, source=2, score=65, duplicate=2)
        self.assertEqual(briefing._target_countries(self.db), ['US'])
        p = Provider({'summary_ko': '브리핑'})
        with patch.object(briefing.config, 'BRIEFING_MAX_ARTICLES', 1):
            briefing.run_briefing(self.db, p, briefing_type='daily')
        self.assertIn('headline 2', p.requests[0][2])
        self.assertNotIn('headline 1', p.requests[0][2])

    def test_oversized_request_preserves_links_and_reports_failure(self):
        for aid in range(1, 502):
            self.add(aid, duplicate=1 if aid==2 else None, ai=1 if aid==2 else 0)
        self.db.execute("UPDATE articles_raw SET title=?", ('x' * 160,))
        self.db.commit()
        p = Provider({'groups': []})
        stats = llm_dedup.run_dedup(self.db, p)
        self.assertEqual(p.requests, [])
        self.assertEqual(stats['failed'], 501)
        self.assertEqual(self.links()[2], 1)

    def test_empty_weekly_replaces_previous_briefing(self):
        self.add(1, source=3, cc='LA', score=60)
        p = Provider({'summary_ko': '과거 내용'})
        briefing.run_briefing(self.db, p, briefing_type='weekly', countries=['LA'], days=1)
        self.db.execute('UPDATE articles_raw SET ai_score=8')
        self.db.commit()
        p = Provider()
        briefing.run_briefing(self.db, p, briefing_type='weekly', countries=['LA'], days=1)
        row = self.db.execute('SELECT article_count,summary,issues,source_articles FROM country_briefings').fetchone()
        self.assertEqual(p.requests, [])
        self.assertEqual(row[0], 0)
        self.assertIn('충족', row[1])
        self.assertEqual(json.loads(row[2]), [])
        self.assertEqual(json.loads(row[3]), [])

    def test_more_than_40_includes_cross_boundary_pair(self):
        for aid in range(1, 82):
            self.add(aid)
        p = Provider({'groups': [[1, 81]]})
        llm_dedup.run_dedup(self.db, p)
        self.assertIn('81:', p.requests[0][2])
        self.assertIn(date.today().isoformat(), p.requests[0][2])
        self.assertEqual(self.links()[81], 1)

    def test_briefing_uses_subject_country_gate_and_full_summary(self):
        self.add(1, source=2, cc='US', score=65)
        self.add(2, cc='JP', score=90)
        self.add(3, score=8)
        self.add(4, score=65, duplicate=1)
        p = Provider({'summary_ko': '검증된 브리핑', 'summary_en': 'Verified briefing'})
        briefing.run_briefing(self.db, p, briefing_type='daily', countries=['US'])
        user = p.requests[0][2]
        self.assertIn('headline 1', user)
        for aid in [2, 3, 4]:
            self.assertNotIn(f'headline {aid}', user)
        self.assertIn('요약 근거 ' * 60, user)
        row = self.db.execute('SELECT article_count,source_articles FROM country_briefings').fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(json.loads(row[1]), ['https://example.com/1'])

    def test_empty_briefing_is_explicit_without_llm(self):
        self.add(1, source=3, cc='LA', score=8)
        p = Provider()
        briefing.run_briefing(self.db, p, briefing_type='daily', countries=['LA'])
        self.assertEqual(p.requests, [])
        row = self.db.execute('SELECT article_count,summary FROM country_briefings').fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 0)
        self.assertIn('충족', row[1])


if __name__ == '__main__':
    unittest.main()
