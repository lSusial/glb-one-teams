"""Google News 링크 해소(2026-09-29) — 429 차단 방지·노출 기사 우선 해소 테스트."""
import sqlite3
import sys
import types
import unittest
from datetime import date
from unittest.mock import patch

import fulltext

GN = 'https://news.google.com/rss/articles/'


class FakeDecoder:
    """googlenewsdecoder.gnewsdecoder 대역. responses[url] = decoded_url 또는 '429'."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, interval=None):
        self.calls.append(url)
        r = self.responses.get(url, '429')
        if r == '429':
            return {'status': False, 'message': 'Request error in decode_url: 429 Client Error: Too Many Requests'}
        return {'status': True, 'decoded_url': r}


def install(decoder):
    mod = types.ModuleType('googlenewsdecoder')
    mod.gnewsdecoder = decoder
    return patch.dict(sys.modules, {'googlenewsdecoder': mod})


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE articles_raw(article_id INTEGER PRIMARY KEY, link TEXT, ai_score INTEGER,
                duplicate_of INTEGER, published_at TEXT, llm_prefilter TEXT, filter_score REAL,
                full_text TEXT);
        ''')
        self.addCleanup(self.db.close)

    def add(self, aid, link, score=None, keep='keep', fscore=1.0, dup=None):
        self.db.execute('INSERT INTO articles_raw VALUES(?,?,?,?,?,?,?,NULL)',
                        (aid, link, score, dup, date.today().isoformat(), keep, fscore))
        self.db.commit()

    def link(self, aid):
        return self.db.execute('SELECT link FROM articles_raw WHERE article_id=?', (aid,)).fetchone()[0]

    def fulltext_state(self, aid):
        return self.db.execute(
            'SELECT fulltext_status, fulltext_failure_reason FROM articles_raw WHERE article_id=?',
            (aid,),
        ).fetchone()

    def test_decode_stops_after_consecutive_429(self):
        dec = FakeDecoder({})                       # 전부 429
        with install(dec):
            out = fulltext.decode_serial([(i, f'{GN}{i}') for i in range(6)])
        self.assertEqual(out, {})
        self.assertEqual(len(dec.calls), fulltext._BLOCK_AFTER)   # 3회 연속 429 후 중단(나머지 요청 안 함)

    def test_decode_skips_plain_links_and_resets_block_count(self):
        dec = FakeDecoder({f'{GN}2': 'https://real/2'})
        with install(dec):
            out = fulltext.decode_serial([(1, 'https://direct/1'), (2, f'{GN}2'), (3, f'{GN}3')])
        self.assertEqual(out, {2: 'https://real/2'})
        self.assertEqual(dec.calls, [f'{GN}2', f'{GN}3'])   # 일반 URL은 요청하지 않음

    def test_display_links_resolved_by_score_within_limit(self):
        self.add(1, f'{GN}1', score=40)
        self.add(2, f'{GN}2', score=80)
        self.add(3, f'{GN}3', score=None)           # 미채점 — 대상 아님
        self.add(4, 'https://direct/4', score=90)   # 이미 원문 링크
        dec = FakeDecoder({f'{GN}1': 'https://real/1', f'{GN}2': 'https://real/2'})
        with install(dec):
            s = fulltext.resolve_display_links(self.db, days=2, limit=1)
        self.assertEqual(s['resolved'], 1)
        self.assertEqual(self.link(2), 'https://real/2')   # 점수 높은 것부터
        self.assertEqual(self.fulltext_state(2)['fulltext_status'], 'pending')
        self.assertEqual(self.link(1), f'{GN}1')           # 한도 밖
        self.assertEqual(dec.calls, [f'{GN}2'])

    def test_display_links_include_siblings_after_representatives(self):
        # 모달 '관련 기사 링크'는 대표 기사에 묶인 형제(duplicate_of)라 대표만 해소하면 GN 링크가 남는다
        self.add(1, f'{GN}1', score=80)                 # 대표
        self.add(2, f'{GN}2', score=None, dup=1)        # 대표의 형제(미채점 키워드 중복 포함)
        self.add(3, f'{GN}3', score=30)                 # 다른 대표(점수 낮음)
        self.add(4, f'{GN}4', score=None, dup=99)       # 존재하지 않는 대표의 자식 — 대상 아님
        dec = FakeDecoder({f'{GN}{i}': f'https://real/{i}' for i in range(1, 5)})
        with install(dec):
            s = fulltext.resolve_display_links(self.db, days=2, limit=10)
        self.assertEqual(dec.calls, [f'{GN}1', f'{GN}3', f'{GN}2'])   # 대표 먼저(점수순) → 형제
        self.assertEqual(s['resolved'], 3)
        self.assertEqual(self.link(4), f'{GN}4')

    def test_fulltext_does_not_extract_unresolved_google_links(self):
        self.add(1, f'{GN}1')
        self.add(2, 'https://direct/2')
        dec = FakeDecoder({})
        with install(dec), patch.object(fulltext, '_extract', side_effect=lambda u: f'text of {u}') as ex, \
                patch('requests.get') as get:
            s = fulltext.run_fulltext(self.db, limit=10, days=2)
        ex.assert_called_once_with('https://direct/2')     # GN 링크로는 본문 추출 안 함
        get.assert_not_called()                            # 성공률 0%인 리다이렉트 폴백 요청도 안 보냄
        self.assertEqual((s['extracted'], s['resolved'], s['failed']), (1, 0, 1))
        self.assertEqual(tuple(self.fulltext_state(1)), ('unresolved_url', 'google_news_decode_failed'))
        self.assertEqual(tuple(self.fulltext_state(2)), ('ok', None))

    def test_recent_extraction_failure_is_not_retried_immediately(self):
        self.add(1, 'https://direct/1')
        with patch.object(fulltext, '_extract', return_value=None):
            first = fulltext.run_fulltext(self.db, limit=10, days=2)
            second = fulltext.run_fulltext(self.db, limit=10, days=2)
        self.assertEqual((first['total'], first['failed']), (1, 1))
        self.assertEqual(second['total'], 0)
        self.assertEqual(tuple(self.fulltext_state(1)), ('extract_failed', 'empty_or_blocked'))


if __name__ == '__main__':
    unittest.main()
