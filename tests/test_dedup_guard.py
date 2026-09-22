"""AI 중복판정 신뢰성 보강(2026-09-21) 테스트 — 대표 선정·오묶음 가드·소급 보정."""
import sqlite3
import unittest
from datetime import date

import llm_dedup as L


class Provider:
    model_id = 'test:model'

    def __init__(self, response):
        self.response = response

    def complete_json_batch(self, requests, **_kwargs):
        return {r[0]: self.response for r in requests}


FED_PREVIEW = 'Warsh faces a tough battle as the Fed girds for expected interest rate hike'
FED_DECISION = 'Fed raises rates in first major step by Warsh to contain inflation'
FED_SUMMARY = ('The Federal Reserve raised interest rates by a quarter point, Chairman Warsh said, '
               'as inflation stayed elevated and markets priced the FOMC decision.')
UAE_SUMMARY = 'Fasset, a UAE digital asset company, plans to list a dirham-denominated stablecoin.'


class DedupGuardTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE media_sources(source_id INTEGER PRIMARY KEY,
                primary_country_code TEXT, tier INTEGER);
            INSERT INTO media_sources VALUES(1,'US',1),(2,'GLOBAL',1);
            CREATE TABLE articles_raw(article_id INTEGER PRIMARY KEY, source_id INTEGER,
                primary_country TEXT, title_ko TEXT, title TEXT, ai_score INTEGER,
                ai_model TEXT, published_at TEXT, duplicate_of INTEGER, dup_by_ai INTEGER,
                summary_ko TEXT, summary_en TEXT, link TEXT, event_type TEXT,
                korean_fi TEXT, personnel_move INTEGER);
        ''')
        self.addCleanup(self.db.close)

    def add(self, aid, title, summary, day='2026-09-16', score=72, source=1,
            duplicate=None, ai=0, pc='US'):
        self.db.execute(
            'INSERT INTO articles_raw VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (aid, source, pc, None, title, score, 'test:model', day, duplicate, ai,
             '', summary, f'https://x/{aid}', '', '', 0))
        self.db.commit()

    def links(self):
        return {r[0]: r[1] for r in self.db.execute('SELECT article_id,duplicate_of FROM articles_raw')}

    # ── 대표 선정 ───────────────────────────────────────────────
    def meta(self, **kw):
        base = {'score': 72, 'pub': '2026-09-16', 'titles': ['x'], 'media_cc': 'US'}
        base.update(kw)
        return base

    def test_pick_rep_prefers_newest_when_scores_tie(self):
        m = {1: self.meta(pub='2026-09-14'), 2: self.meta(pub='2026-09-16')}
        self.assertEqual(L.pick_rep([1, 2], m), 2)

    def test_pick_rep_score_beats_recency(self):
        m = {1: self.meta(score=78, pub='2026-09-14'), 2: self.meta(score=62, pub='2026-09-16')}
        self.assertEqual(L.pick_rep([1, 2], m), 1)

    def test_pick_rep_demotes_preview_even_with_higher_score(self):
        m = {1: self.meta(score=78, titles=[FED_PREVIEW]), 2: self.meta(score=62, titles=[FED_DECISION])}
        self.assertEqual(L.pick_rep([1, 2], m), 2)

    def test_pick_rep_prefers_local_media_for_subject_country(self):
        m = {1: self.meta(media_cc='GLOBAL', pub='2026-09-16'), 2: self.meta(media_cc='US', pub='2026-09-14')}
        self.assertEqual(L.pick_rep([1, 2], m, 'US'), 2)
        self.assertEqual(L.pick_rep([1, 2], m), 1)   # subject 미지정이면 최신

    def test_pick_rep_old_tiebreak_keeps_lower_id(self):
        m = {1: self.meta(), 2: self.meta()}
        self.assertEqual(L.pick_rep([1, 2], m), 1)

    # ── 가드 ────────────────────────────────────────────────────
    def tok_meta(self, *texts):
        return {i + 1: {'tok': L._tokens(t)} for i, t in enumerate(texts)}

    def test_split_releases_unrelated_member(self):
        meta = self.tok_meta(FED_DECISION + ' ' + FED_SUMMARY, FED_SUMMARY + ' Fed decision Warsh',
                             'Fasset stablecoin ' + UAE_SUMMARY)
        comps = sorted(sorted(c) for c in L.split_by_overlap([1, 2, 3], meta, 0.25))
        self.assertEqual(comps, [[1, 2], [3]])

    def test_split_keeps_chain_connected(self):
        a = 'alpha bravo charlie delta echo foxtrot golf hotel'
        b = 'echo foxtrot golf hotel india juliet kilo lima'   # a와 겹침 0.5
        c = 'india juliet kilo lima mike november oscar papa'  # a와 0, b와 0.5
        meta = self.tok_meta(a, b, c)
        self.assertEqual([sorted(x) for x in L.split_by_overlap([1, 2, 3], meta, 0.25)], [[1, 2, 3]])

    def test_pair_guard_allows_same_named_event_below_global_threshold(self):
        meta = {
            1: {'tok': L._tokens('BOJ rate hike currency alliance analysis japan'),
                'title_tok': L._tokens('BOJ rate hike currency alliance')},
            2: {'tok': L._tokens('BOJ raises benchmark rate policy shift inflation'),
                'title_tok': L._tokens('BOJ raises benchmark rate')},
        }
        self.assertTrue(L.pair_passes_guard(1, 2, meta, 0.25))

    def test_pair_guard_rejects_only_generic_event_words(self):
        meta = {
            1: {'tok': L._tokens('bank rate decision alpha lending'),
                'title_tok': L._tokens('bank rate decision')},
            2: {'tok': L._tokens('bank rate decision beta deposits'),
                'title_tok': L._tokens('bank rate decision')},
        }
        self.assertFalse(L.pair_passes_guard(1, 2, meta, 0.8))

    def test_normalize_groups_accepts_both_formats_and_rejects_bad(self):
        self.assertEqual(L._normalize_groups([[1, 2]]), [[1, 2]])
        self.assertEqual(L._normalize_groups([{'event': 'x', 'ids': [1, 2]}]), [[1, 2]])
        self.assertIsNone(L._normalize_groups('bad'))
        self.assertIsNone(L._normalize_groups([{'event': 'x'}]))

    def test_pair_decisions_require_every_pair_once(self):
        lookup = {'p1': (1, 2), 'p2': (3, 4)}
        good = {'decisions': [
            {'pair_id': 'p1', 'same': True}, {'pair_id': 'p2', 'same': False},
        ]}
        self.assertEqual(L._valid_same_pairs(good, lookup), [(1, 2)])
        self.assertIsNone(L._valid_same_pairs(
            {'decisions': [{'pair_id': 'p1', 'same': True}]}, lookup))

    def test_edge_groups_do_not_merge_transitive_chain(self):
        meta = {
            1: self.meta(pub='2026-09-16'),
            2: self.meta(pub='2026-09-15'),
            3: self.meta(pub='2026-09-14'),
        }
        self.assertEqual(L._groups_from_edges({(1, 2), (2, 3)}, meta, 'US'), [[1, 2]])

    def test_pair_prompt_omits_summary(self):
        meta = {
            1: {**self.meta(), 'display_title': 'First title', 'summary': 'LEAKED SUMMARY'},
            2: {**self.meta(), 'display_title': 'Second title', 'summary': 'OTHER SUMMARY'},
        }
        chunks = L._pair_chunks('US', [(1, 2, 0.5)], meta)
        self.assertIn('First title', chunks[0][2])
        self.assertNotIn('LEAKED SUMMARY', chunks[0][2])

    def test_run_dedup_guard_releases_unrelated_and_elects_latest_rep(self):
        self.add(1, FED_PREVIEW, FED_SUMMARY, day='2026-09-14')
        self.add(2, FED_DECISION, FED_SUMMARY, day='2026-09-16')
        self.add(3, 'Fasset to list dirham stablecoin', UAE_SUMMARY, day='2026-09-16')
        stats = L.run_dedup(self.db, Provider({'groups': [{'event': 'Fed', 'ids': [1, 2]}]}), days=None)
        self.assertEqual(self.links(), {1: 2, 2: None, 3: None})   # 결정 기사가 대표, 무관 기사는 풀림
        self.assertEqual(stats['released'], 0)
        self.assertEqual(stats['marked'], 1)

    def test_run_dedup_dry_run_does_not_write(self):
        self.add(1, FED_PREVIEW, FED_SUMMARY, day='2026-09-14')
        self.add(2, FED_DECISION, FED_SUMMARY, day='2026-09-16')
        stats = L.run_dedup(self.db, Provider({'groups': [[1, 2]]}), days=None, dry_run=True)
        self.assertEqual(self.links(), {1: None, 2: None})
        self.assertEqual([sorted(g) for g in stats['groups']['US']], [[1, 2]])

    def test_run_dedup_repoints_keyword_duplicates_to_new_rep(self):
        self.add(1, FED_PREVIEW, FED_SUMMARY, day='2026-09-14')
        self.add(2, FED_DECISION, FED_SUMMARY, day='2026-09-16')
        self.add(3, FED_PREVIEW, FED_SUMMARY, day='2026-09-14', duplicate=1, ai=0, score=None)  # 키워드 중복
        L.run_dedup(self.db, Provider({'groups': [[1, 2]]}), days=None)
        self.assertEqual(self.links(), {1: 2, 2: None, 3: 2})

    # ── 소급 보정 ────────────────────────────────────────────────
    def seed_old_groups(self):
        # 그룹A: 프리뷰가 대표(옛 규칙), 결정 기사가 자식 + 키워드 중복 1건
        self.add(1, FED_PREVIEW, FED_SUMMARY, day='2026-09-14')
        self.add(2, FED_DECISION, FED_SUMMARY, day='2026-09-16', duplicate=1, ai=1)
        self.add(3, FED_PREVIEW, FED_SUMMARY, day='2026-09-14', duplicate=1, ai=0, score=None)
        # 그룹B: 무관한 자식이 섞인 오묶음
        self.add(4, 'Fed decision Warsh', FED_SUMMARY, day='2026-09-16')
        self.add(5, 'Fasset to list dirham stablecoin', UAE_SUMMARY, day='2026-09-16', duplicate=4, ai=1)

    def test_repair_dry_run_changes_nothing(self):
        self.seed_old_groups()
        before = self.links()
        s = L.repair_existing(self.db, apply=False)
        self.assertEqual(self.links(), before)
        self.assertEqual(s['released'], 1)
        self.assertFalse(s['apply'])

    def test_repair_apply_reelects_rep_releases_and_repoints(self):
        self.seed_old_groups()
        s = L.repair_existing(self.db, apply=True)
        links = self.links()
        self.assertEqual(links[1], 2)           # 프리뷰는 이제 자식
        self.assertIsNone(links[2])             # 결정 기사가 대표
        self.assertEqual(links[3], 2)           # 키워드 중복도 새 대표로 이동
        self.assertIsNone(links[5])             # 무관 기사는 풀림
        self.assertEqual(s['rep_changed'], 1)
        flags = {r[0]: r[1] for r in self.db.execute('SELECT article_id,dup_by_ai FROM articles_raw')}
        self.assertEqual(flags[2], 0)
        self.assertEqual(flags[1], 1)
        self.assertEqual(flags[5], 0)
        # 재실행해도 바뀌지 않는다(멱등)
        L.repair_existing(self.db, apply=True)
        self.assertEqual(self.links(), links)


if __name__ == '__main__':
    unittest.main()
