"""배포 차단 항목 AI 재보정·보류 export (2026-10-26 — 금액 감사 1~2건 때문에 전체 배포가 사흘 멈춤)."""
import json
import sqlite3
import unittest
from datetime import date
from pathlib import Path

import amount_repair
import export_json


class FakeProvider:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def complete(self, system, user, **kw):
        self.calls.append(user)
        return self.reply


def make_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript((Path(__file__).parents[1] / 'schema.sql').read_text(encoding='utf-8'))
    conn.execute("INSERT INTO media_sources(source_id, media_name, primary_country_code, language, tier)"
                 " VALUES(1, 'Mint', 'IN', 'en', 1)")
    conn.execute("INSERT INTO media_source_feeds(feed_id, source_id, feed_url, feed_section) VALUES(1, 1, 'https://m/rss', 'main')")
    today = date.today().isoformat()
    for aid, link in ((1, 'https://m/a'), (2, 'https://m/b')):
        conn.execute(
            "INSERT INTO articles_raw(article_id, feed_id, source_id, title, link, content_hash, published_at,"
            " ai_score, ai_model, topics, summary_en, summary_ko, title_ko, title_en)"
            " VALUES(?,1,1,'t',?,?,?,70,'test:model','MARKETS',"
            " 'Adani raised $133 billion', '아다니가 133억 달러를 조달했다', '제목', 'Title')",
            (aid, link, f'h{aid}', today))
    conn.commit()
    return conn


class AmountRepairTests(unittest.TestCase):
    def test_fixes_korean_field_only_when_amount_then_matches(self):
        conn = make_db()
        hold = {'links': {'https://m/a': [['q_en', 'q', 'Adani raised $133 billion', '아다니가 133억 달러를 조달했다']]},
                'briefs': []}
        p = FakeProvider('아다니가 1,330억 달러를 조달했다')
        amount_repair.repair(conn, hold, provider=p)
        self.assertIn('1,330억 달러', p.calls[0])     # 코드가 계산한 정확한 한국어 금액을 지정
        self.assertEqual(conn.execute("SELECT summary_ko FROM articles_raw WHERE article_id=1").fetchone()[0],
                         '아다니가 1,330억 달러를 조달했다')
        self.assertEqual(conn.execute("SELECT summary_ko FROM articles_raw WHERE article_id=2").fetchone()[0],
                         '아다니가 133억 달러를 조달했다')

    def test_keeps_text_when_ai_fix_still_mismatches(self):
        conn = make_db()
        hold = {'links': {'https://m/a': [['q_en', 'q', 'Adani raised $133 billion', '아다니가 133억 달러를 조달했다']]}}
        log = amount_repair.repair(conn, hold, provider=FakeProvider('아다니가 13억 달러를 조달했다'))
        self.assertEqual(conn.execute("SELECT summary_ko FROM articles_raw WHERE article_id=1").fetchone()[0],
                         '아다니가 133억 달러를 조달했다')
        self.assertTrue(any('실패' in line for line in log))

    def test_english_only_pair_and_untranslated_title_are_not_sent_to_ai(self):
        conn = make_db()
        hold = {'links': {'https://m/a': [['q_en', 't_en', 'x', 'y'], ['t_en', 't', 'Rp 400 trillion', 'Rp 400 Triliun']]}}
        p = FakeProvider('')
        amount_repair.repair(conn, hold, provider=p)
        self.assertEqual(p.calls, [])

    def test_weekly_issue_item_is_fixed_in_json_list(self):
        conn = make_db()
        conn.execute("INSERT INTO country_briefings(cc, briefing_date, briefing_type, summary, summary_en, issues, issues_en)"
                     " VALUES('IN', '2026-10-26', 'weekly', '요약', 'Summary', ?, ?)",
                     (json.dumps(['133억 달러 조달', '금리 동결'], ensure_ascii=False),
                      json.dumps(['$133 billion raised', 'rates held'])))
        amount_repair.repair(conn, {'briefs': [['IN', 'weekly']]}, provider=FakeProvider('1,330억 달러 조달'))
        issues = json.loads(conn.execute("SELECT issues FROM country_briefings").fetchone()[0])
        self.assertEqual(issues, ['1,330억 달러 조달', '금리 동결'])


class HoldExportTests(unittest.TestCase):
    def tearDown(self):
        export_json._HOLD_ACTIVE = False

    def test_held_link_and_brief_are_hidden_from_export_queries(self):
        conn = make_db()
        conn.execute("INSERT INTO country_briefings(cc, briefing_date, briefing_type, summary) VALUES('IN', '2026-10-26', 'weekly', 's')")
        export_json.apply_hold(conn, {'links': {'https://m/a': []}, 'briefs': [['IN', 'weekly']]})
        self.assertEqual([r[0] for r in conn.execute("SELECT article_id FROM articles_raw")], [2])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM country_briefings").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM main.articles_raw").fetchone()[0], 2)

    def test_highlight_with_held_source_is_dropped(self):
        conn = make_db()
        items = [{'headline_en': 'A', 'source_article_ids': [1]}, {'headline_en': 'B', 'source_article_ids': [2]}]
        conn.execute("INSERT INTO daily_highlights(date, items) VALUES('2026-10-26', ?)", (json.dumps(items),))
        export_json.apply_hold(conn, {'links': {'https://m/a': []}})
        self.assertEqual([h['headline_en'] for h in export_json._daily_highlights(conn)], ['B'])


if __name__ == '__main__':
    unittest.main()
