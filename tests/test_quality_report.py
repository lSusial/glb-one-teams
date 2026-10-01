import sqlite3
import unittest
from datetime import date
from pathlib import Path

import quality_report


class QualityReportTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        schema = (Path(__file__).parents[1] / 'schema.sql').read_text(encoding='utf-8')
        self.db.executescript(schema)
        self.db.execute(
            "INSERT INTO media_sources(source_id,media_name,primary_country_code,language,tier) "
            "VALUES(1,'Test News','US','en',1)"
        )
        self.db.execute(
            "INSERT INTO media_source_feeds(feed_id,source_id,feed_url,feed_section,last_status) "
            "VALUES(1,1,'https://example.com/rss','top',200)"
        )
        self.addCleanup(self.db.close)

    def add(self, aid, score, full_text='', duplicate_of=None):
        self.db.execute(
            """INSERT INTO articles_raw(
                   article_id,feed_id,source_id,title,link,summary,published_at,content_hash,
                   filter_decision,llm_prefilter,ai_score,summary_en,full_text,duplicate_of)
               VALUES(?,1,1,?,?,?,?,?,'passed','keep',?,?,?,?)""",
            (aid, f'Headline {aid}', f'https://example.com/{aid}', 'Snippet',
             date.today().isoformat(), f'hash-{aid}', score, 'English summary', full_text, duplicate_of),
        )
        self.db.commit()

    def test_report_measures_funnel_country_and_score_concentration(self):
        self.add(1, 58, 'body')
        self.add(2, 58, '')
        self.add(3, None, '')
        report = quality_report.build_report(self.db)
        self.assertEqual(report['summary']['active'], 2)
        self.assertEqual(report['summary']['active_fulltext_pct'], 50.0)
        self.assertEqual(report['summary']['score_mode_pct'], 100.0)
        us = next(c for c in report['countries'] if c['cc'] == 'US')
        self.assertEqual((us['raw'], us['active'], us['displayed']), (3, 2, 2))
        codes = {a['code'] for a in report['alerts']}
        self.assertIn('SCORE_CONCENTRATION', codes)
        self.assertIn('FULLTEXT_LOW', codes)

    def test_evidence_rewards_body_and_independent_sources(self):
        self.add(1, 60, '')
        row = self.db.execute(
            "SELECT a.full_text,a.summary_en,a.source_conflict,m.tier "
            "FROM articles_raw a JOIN media_sources m USING(source_id) WHERE article_id=1"
        ).fetchone()
        weak = quality_report.evidence_score(row, 0)
        self.db.execute("UPDATE articles_raw SET full_text='body' WHERE article_id=1")
        row = self.db.execute(
            "SELECT a.full_text,a.summary_en,a.source_conflict,m.tier "
            "FROM articles_raw a JOIN media_sources m USING(source_id) WHERE article_id=1"
        ).fetchone()
        self.assertGreater(quality_report.evidence_score(row, 2), weak)


if __name__ == '__main__':
    unittest.main()
