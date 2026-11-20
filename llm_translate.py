"""
번역 (llm_translate.py)

영어 기준본(summary_en)을 한국어(summary_ko)로 번역.
원문이 대부분 영어라 rank 는 영어로 분석·요약(canonical)하고, 이 단계가 한국어를 채운다.

원칙:
  - 대상은 '화면에 표시되는' 기사 → 비용 최소화. 단 '표시'의 기준이 화면마다 다르다:
      · 진출국 화면      : ACTIVE(ai_score >= 임계)
      · 미진출국·모니터링 : **임계 없음**(export._compute_non_presence 주석 참조 —
        거점이 없어 점수 루브릭이 낮게 잡히므로 랭킹만 됐으면 노출)
    2026-09-08: 이 둘이 어긋나 미진출국 저점수 기사가 summary_ko 없이 노출됐고,
    UI 폴백(q || q_en)이 걸려 한국어 화면에 영어 요약이 그대로 보였다. 대상을 맞춘다.
  - 양 언어가 정상인 기사는 스킵. 수치·기간·가나 혼입 오류는 해당 한국어 필드만 재번역
  - 레거시(한국어만 있고 영어 없음)는 역방향(KO→EN)도 채워 토글이 양쪽 동작
  - 저비용 모델(role='fast', Haiku)

* 향후 현지어(인니어 등)는 이 모듈에 타깃 언어만 추가하면 영어 기준본에서 fan-out.
"""
from __future__ import annotations

import logging

import config
import db
import numeric_guard
import text_quality
from llm_provider import LLMProvider, get_provider

log = logging.getLogger("llm_translate")

_SYS = {
    "ko": ("You are rewriting financial news into concise Korean for busy bankers. "
           "Rules: ① title_ko — 15자 이내, 핵심 사실만, 신문 헤드라인 스타일 "
           "② summary — 2문장 이내, 단문, '~다' 체. "
           "   중요: title_ko 내용을 절대 반복하지 말 것. title_ko가 다루지 않은 배경·수치·영향만 서술. "
           "Keep KB branch names (예: 뉴욕지점, 프라삭은행). "
           "Preserve trading-session qualifiers: first-session close = 오전장 마감, "
           "intraday = 장중, full-day close = 종가. Never drop the session qualifier. "
           'Output ONLY JSON: {"title_ko": "...", "summary": "..."}'
           + numeric_guard.PRESERVE_MONEY_PROMPT),
    "en": ("Translate the 'summary' below into natural English "
           "(financial/business register). Keep KB branch/entity names. "
           'Output ONLY JSON: {"summary": "..."}' + numeric_guard.PRESERVE_MONEY_PROMPT),
}


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("summary_en", "ALTER TABLE articles_raw ADD COLUMN summary_en TEXT"),
        ("title_ko",   "ALTER TABLE articles_raw ADD COLUMN title_ko   TEXT"),
        ("title_en",   "ALTER TABLE articles_raw ADD COLUMN title_en   TEXT"),
    ])


def _user(summary, title=""):
    lines = []
    if title:
        lines.append(f"title: {title[:200]}")
    lines.append(f"summary: {(summary or '')[:1500]}")
    return "\n".join(lines)


def _long_window_visible_ids(conn) -> set[int]:
    """7~30일 화면에서 실제 선택된 카드의 ID만 반환한다.

    event_type/personnel/korean_fi 태그 전체를 번역하면 화면별 상한과 중복 제거 전에
    수백 건이 잡힌다. export와 같은 선택 함수를 거친 URL만 ID로 되돌려 비용을
    화면에 실제 노출되는 기사로 제한한다.
    """
    import export_json

    urls: set[str] = set()

    def collect(value):
        if isinstance(value, dict):
            if value.get("u"):
                urls.add(value["u"])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(export_json._compute_topics(conn, days=7))
    collect(export_json._compute_korean_fi(conn, days=30))
    collect(export_json._compute_personnel(conn, days=30))
    if not urls:
        return set()
    placeholders = ",".join("?" for _ in urls)
    return {int(r[0]) for r in conn.execute(
        f"SELECT article_id FROM articles_raw WHERE link IN ({placeholders})", tuple(urls))}


def run_translate(conn, provider: LLMProvider | None = None,
                  limit: int | None = None, days: int | None = None,
                  use_batch: bool | None = None,
                  article_ids: list[int] | None = None) -> dict:
    """표시분(ACTIVE) 중 한쪽 언어가 비어있는 기사를 번역해 양 언어를 채운다.

    use_batch: None=배치(50% 할인, 기본) / False=동기 호출(디버깅).
    """
    ensure_columns(conn)
    provider = provider or get_provider("fast", use_batch=use_batch)   # 저비용(Haiku)
    limit = limit or 400   # 미진출국까지 대상에 들어와 한 번에 처리할 물량이 늘었다
    # 노출 대상 = ACTIVE(진출국 기준) 또는 미진출국의 채점 완료분(임계 없이 노출됨).
    np_in = ",".join("?" * len(config.NON_PRESENCE_CODES))
    import export_json
    # 국가 화면은 항상 전일+당일(days=1) 기준이므로, AI 처리창(--days 2)과 무관하게
    # 실제 export에 선택되는 관심 기사만 번역한다.
    watch_ids = {aid for ids in export_json.country_watch_candidate_ids(conn, 1).values() for aid in ids}
    watch_in = ",".join("?" * len(watch_ids)) or "NULL"
    long_ids = _long_window_visible_ids(conn)
    long_in = ",".join("?" * len(long_ids)) or "NULL"

    rows = conn.execute(
        f"""
        SELECT a.article_id, a.title, a.title_en, a.summary_en,
               a.summary_ko, a.title_ko
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.duplicate_of IS NULL
          AND (
            ( (a.ai_score >= ?
               OR (m.primary_country_code IN ({np_in}) AND a.ai_score IS NOT NULL)
               OR (a.topics LIKE '%SOCIETY%' AND a.ai_score >= ?)
               OR a.article_id IN ({watch_in}))
              AND substr(a.published_at,1,10) >= date(
                    (SELECT MAX(substr(published_at,1,10)) FROM articles_raw), '-1 day') )
            -- 긴 기간 화면은 상한·중복 제거 뒤 실제 선택된 카드만 번역한다.
            OR a.article_id IN ({long_in})
          )
          AND (COALESCE(a.summary_en,'') <> '' OR COALESCE(a.summary_ko,'') <> '')
        ORDER BY a.ai_score DESC
        """,
        (config.AI_SCORE_ACTIVE_THRESHOLD, *config.NON_PRESENCE_CODES, config.SOCIETY_ACTIVE_FLOOR,
         *watch_ids, *long_ids),
    ).fetchall()
    if article_ids is not None:
        allowed = {int(x) for x in article_ids}
        rows = [r for r in rows if int(r["article_id"]) in allowed]

    stats = dict(total=0, ko=0, en=0)

    # 요청 일괄 구성(방향은 custom_id 에 인코딩) → 배치 제출(50% 할인) 또는 동기 폴백
    requests, meta = [], {}
    for i, r in enumerate(rows):
        has_en = bool((r["summary_en"] or "").strip())
        has_ko = bool((r["summary_ko"] or "").strip())
        has_title_ko = bool((r["title_ko"] or "").strip())
        en_title = r['title_en'] or r['title'] or ''
        bad_summary = (not has_ko or text_quality.korean_issues(r['summary_en'] or '', r['summary_ko'] or '')
                       or numeric_guard.amount_mismatch(r['summary_en'] or '', r['summary_ko'] or ''))
        bad_title = (not has_title_ko or text_quality.korean_issues(en_title, r['title_ko'] or '', headline=True)
                     or numeric_guard.amount_mismatch(en_title+' '+(r['summary_en'] or ''), r['title_ko'] or ''))
        if has_en and (bad_summary or bad_title):   # 정상 필드는 유지하고 문제 필드만 교체한다.
            cid, target = str(i), "ko"
            src_s = r["summary_en"]
        elif has_ko and not has_en:                        # KO(레거시) → EN
            cid, target = str(i), "en"
            src_s = r["summary_ko"]
        else:
            continue
        requests.append((cid, _SYS[target], _user(src_s, en_title), 600))
        meta[cid] = (r, target, src_s, en_title, bad_summary, bad_title)
        if len(requests) >= limit:
            break

    stats['total'] = len(requests)

    results = provider.complete_json_batch(requests) if requests else {}

    cur = conn.cursor()
    retry = []

    def korean_candidate(data, row, src, title, bad_summary, bad_title):
        s = str(data.get('summary') or '')[:1500] if bad_summary else row['summary_ko']
        t = str(data.get('title_ko') or '')[:100] if bad_title else row['title_ko']
        errors = (not s or not t or numeric_guard.amount_mismatch(src+' '+title, s+' '+t)
                  or text_quality.korean_issues(src, s)
                  or text_quality.korean_issues(title, t, headline=True))
        return None if errors else (s,t)

    for cid, (r, target, src_s, title, bad_summary, bad_title) in meta.items():
        article_id = r['article_id']
        data = results.get(cid) or {}
        s = str(data.get("summary") or "")[:1500]
        if not s:
            log.warning("번역 응답 없음/무효 — 저장 안 함(article_id=%s)", article_id)
            continue
        if target == "ko":
            candidate = korean_candidate(data,r,src_s,title,bad_summary,bad_title)
            if candidate is None:
                retry.append((cid,_SYS['ko'],_user(src_s,title)+'\nPrevious output failed validation. '
                              'Preserve exchange-rate units and percentage periods; remove Japanese kana.',600))
                continue
            cur.execute("UPDATE articles_raw SET summary_ko=?, title_ko=? WHERE article_id=?",
                        (*candidate, article_id))
            stats["ko"] += 1
        else:
            if numeric_guard.amount_mismatch(src_s, s):
                log.warning("번역 금액 불일치 — 저장 안 함(article_id=%s)", article_id)
                continue
            cur.execute("UPDATE articles_raw SET summary_en=? WHERE article_id=?",
                        (s, article_id))
            stats["en"] += 1
    again = provider.complete_json_batch(retry) if retry else {}
    for cid, *_ in retry:
        r, _, src, title, bad_summary, bad_title = meta[cid]
        candidate = korean_candidate(again.get(cid) or {},r,src,title,bad_summary,bad_title)
        if candidate is None:
            log.warning('번역 재검증 실패 — 기존 필드 보존(article_id=%s)', r['article_id'])
            continue
        cur.execute('UPDATE articles_raw SET summary_ko=?,title_ko=? WHERE article_id=?',(*candidate,r['article_id']))
        stats['ko'] += 1
    conn.commit()

    log.info("번역 완료 — 대상=%d  KO채움=%d  EN채움=%d", stats["total"], stats["ko"], stats["en"])
    return stats
