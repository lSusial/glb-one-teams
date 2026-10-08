import json
import re
import sqlite3
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import briefing
import config
import db
import export_json
import llm_dedup
import llm_ranker
import llm_expand
import llm_translate
import numeric_guard
import taxonomy


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

    def complete_json(self, system, user, **kw):
        if self.fail:
            raise RuntimeError('API unavailable')
        return self.response


class SchemaContractTests(unittest.TestCase):
    def test_fresh_schema_contains_runtime_columns_and_history(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        schema = (Path(__file__).parents[1] / 'schema.sql').read_text(encoding='utf-8')
        conn.executescript(schema)
        article_cols = {r[1] for r in conn.execute('PRAGMA table_info(articles_raw)')}
        briefing_cols = {r[1] for r in conn.execute('PRAGMA table_info(country_briefings)')}
        self.assertTrue({
            'ai_score_factors', 'market_importance', 'kb_relevance',
            'summary_en', 'title_en', 'event_type', 'source_links',
            'source_conflict', 'publisher_name',
            'primary_country', 'dup_by_ai', 'korean_fi', 'personnel_move',
            'expanded_summary', 'expanded_summary_en', 'fulltext_status',
            'fulltext_attempted_at', 'fulltext_failure_reason',
        } <= article_cols)
        self.assertTrue({
            'summary_en', 'issues_en', 'outlook_en', 'keywords_en', 'key_stat_en',
            'week_start', 'week_end',
        } <= briefing_cols)
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='indicator_history'"
        ).fetchone())


class ArchiveLinkExportTests(unittest.TestCase):
    """태그/토픽 아카이브 링크는 국가탭뿐 아니라 모니터링·홈 핵심뉴스·미진출국 카드에서도 빠져야 한다.
    2026-10-01 배포본: 국가탭에서 걸러진 인옥스 IPO(ET '/topic/pvr-inox-compensation-order')가 모니터링 탭에 남음."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript((Path(__file__).parents[1] / 'schema.sql').read_text(encoding='utf-8'))
        self.db.executemany(
            'INSERT INTO media_sources(source_id, media_name, primary_country_code, language, tier) VALUES(?,?,?,?,?)',
            [(1, 'Economic Times', 'IN', 'en', 1), (2, 'GNews Philippines', 'PH', 'en', 2)])
        self.db.executemany('INSERT INTO media_source_feeds(feed_id, source_id, feed_url, feed_section) VALUES(?,?,?,?)',
                            [(1, 1, 'https://et/rss', 'main'), (2, 2, 'https://gn/ph', 'main')])
        today = date.today().isoformat()
        rows = [
            (1, 1, 'Inox Clean Energy to file IPO', 'https://economictimes.indiatimes.com/topic/pvr-inox-compensation-order'),
            (2, 1, 'RBI holds repo rate steady', 'https://economictimes.indiatimes.com/news/economy/rbi-holds/articleshow/1.cms'),
            (3, 2, 'BSP tightens casino payment rules', 'https://www.inquirer.net/tags/casino'),
            (4, 2, 'BSP keeps policy rate unchanged', 'https://www.inquirer.net/business/bsp-rate'),
        ]
        for aid, src, title, link in rows:
            self.db.execute(
                "INSERT INTO articles_raw(article_id, feed_id, source_id, title, link, content_hash, published_at,"
                " ai_score, ai_model, topics, event_type, summary_en, summary_ko, title_ko, title_en)"
                " VALUES(?,?,?,?,?,?,?,70,'test:model','MARKETS','REG',?,?,?,?)",
                (aid, src, src, title, link, f'h{aid}', today, f'{title} summary', f'{title} 요약', title, title))
        self.db.commit()

    def links(self, obj):
        if isinstance(obj, dict):
            return ([obj['u']] if 'u' in obj else []) + [u for v in obj.values() for u in self.links(v)]
        if isinstance(obj, list):
            return [u for v in obj for u in self.links(v)]
        return []

    def test_monitoring_home_and_non_presence_cards_skip_archive_links(self):
        for name, out in (('topics', export_json._compute_topics(self.db)),
                          ('top_news', export_json._compute_top_news(self.db)),
                          ('non_presence', export_json._compute_non_presence(self.db))):
            with self.subTest(name):
                links = self.links(out)
                self.assertTrue(links, name)
                self.assertFalse([u for u in links if '/topic/' in u or '/tags/' in u], name)


class RankRerunTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE media_sources(source_id INTEGER PRIMARY KEY,
                primary_country_code TEXT, tier INTEGER, media_name TEXT);
            INSERT INTO media_sources VALUES(1, 'US', 1, 'Test News');
            CREATE TABLE articles_raw(article_id INTEGER PRIMARY KEY, source_id INTEGER,
                title TEXT, summary TEXT, full_text TEXT, link TEXT, publisher_name TEXT,
                duplicate_of INTEGER, published_at TEXT, llm_prefilter TEXT,
                ai_score INTEGER, filter_score INTEGER, summary_ko TEXT,
                expanded_summary TEXT, expanded_summary_en TEXT);
        ''')
        self.db.executemany(
            "INSERT INTO articles_raw VALUES(?,1,?,?,?,?,NULL,NULL,?,'keep',?,?,?, ?, ?)",
            [
                (1, 'First', 'old snippet', 'new full text', 'https://example.com/1',
                 date.today().isoformat(), 60, 10, '기존 요약', '기존 긴 요약', 'old long'),
                (2, 'Second', 'other snippet', 'other full text', 'https://example.com/2',
                 date.today().isoformat(), 65, 9, '다른 요약', '다른 긴 요약', 'other long'),
            ],
        )
        self.db.commit()
        self.addCleanup(self.db.close)

    @staticmethod
    def response(score_factors=None):
        return {
            'score_factors': score_factors or {
                'directness': 3, 'magnitude': 4, 'urgency': 2, 'novelty': 1,
            },
            'title_ko': '새 제목', 'title_en': 'New title',
            'summary_en': 'A new evidence-based summary.',
            'topics': ['ECONOMY'], 'event_type': [], 'primary_country': 'US',
        }

    def test_exact_article_rerank_updates_only_success_and_invalidates_derivatives(self):
        result = llm_ranker.run_rank(self.db, provider=Provider(self.response()), article_ids=[1])
        self.assertEqual((result['total'], result['ranked']), (1, 1))
        first = self.db.execute(
            'SELECT ai_score, market_importance, kb_relevance, summary_ko, expanded_summary '
            'FROM articles_raw WHERE article_id=1'
        ).fetchone()
        self.assertEqual((first['ai_score'], first['market_importance'], first['kb_relevance']),
                         (70, 63, 75))
        self.assertIsNone(first['summary_ko'])
        self.assertIsNone(first['expanded_summary'])
        self.assertEqual(self.db.execute(
            'SELECT ai_score FROM articles_raw WHERE article_id=2'
        ).fetchone()[0], 65)

    def test_invalid_exact_rerank_preserves_existing_article(self):
        result = llm_ranker.run_rank(self.db, provider=Provider({}), article_ids=[1])
        row = self.db.execute(
            'SELECT ai_score, summary_ko, expanded_summary FROM articles_raw WHERE article_id=1'
        ).fetchone()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(tuple(row), (60, '기존 요약', '기존 긴 요약'))

    def test_article_not_mentioning_media_country_goes_global(self):
        # 2026-10-08 Straits Times '호르무즈 유조선 공격'이 SG 탭에 노출 — 매체 국가 언급이 없으면 GLOBAL
        hormuz = dict(self.response(), title_en='Hormuz tanker attacks hit weekly record',
                      summary_en='Attacks on tankers near Iran hit a record.', primary_country='')
        llm_ranker.run_rank(self.db, provider=Provider(hormuz), article_ids=[1])
        local = dict(self.response(), summary_en='The Federal Reserve tightened bank capital rules.')
        llm_ranker.run_rank(self.db, provider=Provider(local), article_ids=[2])
        pc = dict(self.db.execute('SELECT article_id, primary_country FROM articles_raw').fetchall())
        self.assertEqual(pc, {1: 'GLOBAL', 2: 'US'})

    def test_rank_and_expand_prompts_carry_publication_date(self):
        # 연도 없는 'Nov. 10'을 2025년으로 채운 희토류 기사(실제 2026) — 게시일을 기준으로 주어야 한다
        provider = Provider(self.response())
        llm_ranker.run_rank(self.db, provider=provider, article_ids=[1])
        self.assertIn(f'게시일: {date.today().isoformat()}', provider.requests[0][2])

    def test_empty_exact_scope_does_not_fall_back_to_unscored_queue(self):
        provider = Provider(self.response())
        result = llm_ranker.run_rank(self.db, provider=provider, article_ids=[])
        self.assertEqual(result['total'], 0)
        self.assertEqual(provider.requests, [])


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE media_sources(source_id INTEGER PRIMARY KEY,
                primary_country_code TEXT, tier INTEGER, media_name TEXT);
            INSERT INTO media_sources VALUES
                (1,'US',1,'US Test'),(2,'JP',1,'JP Test'),(3,'LA',1,'LA Test');
            CREATE TABLE articles_raw(article_id INTEGER PRIMARY KEY, source_id INTEGER,
                primary_country TEXT, title_ko TEXT, title TEXT, ai_score INTEGER,
                ai_model TEXT, published_at TEXT, duplicate_of INTEGER, dup_by_ai INTEGER,
                summary_ko TEXT, summary_en TEXT, link TEXT, event_type TEXT,
                korean_fi TEXT, personnel_move INTEGER, topics TEXT);
        ''')
        self.addCleanup(self.db.close)

    def add(self, aid, source=1, cc='US', score=60, day=None, duplicate=None, ai=0):
        self.db.execute('INSERT INTO articles_raw VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                        (aid, source, cc, None, f'headline {aid}', score, 'test:model',
                         day or date.today().isoformat(), duplicate, ai,
                         '요약 근거 ' * 60, 'Evidence summary', f'https://example.com/{aid}',
                         '', '', 0))
        self.db.commit()

    def links(self):
        return {r[0]: r[1] for r in self.db.execute('SELECT article_id,duplicate_of FROM articles_raw')}

    def test_effective_country_expression_prefers_article_subject(self):
        expr = db.effective_country_expr()
        rows = self.db.execute(
            f"SELECT a.article_id, {expr} AS cc FROM articles_raw a "
            "JOIN media_sources m ON m.source_id=a.source_id ORDER BY a.article_id"
        ).fetchall()
        self.assertEqual(rows, [])
        self.add(1, source=1, cc='JP')
        self.db.execute("UPDATE articles_raw SET primary_country='' WHERE article_id=1")
        self.add(2, source=1, cc='ID')
        rows = self.db.execute(
            f"SELECT a.article_id, {expr} AS cc FROM articles_raw a "
            "JOIN media_sources m ON m.source_id=a.source_id ORDER BY a.article_id"
        ).fetchall()
        self.assertEqual([(r['article_id'], r['cc']) for r in rows], [(1, 'US'), (2, 'ID')])

    def test_rank_score_factors_are_deterministic_and_differentiated(self):
        score, factors = llm_ranker._score_from_data({"score_factors": {
            "directness": 3, "magnitude": 2, "urgency": 3, "novelty": 2,
        }, "ai_score": 62})
        self.assertEqual(score, 67)
        self.assertEqual(factors["directness"], 3)

    def test_standalone_banking_sector_story_crosses_active_threshold(self):
        """2026-10-06: directness=3(KB 미언급, 현지 은행업 단독 기사)이 통상적인
        magnitude·urgency·novelty와 맞물리면 ACTIVE 임계(55)를 넘어야 한다."""
        score, _ = llm_ranker._score_from_data({"score_factors": {
            "directness": 3, "magnitude": 2, "urgency": 1, "novelty": 2,
        }})
        self.assertGreaterEqual(score, config.AI_SCORE_ACTIVE_THRESHOLD)

    def test_rank_score_falls_back_to_legacy_value(self):
        self.assertEqual(llm_ranker._score_from_data({"ai_score": 72}), (72, None))

    def test_rank_dimensions_separate_market_and_kb_relevance(self):
        self.assertEqual(llm_ranker._score_dimensions({
            'directness': 3, 'magnitude': 4, 'urgency': 2, 'novelty': 1,
        }), (63, 75))

    def test_invalid_rank_score_is_not_silently_saved_as_50(self):
        self.assertEqual(llm_ranker._score_from_data({}), (None, None))

    def test_off_list_country_code_resolves_to_global_not_media_fallback(self):
        """2026-10-06: 영국 매체(Reuters UK)가 쓴 이탈리아 은행 합병 기사에서 모델이
        목록에 없는 'IT'를 내자 매체 국적(GB)으로 새어 GB 탭에 뜬 문제. ISO2 형식의
        목록 밖 코드는 '특정국은 식별됐다'는 뜻이므로 GLOBAL로 처리해 매체 국적
        폴백을 막는다."""
        self.assertEqual(llm_ranker._valid_primary_country("IT"), "GLOBAL")
        self.assertEqual(llm_ranker._valid_primary_country("it"), "GLOBAL")

    def test_truly_empty_country_code_still_falls_back_to_media(self):
        """빈 값/형식이 다른 응답은 기존 설계대로 None(표시 시 매체 국적 폴백)."""
        self.assertIsNone(llm_ranker._valid_primary_country(""))
        self.assertIsNone(llm_ranker._valid_primary_country(None))
        self.assertIsNone(llm_ranker._valid_primary_country("unknown"))

    def test_known_country_code_passes_through(self):
        self.assertEqual(llm_ranker._valid_primary_country("jp"), "JP")

    def test_cross_source_single_amount_conflict_is_detected(self):
        conflict = numeric_guard.source_amount_conflicts([
            'The tariff deal covers $30 billion of goods.',
            'The tariff agreement covers USD 60 billion in goods.',
            'No amount was given in this report.',
        ])
        self.assertEqual(conflict, {'USD': [30e9, 60e9]})

    def test_multi_amount_explanation_is_not_auto_flagged(self):
        self.assertEqual(numeric_guard.source_amount_conflicts([
            'Each side covers $30 billion, for $60 billion combined.',
            'The combined agreement is worth $60 billion.',
        ]), {})

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
        p.complete_json_batch = lambda requests: {
            r[0]: {'groups': []} for r in requests if r[0].startswith('US__')}
        stats = llm_dedup.run_dedup(self.db, p)
        self.assertEqual(self.links()[2], None)
        self.assertEqual(self.links()[4], 3)
        self.assertEqual(stats['failed'], 2)

    def test_briefing_ranks_before_limit_and_discovers_subject(self):
        self.add(1, source=2, score=65)
        self.add(2, source=2, score=65)
        for aid in range(3, 8):
            self.add(aid, source=1, score=65, duplicate=2)  # 별도 발행사 보도 → 순위 보너스
        self.assertEqual(briefing._target_countries(self.db), ['US'])
        p = Provider({'summary_ko': '브리핑'})
        with patch.object(briefing.config, 'BRIEFING_MAX_ARTICLES', 1):
            briefing.run_briefing(self.db, p, briefing_type='daily')
        self.assertIn('headline 2', p.requests[0][2])
        self.assertNotIn('headline 1', p.requests[0][2])

    def test_large_country_is_split_into_bounded_requests(self):
        for aid in range(1, 502):
            self.add(aid, duplicate=1 if aid==2 else None, ai=1 if aid==2 else 0)
        self.db.execute("UPDATE articles_raw SET title=?", ('x' * 160,))
        self.db.commit()
        p = Provider({'groups': []})
        stats = llm_dedup.run_dedup(self.db, p)
        self.assertGreater(len(p.requests), 1)
        self.assertTrue(all(req[0].startswith('US__') for req in p.requests))
        self.assertTrue(all(req[3] <= 4096 for req in p.requests))
        self.assertEqual(stats['failed'], 0)
        self.assertIsNone(self.links()[2])

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
        p = Provider()
        def respond(requests):
            p.requests.extend(requests)
            out = {}
            for cid, _system, user, _tokens in requests:
                same = []
                for m in re.finditer(r'(p\d+)\nA (\d+).*?\nB (\d+)', user):
                    if {int(m.group(2)), int(m.group(3))} == {1, 81}:
                        same.append(m.group(1))
                out[cid] = {'same_pair_ids': same}
            return out
        p.complete_json_batch = respond
        llm_dedup.run_dedup(self.db, p)
        all_input = '\n'.join(r[2] for r in p.requests)
        self.assertIn('B 81 ', all_input)
        self.assertIn(date.today().isoformat(), all_input)
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

    def test_briefing_rejects_summary_with_mismatched_amount(self):
        self.add(1, cc='US', score=65)
        self.db.execute("UPDATE articles_raw SET summary_ko=?, summary_en=? WHERE article_id=1",
                        ('연준이 300억 달러 규모 기구를 발표했다.', 'The Fed announced a $30 billion facility.'))
        self.db.commit()
        p = Provider({'summary_ko': '연준이 $30억 규모 기구를 발표했다.',
                      'summary_en': 'The Fed announced a $30 billion facility.'})
        briefing.run_briefing(self.db, p, briefing_type='daily', countries=['US'])
        self.assertIsNone(self.db.execute('SELECT 1 FROM country_briefings').fetchone())

    def test_briefing_amount_mismatch_is_retried_once_with_exact_amount_instruction(self):
        # 2026-10-07: 금액 검증 거부 시 재시도가 없어 IN 브리핑이 6일째 갱신되지 않았다
        self.add(1, cc='US', score=65)
        self.db.execute("UPDATE articles_raw SET summary_ko=?, summary_en=? WHERE article_id=1",
                        ('연준이 300억 달러 규모 기구를 발표했다.', 'The Fed announced a $30 billion facility.'))
        self.db.commit()

        class SeqProvider(Provider):
            def __init__(self, responses):
                super().__init__()
                self.responses = list(responses)

            def complete_json_batch(self, requests):
                self.requests.extend(requests)
                resp = self.responses.pop(0)
                return {r[0]: resp for r in requests}

        p = SeqProvider([
            {'summary_ko': '연준이 30억 달러 규모 기구를 발표했다.', 'summary_en': 'The Fed announced a $3 billion facility.'},
            {'summary_ko': '연준이 300억 달러 규모 기구를 발표했다.', 'summary_en': 'The Fed announced a $30 billion facility.'},
        ])
        briefing.run_briefing(self.db, p, briefing_type='daily', countries=['US'])
        self.assertEqual(len(p.requests), 2)
        self.assertIn('금액', p.requests[1][2])               # 재요청엔 금액 그대로 쓰라는 지시가 붙는다
        row = self.db.execute('SELECT summary FROM country_briefings').fetchone()
        self.assertEqual(row[0], '연준이 300억 달러 규모 기구를 발표했다.')

    def test_stale_daily_briefing_is_not_exported(self):
        # 2026-10-07: IN 탭에 10/1자 '준비 중' 브리핑이 6일째 노출
        briefing.ensure_table(self.db)
        for cc, d in (('IN', '2026-10-01'), ('VN', '2026-10-06'), ('US', '2026-10-07')):
            self.db.execute("INSERT INTO country_briefings(cc, briefing_date, briefing_type, summary, summary_en)"
                            " VALUES(?,?,'daily',?,?)", (cc, d, f'{cc} 브리핑', f'{cc} brief'))
        self.db.commit()
        out = export_json._daily_briefs(self.db, '2026-10-07')
        self.assertEqual(sorted(out), ['US', 'VN'])          # 기준일·전일만 노출

    def test_empty_briefing_is_explicit_without_llm(self):
        self.add(1, source=3, cc='LA', score=8)
        p = Provider()
        briefing.run_briefing(self.db, p, briefing_type='daily', countries=['LA'])
        self.assertEqual(p.requests, [])
        row = self.db.execute('SELECT article_count,summary FROM country_briefings').fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 0)
        self.assertIn('충족', row[1])

    def test_highlight_sources_are_limited_to_input_articles(self):
        rows = [{'article_id': 10}, {'article_id': 20}]
        items = [
            {'headline_ko': '검증된 항목', 'source_article_ids': [10, '20', 10, 999]},
            {'headline_ko': '출처 없는 항목', 'source_article_ids': [999]},
            {'headline_ko': 'ID 누락 항목'},
        ]
        out = briefing._validate_highlight_sources(items, rows, 10)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['source_article_ids'], [10, 20])
        self.assertIn('source_article_ids', briefing._system_highlights(10))

    def test_highlight_with_unsupported_usd_amount_is_rejected(self):
        rows = [{'article_id': 10, 'title': 'Vietnam establishes 190-million-USD company',
                 'title_ko': '베트남 1.9억 달러 회사 설립', 'summary_ko': '',
                 'summary_en': 'Charter capital is 190 million USD.'}]
        items = [
            {'headline_ko': '베트남 190억 달러 회사 설립',
             'headline_en': 'Vietnam establishes $1.9 billion company', 'source_article_ids': [10]},
            {'headline_ko': '베트남 1.9억 달러 회사 설립',
             'headline_en': 'Vietnam establishes $190 million company', 'source_article_ids': [10]},
        ]
        out = briefing._validate_highlight_sources(items, rows, 10)
        self.assertEqual(len(out), 1)
        self.assertIn('$190 million', out[0]['headline_en'])

    def test_highlight_rejects_dollar_sign_with_korean_unit_mismatch(self):
        rows = [{'article_id': 10, 'title': 'US, China reach $30 billion tariff deal',
                 'title_ko': '', 'summary_ko': '',
                 'summary_en': 'The deal covers $30 billion in goods.'}]
        items = [{'headline_ko': '미국-중국 $30억 관세 협상 타결',
                  'headline_en': 'U.S.-China reach $30 billion tariff deal',
                  'source_article_ids': [10]}]
        out = briefing._validate_highlight_sources(items, rows, 10)
        self.assertEqual(out, [])

    def test_quiz_accepts_valid_payload_with_source_in_allowed_set(self):
        data = {
            'question_ko': '기준금리를 몇 bp 인상했나?', 'question_en': 'By how many bp was the rate hiked?',
            'choices_ko': ['25bp', '50bp'], 'choices_en': ['25bp', '50bp'],
            'correct_index': 0, 'explanation_ko': '기사 본문 근거', 'explanation_en': 'per the article',
            'source_article_id': 10,
        }
        out = briefing._validate_quiz(data, {10, 20})
        self.assertIsNotNone(out)
        self.assertEqual(out['source_article_id'], 10)
        self.assertEqual(out['correct_index'], 0)

    def test_quiz_rejects_mismatched_choice_counts(self):
        data = {'question_ko': 'q', 'question_en': 'q', 'choices_ko': ['a', 'b'],
                'choices_en': ['a'], 'correct_index': 0, 'source_article_id': 10}
        self.assertIsNone(briefing._validate_quiz(data, {10}))

    def test_quiz_rejects_correct_index_out_of_range(self):
        data = {'question_ko': 'q', 'question_en': 'q', 'choices_ko': ['a', 'b'],
                'choices_en': ['a', 'b'], 'correct_index': 2, 'source_article_id': 10}
        self.assertIsNone(briefing._validate_quiz(data, {10}))

    def test_quiz_rejects_source_not_in_allowed_set(self):
        data = {'question_ko': 'q', 'question_en': 'q', 'choices_ko': ['a', 'b'],
                'choices_en': ['a', 'b'], 'correct_index': 0, 'source_article_id': 999}
        self.assertIsNone(briefing._validate_quiz(data, {10}))

    def test_export_resolves_highlight_source_id_to_exact_article(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript('''
            CREATE TABLE media_sources(source_id INTEGER PRIMARY KEY, media_name TEXT,
                primary_country_code TEXT, language TEXT, tier INTEGER);
            INSERT INTO media_sources VALUES(1,'Test News','IN','en',1);
            CREATE TABLE articles_raw(
                article_id INTEGER PRIMARY KEY, source_id INTEGER, ai_score INTEGER,
                title TEXT, title_ko TEXT, title_en TEXT, summary_ko TEXT, summary_en TEXT,
                expanded_summary TEXT, expanded_summary_en TEXT, link TEXT, published_at TEXT,
                topics TEXT, primary_country TEXT, duplicate_of INTEGER);
            INSERT INTO articles_raw VALUES(
                10,1,72,'RBI raises rate','인도 중앙은행 금리 인상','RBI raises rate',
                '금리 인상 요약','Rate increase summary','','','https://example.com/10',
                '2026-09-22','ECONOMY','IN',NULL);
            CREATE TABLE daily_highlights(id INTEGER PRIMARY KEY, date TEXT, items TEXT, model TEXT,
                generated_at TEXT);
        ''')
        items = json.dumps([{'headline_ko': '인도 중앙은행 금리 인상',
                             'source_article_ids': [10]}], ensure_ascii=False)
        conn.execute('INSERT INTO daily_highlights VALUES(1,?,?,?,?)',
                     ('2026-09-22', items, 'test:model', '2026-09-22'))
        out = export_json._daily_highlights(conn)
        self.assertEqual(out[0]['source_articles'][0]['article_id'], 10)
        self.assertEqual(out[0]['source_articles'][0]['u'], 'https://example.com/10')

    def test_translation_rejects_amount_that_does_not_match_source(self):
        self.add(1, score=60)
        self.db.execute("UPDATE articles_raw SET summary_en=?, summary_ko=NULL, title_ko=NULL "
                         "WHERE article_id=1", ('Vietnam raises $133 billion in new bonds.',))
        self.db.commit()
        llm_translate.run_translate(
            self.db, Provider({'title_ko': '베트남 국채 발행', 'summary': '베트남이 133억 달러 국채를 발행했다.'}))
        row = self.db.execute('SELECT summary_ko FROM articles_raw WHERE article_id=1').fetchone()
        self.assertIsNone(row['summary_ko'])

    def test_translation_stores_amount_that_matches_source(self):
        self.add(1, score=60)
        self.db.execute("UPDATE articles_raw SET summary_en=?, summary_ko=NULL, title_ko=NULL "
                         "WHERE article_id=1", ('Vietnam raises $133 billion in new bonds.',))
        self.db.commit()
        llm_translate.run_translate(
            self.db, Provider({'title_ko': '베트남 국채 발행', 'summary': '베트남이 1,330억 달러 국채를 발행했다.'}))
        row = self.db.execute('SELECT summary_ko FROM articles_raw WHERE article_id=1').fetchone()
        self.assertEqual(row['summary_ko'], '베트남이 1,330억 달러 국채를 발행했다.')

    def test_translation_covers_displayed_items_below_active_threshold(self):
        # 2026-09-28 라이브: 사회 예외(52점)·인사동향 카드가 번역 대상에서 빠져 한국어 화면에 영어 요약이 떴다
        for aid in (1, 2, 3, 4):
            self.add(aid, score=52)
        self.db.execute("UPDATE articles_raw SET summary_ko=NULL, title_ko=NULL")
        self.db.execute("UPDATE articles_raw SET topics='SOCIETY' WHERE article_id=1")
        self.db.execute("UPDATE articles_raw SET personnel_move=1 WHERE article_id=2")
        self.db.execute("UPDATE articles_raw SET korean_fi='SHINHAN' WHERE article_id=3")
        self.db.execute("UPDATE articles_raw SET topics='ECONOMY' WHERE article_id=4")   # 노출 안 됨
        self.db.commit()
        llm_translate.run_translate(self.db, Provider({'title_ko': '제목', 'summary': '요약'}))
        done = {r[0] for r in self.db.execute('SELECT article_id FROM articles_raw WHERE summary_ko IS NOT NULL')}
        self.assertEqual(done, {1, 2, 3})

    def test_translation_can_be_limited_to_explicit_article_ids(self):
        self.add(1, score=60)
        self.add(2, score=60)
        self.db.execute("UPDATE articles_raw SET summary_ko=NULL, title_ko=NULL")
        self.db.commit()
        result = llm_translate.run_translate(
            self.db, Provider({'title_ko': '제목', 'summary': '요약'}), article_ids=[2])
        self.assertEqual(result['total'], 1)
        done = {r[0] for r in self.db.execute(
            'SELECT article_id FROM articles_raw WHERE summary_ko IS NOT NULL')}
        self.assertEqual(done, {2})

    def test_superseded_story_is_detected(self):
        items = [
            (1, '2026-09-27', "India's 3-day bank strike may delay September salaries"),
            (2, '2026-09-28', 'Indian bank unions defer nationwide strike after agreement with IBA'),
            (3, '2026-09-27', 'Bank unions to begin 3-day strike from September 28'),
            (4, '2026-09-27', 'RBI keeps repo rate unchanged'),
            (5, '2026-09-21', 'Mutual Trust Bank signs agreement with Central Depository'),  # 'agreement'만 겹침
            (6, '2026-09-20', 'Bank unions threaten nationwide strike'),                     # 3일 창 밖
        ]
        self.assertEqual(llm_dedup.superseded_ids(items), {1, 3})
        # 후속 기사가 더 오래됐거나 연기·취소 신호가 없으면 내리지 않는다
        self.assertEqual(llm_dedup.superseded_ids([
            (1, '2026-09-28', "India's 3-day bank strike may delay September salaries"),
            (2, '2026-09-27', 'Indian bank unions defer nationwide strike after agreement with IBA')]), set())
        self.assertEqual(llm_dedup.superseded_ids([
            (1, '2026-09-27', 'Japan to raise consumption tax'),
            (2, '2026-09-28', 'Fed postpones rate decision')]), set())

    def test_expired_preview_is_removed_after_one_day(self):
        items = [
            (1, '2026-09-29', 'Fed expected to release PCE tomorrow'),
            (2, '2026-09-30', 'BOJ decision ahead of meeting'),
            (3, '2026-09-29', 'Fed publishes PCE inflation data'),
            (4, '', 'ECB decision expected tomorrow'),
        ]
        self.assertEqual(llm_dedup.expired_preview_ids(items, '2026-10-01'), {1})
        self.assertEqual(llm_dedup.expired_preview_ids(items, 'bad-date'), set())

    def test_archive_link_exclusion_filters_topic_and_tag_pages(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE articles_raw(article_id INTEGER, link TEXT)')
        db.executemany('INSERT INTO articles_raw VALUES(?,?)', [
            (1, 'https://economictimes.indiatimes.com/topic/pvr-inox-compensation-order'),
            (2, 'https://vietnamnews.vn/tags/industrial-bank'),
            (3, 'https://economictimes.indiatimes.com/markets/ipos/inox-clean-energy-ipo/articleshow/1.cms')])
        kept = [r[0] for r in db.execute(f'SELECT article_id FROM articles_raw a WHERE 1=1{export_json._ARCHIVE_LINK_EXCL}')]
        self.assertEqual(kept, [3])

    def test_related_links_capped_at_four_including_self(self):
        a = {'title_ko': '본 기사', 'title': 'raw', 'link': 'https://x/0', 'media_name': 'M0'}
        sibs = [{'t': f's{i}', 'u': f'https://x/{i}', 'src': f'M{i}'} for i in range(1, 7)]
        rl = export_json._related_links(a, None, sibs, [])
        self.assertEqual([r['u'] for r in rl], ['https://x/0', 'https://x/1', 'https://x/2', 'https://x/3'])

    def test_related_link_rejects_same_topic_but_different_event(self):
        rep = {'title': 'Minutes: BOJ discussed faster rate hikes at July meeting',
               'title_en': 'BOJ July minutes reveal board discussion of faster rate-hike pace',
               'summary_en': 'Bank of Japan minutes from July show board members discussed accelerating rate hikes amid persistent inflation, with one member warning that delaying action could force rapid, substantial increases later. The BOJ subsequently raised its benchmark rate by 0.25 percentage points to around 1.25 percent in September. Governor Ueda signaled the bank plans to continue hiking rates to stabilize inflation at approximately 2 percent.'}
        other = {'title': 'BOJ rate hike in October is a real possibility, ex-official says',
                 'title_en': 'BOJ rate hike in October flagged as real possibility by ex-official',
                 'summary_en': "A former Bank of Japan official has indicated that a rate hike in October is a genuine possibility, signaling potential monetary policy tightening ahead. The comment reflects ongoing debate within the BOJ about the timing and pace of further rate increases following recent policy adjustments. Such a move would have direct implications for yen strength, funding costs in Japan, and regional financial conditions affecting KB's Tokyo operations."}
        same = {'title': 'Bank of Japan debated need for faster rate hikes, July minutes show',
                'title_en': 'Bank of Japan debated faster rate hikes in July, minutes reveal',
                'summary_en': 'The July meeting minutes show board members discussed accelerating interest rate increases as inflation pressures persist.'}
        self.assertFalse(export_json._same_story_for_link(rep, other))
        self.assertTrue(export_json._same_story_for_link(rep, same))

    def test_country_story_member_is_validated_before_becoming_related_link(self):
        rep = {'title': 'Nifty Bank crashes 1,800 points in 2 days and slips below 54K',
               'title_en': "India's Nifty Bank index crashes below 54K on RBI rate-hike concerns",
               'summary_en': "India's Nifty Bank index fell 1% to 53,786 on Tuesday, marking its lowest level in four months, as investors braced for potential RBI rate hikes ahead of the central bank's October monetary policy meeting. Rising oil and food prices, combined with Fed rate hikes, have pushed the RBI toward tightening.",
               'title_ko': '인도 은행지수 하락', 'link': 'https://x/rep', 'media_name': 'A'}
        unrelated = {'title': 'Rupee recoups intraday losses as RBI intervenes via dollar sales',
                     'title_en': 'RBI dollar sales help rupee recover intraday losses',
                     'summary_en': "The Indian rupee reversed intraday weakness after the Reserve Bank of India intervened by selling dollars in the foreign exchange market. The RBI's action stabilized the currency following earlier depreciation pressure. Such interventions are routine RBI practice to manage rupee volatility and support exchange-rate stability.",
                     'title_ko': '루피 낙폭 회복', 'link': 'https://x/other', 'media_name': 'B'}
        self.assertEqual(
            [x['u'] for x in export_json._related_links(rep, None, [], [unrelated])],
            ['https://x/rep'])

    def test_no_news_signal_is_unknown(self):
        self.assertEqual(export_json._signal_band(None), 'unknown')

    def test_mood_uses_current_risk_events_not_removed_topic(self):
        flat = [{'kind': 'index', 'change_pct': 0.0}]
        calm = [{'ai_score': 90, 'topics': 'ECONOMY', 'event_type': ''}]
        risky = [{'ai_score': 60, 'topics': 'ECONOMY', 'event_type': 'SANCTION'},
                 {'ai_score': 60, 'topics': 'POLICY', 'event_type': 'INCIDENT'}]
        self.assertGreater(export_json._mood_level(calm, flat)[0], 70)
        self.assertLess(export_json._mood_level(risky, flat)[0], 55)

    def test_mood_reacts_to_market_stress_without_risk_article(self):
        calm = [{'ai_score': 60, 'topics': 'MARKETS', 'event_type': ''}]
        stressed = [{'kind': 'fx', 'change_pct': 2.5},
                    {'kind': 'index', 'change_pct': -2.5}]
        self.assertLess(export_json._mood_level(calm, stressed)[0], 70)

    def test_pulse_categories_follow_current_taxonomy(self):
        self.assertEqual([x[0] for x in export_json._pulse_cats()], taxonomy.codes())
        self.assertEqual(taxonomy.codes(),
                         ['ECONOMY', 'MARKETS', 'TECH', 'GEO', 'POLICY', 'SOCIETY'])

    def test_snapshot_date_uses_collection_run_not_export_wall_clock(self):
        """AI 분석이 자정을 넘겨 끝나도 아카이브 날짜는 수집일에 고정돼야 한다.

        2026-10-01 발견: export 실행 시각(다음날 새벽)을 스냅샷 날짜로 쓰면, 전날 수집분이
        다음날 폴더에 저장되고 전날 폴더엔 그 전전날의 낡은 스냅샷만 남는 어긋남이 생겼다."""
        self.db.executescript(
            "CREATE TABLE fetch_runs(run_id INTEGER PRIMARY KEY, started_at TEXT);"
            "INSERT INTO fetch_runs VALUES(1, '2026-09-30 05:28:27');"
        )
        self.assertEqual(export_json._snapshot_date(self.db), '2026-09-30')

    def test_snapshot_date_falls_back_to_today_without_fetch_history(self):
        self.db.executescript("CREATE TABLE fetch_runs(run_id INTEGER PRIMARY KEY, started_at TEXT);")
        self.assertEqual(export_json._snapshot_date(self.db),
                          export_json._snapshot_date(None))


class DailyHighlightsReturnTests(unittest.TestCase):
    """2026-10-08: generate_daily_highlights()가 성공 경로(항목 저장 후)에서 return 없이
    끝나 None을 반환, main.py의 s4['written']이 TypeError로 전체 ai 파이프라인을 죽였다
    (실제 운영 중 발생 — 오늘의 글로벌 핵심은 DB에 정상 저장됐으나 이후 퀴즈·지표·배포
    단계가 전부 스킵됨). 성공 시에도 dict를 반환하는지 회귀 테스트로 고정."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE media_sources(source_id INTEGER PRIMARY KEY,
                primary_country_code TEXT, tier INTEGER, media_name TEXT);
            INSERT INTO media_sources VALUES (1,'US',1,'US Test');
            CREATE TABLE articles_raw(article_id INTEGER PRIMARY KEY, source_id INTEGER,
                primary_country TEXT, title_ko TEXT, title TEXT, ai_score INTEGER,
                ai_model TEXT, published_at TEXT, duplicate_of INTEGER, dup_by_ai INTEGER,
                summary_ko TEXT, summary_en TEXT, link TEXT, event_type TEXT,
                korean_fi TEXT, personnel_move INTEGER, topics TEXT);
        ''')
        self.db.execute(
            "INSERT INTO articles_raw VALUES(1,1,'US',NULL,'Fed raises rates',70,'test:model',?,"
            "NULL,0,'요약','Evidence summary','https://example.com/1','', '', 0, NULL)",
            (date.today().isoformat(),))
        self.db.commit()
        self.addCleanup(self.db.close)

    def test_successful_run_returns_written_count_not_none(self):
        provider = Provider({'highlights': [{
            'category': '금리', 'headline_ko': '연준 금리 인상', 'headline_en': 'Fed raises rates',
            'country_codes': ['US'], 'source_article_ids': [1],
        }]})
        result = briefing.generate_daily_highlights(self.db, provider=provider)
        self.assertEqual(result, {'written': 1})
        self.assertIsNotNone(
            self.db.execute('SELECT items FROM daily_highlights').fetchone())


class NumericGuardTests(unittest.TestCase):
    def test_usd_prefix_and_korean_man_units_are_parsed(self):
        self.assertEqual(numeric_guard.usd_values('USD 190 million'), [190e6])
        self.assertEqual(numeric_guard.usd_values('1,900만 달러'), [19e6])
        self.assertEqual(numeric_guard.usd_values('1억9천만 달러'), [190e6])
        self.assertEqual(numeric_guard.usd_values('1조 2,000억 달러'), [1.2e12])
        self.assertEqual(numeric_guard.usd_values('$30억의 투자'), [3e9])

    def test_mistranslated_amounts_are_flagged(self):
        self.assertTrue(numeric_guard.usd_mismatch('The firm raised USD 190 million', '베트남 190억 달러'))
        self.assertTrue(numeric_guard.usd_mismatch('a $19 million deal', '19만 달러 거래'))
        self.assertFalse(numeric_guard.usd_mismatch('a $19 million deal', '1,900만 달러 거래'))
        self.assertFalse(numeric_guard.usd_mismatch('$1.2 trillion', '1조 2,000억 달러'))

    def test_decimal_with_korean_unit_is_not_truncated(self):
        # 2026-09-28 라이브 IN 브리핑: "$186.5억"이 186달러로 읽혀 정상 번역이 불일치 판정됨
        self.assertEqual(numeric_guard.usd_values('$186.5억을 기록'), [186.5e8])
        self.assertFalse(numeric_guard.usd_mismatch('$18.65 billion', '$186.5억'))
        self.assertTrue(numeric_guard.usd_mismatch('$18.65 billion', '$18.65억'))

    def test_expanded_korean_summary_with_wrong_amount_is_not_stored(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE media_sources(source_id INTEGER PRIMARY KEY, primary_country_code TEXT,
                media_name TEXT, tier INTEGER);
            INSERT INTO media_sources VALUES(1,'IN','Business Standard',1);
            CREATE TABLE articles_raw(article_id INTEGER PRIMARY KEY, source_id INTEGER, title TEXT,
                summary TEXT, full_text TEXT, link TEXT, ai_score INTEGER, duplicate_of INTEGER,
                published_at TEXT);
            INSERT INTO articles_raw VALUES(1,1,'RBI net dollar purchases hit record $18.65bn',
                'RBI bought a net $18.65 billion in July.','', 'https://x/1', 70, NULL, '2026-09-27');
        """)
        llm_expand.run_expand(db, Provider({
            'expanded_summary_en': 'The RBI purchased a net $18.65 billion in July.',
            'expanded_summary_ko': '인도중앙은행이 7월 달러 순매입액 $18.65억을 기록했다.'}))
        row = db.execute('SELECT expanded_summary, expanded_summary_en FROM articles_raw').fetchone()
        self.assertIsNone(row['expanded_summary'])
        self.assertEqual(row['expanded_summary_en'], 'The RBI purchased a net $18.65 billion in July.')

    def test_inr_crore_and_korean_rupee_units_are_parsed(self):
        self.assertEqual(numeric_guard.inr_values('Rs 10,000 crore IPO'), [1e11])
        self.assertEqual(numeric_guard.inr_values('a Rs 22,568-crore IPO at Rs 1,785 per share'),
                         [2.2568e11, 1785])
        self.assertEqual(numeric_guard.inr_values('₹2,000 fee and 38.5 lakh applications'), [2000, 3.85e6])
        self.assertEqual(numeric_guard.inr_values('2조 2,568억 루피, 주당 1,785루피'), [2.2568e12, 1785])
        self.assertEqual(numeric_guard.inr_values('Rp 5,000 / 5,000루피아'), [])   # 인도네시아 루피아 제외

    def test_crore_mistranslations_are_flagged(self):
        # 2026-09-28 라이브: crore(1천만)를 억(1억)으로 옮긴 10배 오류들
        self.assertTrue(numeric_guard.amount_mismatch('Inox to file Rs 10,000 crore IPO', '인옥스 1조 루피 IPO'))
        self.assertTrue(numeric_guard.amount_mismatch('raised Rs 6,746 crore', '6,746억 루피를 조달'))
        self.assertFalse(numeric_guard.amount_mismatch('raised Rs 6,746 crore', '674.6억 루피를 조달'))
        self.assertTrue(numeric_guard.amount_mismatch('$96 billion', '96억 달러'))   # USD도 함께 본다

    def test_ranker_drops_korean_title_with_wrong_amount(self):
        self.assertEqual(llm_ranker._checked_title_ko(
            '인옥스 1조 루피 IPO 추진', 'Inox to file Rs 10,000 crore IPO', '', ''), '')
        self.assertEqual(llm_ranker._checked_title_ko(
            '인옥스 1,000억 루피 IPO 추진', 'Inox to file Rs 10,000 crore IPO', '', ''), '인옥스 1,000억 루피 IPO 추진')

    def test_ranker_drops_english_title_with_wrong_currency(self):
        # 2026-09-29 라이브: Rp 9.1조(약 $5.7억)를 영문 제목에 "$9.1 trillion"으로 표기
        self.assertEqual(llm_ranker._checked_title_en(
            'Indonesia records $9.1 trillion scam losses',
            'Indonesia records Rp9.1 trillion in online scam losses',
            'OJK reported losses of Rp 9.1 trillion (approximately $570 million USD).'), '')
        self.assertEqual(llm_ranker._checked_title_en(
            'Indonesia records Rp9.1 trillion scam losses', 'raw', 'about $570 million'),
            'Indonesia records Rp9.1 trillion scam losses')

    def test_highlight_country_codes_follow_source_articles(self):
        # 2026-09-29 라이브: 근거 기사는 CN뿐인데 탑이슈 국가가 JP로 태그됨
        rows = [{'article_id': 10, 'title': 'Trump offered arms sales to China', 'title_ko': '',
                 'summary_ko': '', 'summary_en': '', 'cc': 'US', 'subject_cc': 'CN'},
                {'article_id': 20, 'title': 'Fed cuts', 'title_ko': '', 'summary_ko': '',
                 'summary_en': '', 'cc': 'GLOBAL', 'subject_cc': 'GLOBAL'}]
        out = briefing._validate_highlight_sources([
            {'headline_ko': '미국, 중국에 군사장비 판매 제안', 'country_codes': ['JP'], 'source_article_ids': [10]},
            {'headline_ko': '미중 관세', 'country_codes': ['US', 'CN'], 'source_article_ids': [10]},
            {'headline_ko': '연준', 'country_codes': ['US'], 'source_article_ids': [20]},
        ], rows, 10)
        self.assertEqual([h['country_codes'] for h in out], [['CN'], ['US', 'CN'], ['US']])

    def test_translation_rejects_wrong_rupee_amount(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE media_sources(source_id INTEGER PRIMARY KEY, primary_country_code TEXT);
            INSERT INTO media_sources VALUES(1,'IN');
            CREATE TABLE articles_raw(article_id INTEGER PRIMARY KEY, source_id INTEGER, title TEXT,
                title_ko TEXT, summary_en TEXT, summary_ko TEXT, ai_score INTEGER, duplicate_of INTEGER,
                published_at TEXT, primary_country TEXT, topics TEXT, personnel_move INTEGER, korean_fi TEXT);
        """)
        db.execute("INSERT INTO articles_raw VALUES(1,1,'NSE IPO',NULL,'NSE raised Rs 6,746 crore.',NULL,70,NULL,?,'IN',"
                   "NULL,0,'')",
                   (date.today().isoformat(),))
        db.commit()
        llm_translate.run_translate(db, Provider({'title_ko': 'NSE IPO', 'summary': 'NSE가 6,746억 루피를 조달했다.'}))
        self.assertIsNone(db.execute('SELECT summary_ko FROM articles_raw').fetchone()['summary_ko'])

    def test_dollar_sign_without_digits_does_not_raise(self):
        self.assertEqual(numeric_guard.usd_values('priced in US$, analysts said. Up to $5...'), [5.0])


class OtherCurrencyGuardTests(unittest.TestCase):
    """2026-10-07 HK 브리핑: 원문 HK$500 billion → '500억 홍콩달러'(10배 축소)가 그대로 노출.
    USD·INR만 검사하던 금액 검증을 거점 통화로 확대."""

    def test_wrong_scale_in_other_currencies_is_flagged(self):
        cases = [
            ('HK$500 billion IPO', '5,000억 홍콩달러', '500억 홍콩달러'),
            ('Rp 9.1 trillion losses', '9.1조 루피아', '91조 루피아'),
            ('a ¥1.2 trillion package', '1.2조 엔', '12조 엔'),
            ('10 billion yuan fund', '100억 위안', '10억 위안'),
            ('RMB 10 billion fund', '100억 위안', '1,000억 위안'),
            ('VND 50 trillion bonds', '50조 동', '5조 동'),
            ('a £250,000 loan', '25만 파운드', '250만 파운드'),
            ('S$2 billion deal', '20억 싱가포르달러', '2억 싱가포르달러'),
            ('THB 30 billion stimulus', '300억 바트', '30억 바트'),
            ('€5 billion deal', '50억 유로', '5억 유로'),
            # 2026-10-07 라이브: 방글라데시 crore를 억으로 옮긴 10배 오류들, 말레이시아·싱가포르·인니
            ('a Tk20,000 crore revival fund', '2,000억 타카', '2조 타카'),
            ('Tk 7,000 crore in state investment', '700억 타카', '7000억 타카'),
            ('debt reached roughly Tk7 lakh crore', '7조 타카', '70조 타카'),
            ('Tk 32.45b deposit outflow', '324억 타카', '3,245억 타카'),
            ('RM9.273 billion owed', '92.7억 링깃', '9.27억 링깃'),
            ('The S$405 million facility', '4억 500만 싱가포르달러', '4억 5천만 싱가포르달러'),
        ]
        for src, ok, bad in cases:
            with self.subTest(src):
                self.assertFalse(numeric_guard.amount_mismatch(src, ok))
                self.assertTrue(numeric_guard.amount_mismatch(src, bad))

    def test_hong_kong_dollar_sign_is_not_read_as_us_dollar(self):
        self.assertEqual(numeric_guard.usd_values('HK$500bn and S$2bn'), [])
        self.assertEqual(numeric_guard.usd_values('US$5bn'), [5e9])
        self.assertEqual(numeric_guard.fx_values('US$500 million'), {})   # US$의 S$를 싱가포르달러로 읽지 않음
        self.assertEqual(numeric_guard.inr_values('Rs 7 lakh crore'), [7e12])
        # 원문에 USD가 있고 출력에 HK$가 있어도 USD 불일치로 오판하지 않는다
        self.assertFalse(numeric_guard.amount_mismatch('$3 billion; HK$500 billion', 'HK$500 billion'))

    def test_korean_amount_hints_give_exact_korean_values(self):
        # 2026-10-07: 재생성해도 LLM이 crore·billion을 억·조로 옮기며 매번 10배 틀려 한국어가 비었다
        hint = numeric_guard.korean_amount_hints(
            'a Tk20,000 crore fund; RM9.273 billion owed; S$405 million plant; Rs 1,785 per share')
        for want in ('2,000억 타카', '92.73억 링깃', '4.05억 싱가포르달러'):
            self.assertIn(want, hint)
        self.assertNotIn('루피', hint)                      # 1만 미만 소액(주가)은 힌트 불필요
        self.assertEqual(numeric_guard.korean_amount_hints('no amounts here'), '')
        self.assertIn('1조 6,601억 타카', numeric_guard.korean_amount_hints('Tk166,010 crore'))
        # 힌트 값은 검증을 통과해야 한다
        src = 'a Tk20,000 crore fund'
        self.assertFalse(numeric_guard.amount_mismatch(src, '2,000억 타카 규모 기금'))

    def test_translation_prompt_carries_korean_amount_hints(self):
        import llm_translate
        user = llm_translate._user('Businesses seek loans from a Tk20,000 crore fund.', 'Tk20,000cr fund')
        self.assertIn('2,000억 타카', user)

    def test_korean_dong_word_is_not_an_amount(self):
        self.assertFalse(numeric_guard.amount_mismatch('VND 50 trillion', '금리를 3.5%로 동결했다'))
        self.assertEqual(numeric_guard.fx_values('정부가 동결 방침을 밝혔다'), {})


class StoryDedupTests(unittest.TestCase):
    """2026-10-08 배포본: 같은 사건이 국가탭·홈 핵심뉴스·모니터링에 여러 건 노출."""

    @staticmethod
    def row(aid, title_en, summary_en, title_ko='한국어 제목', event_type='', cc='IN'):
        return {'article_id': aid, 'title': title_en, 'title_en': title_en, 'title_ko': title_ko,
                'summary_en': summary_en, 'summary_ko': '한국어 요약', 'event_type': event_type, 'cc': cc}

    RBI_A = ('RBI raises repo rate 25bp to 5.5% for first time since Feb 2023',
             "India's Reserve Bank raised its repo rate by 25 basis points to 5.5% on Wednesday, the first "
             'increase in 44 months, citing stronger-than-expected growth and price pressures.')
    RBI_B = ('RBI raises repo rate 25 bps to 5.5%, shifts to calibrated tightening',
             "India's Reserve Bank Monetary Policy Committee voted unanimously to raise the repo rate by 25 "
             'basis points to 5.5% on October 7, citing rising inflation and broad-based price pressures.')
    NRI = ('NRI deposit inflows surge 6x to $36.2bn in Apr-Jul FY27',
           'Non-resident Indian (NRI) deposit flows surged over sixfold to $36.2 billion during April-July '
           'FY27, according to RBI data. The sharp increase reflects strong capital inflows from overseas '
           'Indians, likely driven by higher domestic interest rates and rupee stability. This surge has '
           'significant implications for Indian banking sector liquidity and foreign exchange reserves.')
    RBI_FX = ("RBI's net dollar purchases hit record $18.65bn in July",
              "India's central bank purchased a record net $18.65 billion in dollars during July, according "
              "to the RBI's monthly bulletin. The surge in dollar accumulation reflects efforts to manage "
              'currency volatility and bolster foreign exchange reserves amid broader capital flow dynamics '
              'in the Indian market.')

    def test_country_feed_merges_same_story_even_with_korean_titles(self):
        rows = [self.row(1, *self.RBI_A), self.row(2, *self.RBI_B), self.row(3, *self.NRI)]
        reps, sizes, _ = export_json._dedup_country_feed(rows, {})
        self.assertEqual([r['article_id'] for r in reps], [1, 3])
        self.assertEqual(sizes[1], 2)

    def test_different_stories_from_same_institution_are_not_merged(self):
        reps, _, _ = export_json._dedup_country_feed([self.row(1, *self.NRI), self.row(2, *self.RBI_FX)], {})
        self.assertEqual(len(reps), 2)

    def test_monitoring_bucket_skips_same_incident(self):
        hack_a = ('South Korea probes AI use in bank cyberattacks; KB among targets',
                  'South Korean authorities are investigating whether AI was used in cyberattacks on '
                  'major banks including KB Kookmin.')
        hack_b = ('South Korea reports AI-assisted bank hacks affecting major lenders',
                  'South Korean authorities reported AI-assisted hacking attempts on major banks, '
                  'and investigators are examining the cyberattacks.')
        rows = [self.row(1, *hack_a, event_type='INCIDENT', cc='KR'),
                self.row(2, *hack_b, event_type='INCIDENT', cc='KR'),
                self.row(3, *self.RBI_A, event_type='INCIDENT')]
        arts, _ = export_json._event_bucket(rows, 'INCIDENT', 10, lambda r: {'id': r['article_id'], 'cc': r['cc']})
        self.assertEqual([a['id'] for a in arts], [1, 3])

    def test_preview_is_hidden_once_the_decision_is_reported(self):
        items = [(1, '2026-10-06', 'RBI poised for first rate hike in nearly four years amid rupee, inflation pressures'),
                 (2, '2026-10-07', 'RBI raises repo rate 25bp to 5.5% for first time since Feb 2023'),
                 (3, '2026-10-06', "India's SEBI to reverse derivative settlement rules")]
        self.assertEqual(llm_dedup.superseded_ids(items), {1})
        # 결정 기사보다 나중의 예고(다음 회의 전망)는 남는다
        self.assertEqual(llm_dedup.superseded_ids(
            [(1, '2026-10-08', 'RBI poised for another rate hike in December'), items[1]]), set())


class CountryMentionTests(unittest.TestCase):
    def test_country_mentions(self):
        import kb_network
        self.assertFalse(kb_network.mentions_country(
            'SG', 'Hormuz tanker attacks hit weekly record since Iran conflict began'))
        self.assertTrue(kb_network.mentions_country('SG', "Singapore's MAS issues AI risk guidelines"))
        self.assertTrue(kb_network.mentions_country('US', 'HUD probes Wells Fargo over lending under Trump'))
        self.assertTrue(kb_network.mentions_country('MY', 'anything'))   # 앵커 없는 국가는 판단 안 함


if __name__ == '__main__':
    unittest.main()
