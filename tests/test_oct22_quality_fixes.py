"""10/22 실데이터 감사에서 확인한 오류의 재현 테스트."""
import json
import sqlite3
import unittest
from unittest.mock import patch

import category_guard
import db
import export_json
import llm_dedup
import llm_expand


class QualityFixesTests(unittest.TestCase):
    def connection(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript('''
            CREATE TABLE media_sources(source_id INTEGER PRIMARY KEY,
                primary_country_code TEXT, media_name TEXT, tier INTEGER);
            INSERT INTO media_sources VALUES(1,'IN','News',1);
            CREATE TABLE articles_raw(article_id INTEGER PRIMARY KEY, source_id INTEGER DEFAULT 1,
                primary_country TEXT DEFAULT 'IN', title TEXT, title_ko TEXT, title_en TEXT,
                summary TEXT DEFAULT '', summary_en TEXT DEFAULT '', full_text TEXT DEFAULT '',
                link TEXT, ai_score INTEGER, published_at TEXT, duplicate_of INTEGER,
                dup_by_ai INTEGER DEFAULT 0, expanded_summary TEXT, expanded_summary_en TEXT);
        ''')
        return conn

    def test_bi_rate_announcement_is_removed_when_result_arrives(self):
        items = [
            (1, '2026-10-20', 'BI Rate Announcement Due Tomorrow as OJK Leaves Lending Rate Adjustments to Market'),
            (2, '2026-10-21', 'Bank Indonesia holds BI Rate at 5.75%, raises deposit facility to 5%'),
            (3, '2026-10-20', 'RBI rate announcement due tomorrow'),
        ]
        self.assertEqual(llm_dedup.superseded_ids(items), {1})

    def test_english_canonical_title_overrides_korean_imminent(self):
        self.assertFalse(llm_dedup.is_preview(
            '영국 물가 발표, 금리인상 임박 관측',
            'UK September inflation fuels expectations of imminent BoE rate rise'))
        self.assertTrue(llm_dedup.is_preview('금리 결정 임박', 'RBI expected to raise repo rate'))
        self.assertTrue(llm_dedup.is_preview('캄보디아 최저임금 표결 임박'))

    def test_refresh_representatives_keeps_membership_and_keyword_children(self):
        conn = self.connection()
        conn.executescript('''
            INSERT INTO articles_raw(article_id,title,title_en,ai_score,published_at,duplicate_of,dup_by_ai)
            VALUES (1,'Trade deal','Trade deal',54,'2026-10-14',NULL,0),
                   (2,'Trade deal update','Trade deal update',64,'2026-10-21',1,1),
                   (3,'Copy','Copy',NULL,'2026-10-21',1,0),
                   (4,'Unrelated','Unrelated',90,'2026-10-21',NULL,0);
        ''')
        stats = llm_dedup.refresh_representatives(conn, days=7, apply=False)
        self.assertEqual(stats['rep_changed'], 1)
        self.assertEqual(conn.execute('SELECT duplicate_of FROM articles_raw WHERE article_id=2').fetchone()[0], 1)
        llm_dedup.refresh_representatives(conn, days=7, apply=True)
        links = dict(conn.execute('SELECT article_id,duplicate_of FROM articles_raw'))
        self.assertEqual(links, {1: 2, 2: None, 3: 2, 4: None})

    def test_visible_ids_include_low_score_cards_but_not_related_links(self):
        conn = self.connection()
        conn.executescript("""
            INSERT INTO articles_raw(article_id,link) VALUES
                (1,'https://x/1'),(2,'https://x/2'),(3,'https://x/3');
        """)
        card = {'t': 'Social story', 'u': 'https://x/1',
                'rl': [{'t': 'Related source', 'u': 'https://x/2'}]}
        with patch.object(export_json, 'export_countries', return_value={'countries': [{'articles': [card]}]}) as countries, \
             patch.object(export_json, '_compute_topics', return_value=[]), \
             patch.object(export_json, '_compute_top_news', return_value=[]), \
             patch.object(export_json, '_daily_highlights', return_value=[{'source_articles': [{'t': 'Source', 'u': 'https://x/3'}]}]):
            self.assertEqual(export_json.visible_article_ids(conn), {1, 3})
            countries.assert_called_once_with(conn, write=False)

    def test_explicit_visible_ids_expand_low_score_social_and_watch_cards(self):
        conn = self.connection()
        conn.executescript("""
            INSERT INTO articles_raw(article_id,title,link,ai_score,published_at)
            VALUES(1,'Social news','https://x/1',45,'2026-10-21'),
                  (2,'High score elsewhere','https://x/2',99,'2026-10-21');
        """)
        class Provider:
            def complete_json_batch(self, requests):
                return {r[0]: {'expanded_summary_en': 'Social news.', 'expanded_summary_ko': '사회 소식이다.'} for r in requests}
        with patch.object(llm_expand, '_cluster_sources', return_value=[]):
            llm_expand.run_expand(conn, Provider(), article_ids={1})
        self.assertIsNotNone(conn.execute('SELECT expanded_summary FROM articles_raw WHERE article_id=1').fetchone()[0])
        self.assertIsNone(conn.execute('SELECT expanded_summary FROM articles_raw WHERE article_id=2').fetchone()[0])
        self.assertEqual(llm_expand.run_expand(conn, Provider(), article_ids=set())['total'], 0)

    def test_synthesis_sources_must_pass_same_story_guard(self):
        conn = self.connection()
        conn.executescript("""
            INSERT INTO articles_raw(article_id,title,title_en,summary_en,link)
            VALUES(2,'Fairfax fertiliser exports','Fairfax fertiliser exports to India',
                'Fairfax is expanding Canadian fertiliser exports to India.','https://x/2');
        """)
        rep = {'title': 'Fairfax buys IDBI bank', 'title_en': 'Fairfax buys IDBI bank',
               'summary_en': 'Fairfax will buy IDBI bank.', 'link': 'https://x/1', 'media_name': 'News'}
        links = export_json._related_links(rep, [{'t': 'Fairfax exports', 'u': 'https://x/2', 'src': 'News'}], [], [], conn=conn)
        self.assertEqual([x['u'] for x in links], ['https://x/1'])

    def test_url_lookup_uses_current_root_instead_of_older_duplicate(self):
        conn = self.connection()
        conn.executescript("""
            INSERT INTO articles_raw(article_id,title,link,ai_score,published_at,duplicate_of)
            VALUES (1,'Old headline','https://x/same',90,'2026-10-20',2),
                   (2,'Current root','https://x/same',65,'2026-10-21',NULL);
        """)
        self.assertEqual(db.article_by_url(conn, 'https://x/same')['article_id'], 2)

    def test_high_level_diplomatic_investment_council_is_grounded_geo(self):
        text = ('US and Uzbekistan sign investment agreements. President Mirziyoyev met '
                'Secretary of State Marco Rubio and presidential special envoy Sergio Gor.')
        self.assertIsNone(category_guard.ungrounded_geo_reason(text))
        self.assertIsNotNone(category_guard.ungrounded_geo_reason('A domestic company signed investment agreements.'))

    def test_same_quarter_earnings_links_survive_wording_differences(self):
        rep = {'title_en': 'CATL profit growth slows in Q3 as analysts flag EV demand risks',
               'summary_en': 'CATL reported third-quarter net profit of 21.25 billion yuan.'}
        child = {'title_en': 'CATL shares plunge 10% in Hong Kong after weak third-quarter profit',
                 'summary_en': 'CATL shares fell after third-quarter profit disappointed investors.'}
        self.assertTrue(export_json._same_story_for_link(rep, child))
        other_quarter = dict(child, title_en=child['title_en'].replace('third-quarter', 'first-quarter'),
                             summary_en=child['summary_en'].replace('third-quarter', 'first-quarter'))
        self.assertFalse(export_json._same_story_for_link(rep, other_quarter))

class PipelinePreflightTests(unittest.TestCase):
    def test_permission_failure_stops_before_backup_and_collect(self):
        import pathlib
        import subprocess
        import tempfile
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / 'scripts').mkdir()
            (root / '.venv/bin').mkdir(parents=True)
            shutil.copy2(pathlib.Path(__file__).resolve().parents[1] / 'scripts/daily_pipeline.sh', root / 'scripts/daily_pipeline.sh')
            python = root / '.venv/bin/python'
            python.write_text('#!/bin/sh\necho "Operation not permitted" >&2\nexit 1\n')
            python.chmod(0o755)
            result = subprocess.run(['bash', str(root / 'scripts/daily_pipeline.sh')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('전체 디스크 접근 권한', result.stdout)
            self.assertFalse(list((root / 'data/backups').iterdir()))
            self.assertFalse((root / 'data/logs/.pipeline.pid').exists())


    def test_failed_backup_does_not_start_collection(self):
        import pathlib
        import subprocess
        import tempfile
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / 'scripts').mkdir()
            (root / '.venv/bin').mkdir(parents=True)
            shutil.copy2(pathlib.Path(__file__).resolve().parents[1] / 'scripts/daily_pipeline.sh', root / 'scripts/daily_pipeline.sh')
            python = root / '.venv/bin/python'
            python.write_text('#!/bin/sh\nif [ "$1" != "-c" ]; then touch started; fi\nexit 0\n')
            python.chmod(0o755)
            result = subprocess.run(['bash', str(root / 'scripts/daily_pipeline.sh')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('DB 백업 실패', result.stdout)
            self.assertFalse((root / 'started').exists())


if __name__ == '__main__':
    unittest.main()
