"""
LLM 근접중복 판정 (llm_dedup.py, 2026-09-16)

토큰 유사도(keyword_filter의 duplicate_of, 제목 0.75)로 못 잡는 '같은 사건 다른 표현'
근접중복을 LLM 의미판단으로 묶는다.
예: "Rupee falls 30 paise to 95.84" vs "Rupee slumps 38 paise to 95.92" (같은 날 루피 하락).

범위: 노출 후보(최근 N일, ai_score 있고 아직 비중복)만 — 국가별 전체 후보를 한 요청에서 비교한다.
출력: 각 그룹의 최고 ai_score를 대표로, 나머지는 duplicate_of=대표id + dup_by_ai=1.
재실행 안전: 유효 응답을 받은 국가의 검사 대상만 원자적으로 교체한다.
표시 피드(국가·모니터링)는 이미 duplicate_of IS NULL 로 거르므로 export 변경 불필요.
"""
from __future__ import annotations

import logging

import config
import db
from llm_provider import LLMProvider, get_provider

log = logging.getLogger("llm_dedup")

_SYSTEM = (
    "당신은 뉴스 근접중복 판정기다. 같은 국가 헤드라인 목록에서 '같은 하나의 사건·발표·흐름을 "
    "보도한' 기사들을 그룹으로 묶는다. 독자에게 '같은 뉴스'로 읽히면 한 그룹이다.\n"
    "다음은 모두 한 그룹으로 본다(수치·표현·매체·각도만 다른 경우):\n"
    "  · 같은 발표를 여러 각도·반응·후속으로 보도 — 예: 같은 중앙은행의 '한 번의' 금리결정을 "
    "'인상 결정'·'시장/국채 반응'·'당국자 발언'·'정치권 반응'·'분석'으로 각각 → 전부 한 그룹.\n"
    "  · 같은 규제·정책의 도입·세부·시행·분쟁·소송을 나눠 보도 — 예: 같은 수수료 정책의 "
    "'X bp 인상'·'대법원 소송'·'GST 부과'·'소상인 반발'·'체계 개편' → 한 그룹.\n"
    "  · 같은 거래·상장·인사를 여러 매체가 반복 보도.\n"
    "★ 근본 사건이 다르면 묶지 마라: 다른 날짜의 별개 결정, 다른 기업·종목의 거래, "
    "서로 무관한 주제(물가 ↔ 환율 ↔ 개별기업처럼 사건이 다른 것)는 각각 둔다.\n"
    "핵심 기준: '같은 하나의 사건/정책/거래'면 각도가 달라도 묶고, '별개 사건'이면 나눈다.\n"
    "JSON만 출력(설명 없이): {\"groups\": [[id, id, ...], ...]}  "
    "— 2건 이상 묶인 그룹만 넣고, 단독 기사는 생략."
)


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("dup_by_ai", "ALTER TABLE articles_raw ADD COLUMN dup_by_ai INTEGER DEFAULT 0"),
    ])


def run_dedup(conn, provider: LLMProvider | None = None,
              days: int | None = 3, use_batch: bool | None = None,
              only_cc: str | None = None) -> dict:
    """노출 후보를 국가별로 LLM 근접중복 판정. only_cc 지정 시 그 국가만(디버그)."""
    ensure_columns(conn)
    provider = provider or get_provider("fast", use_batch=use_batch)

    date_clause, params = db.days_clause_now(days)
    rows = conn.execute(
        f"""
        SELECT a.article_id, COALESCE(a.title_ko, a.title) AS t, a.ai_score, a.published_at,
               COALESCE(NULLIF(a.primary_country, ''), m.primary_country_code) AS cc
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.ai_score IS NOT NULL
          AND (a.duplicate_of IS NULL OR a.dup_by_ai = 1)
          AND a.ai_model LIKE '%:%'{date_clause}
        ORDER BY a.ai_score DESC, a.published_at DESC, a.article_id ASC
        """,
        params,
    ).fetchall()

    by_cc: dict[str, list] = {}
    for r in rows:
        cc = r["cc"] or "??"
        if only_cc and cc != only_cc:
            continue
        by_cc.setdefault(cc, []).append(r)

    requests, ctx = [], {}
    oversized = 0
    for cc, arts in by_cc.items():
        if len(arts) < 2:
            continue
        lines = "\n".join(f"{a['article_id']}: [{(a['published_at'] or '')[:10]}] {(a['t'] or '')[:160]}" for a in arts)
        # 광범위한 백필에서 컨텍스트를 넘기지 않는다. 일부만 잘라 검사하지 않고
        # 기존 결과를 보존하며 명시적으로 실패 집계한다(더 좁은 days로 재실행).
        if len(lines) > 80000:
            oversized += len(arts)
            log.warning("AI 중복판정 입력 초과 — 국가=%s 후보=%d, days를 줄여 재실행 필요", cc, len(arts))
            continue
        cid = str(cc)
        requests.append((cid, _SYSTEM, f"[국가:{cc}] 헤드라인 목록:\n{lines}", min(16000, max(1024, len(arts) * 24))))
        ctx[cid] = {a["article_id"]: (a["ai_score"] or 0) for a in arts}

    results = provider.complete_json_batch(requests) if requests else {}

    marked = reviewed = 0
    failed = oversized
    for cid, scores in ctx.items():
        res = results.get(cid)
        groups = res.get("groups") if isinstance(res, dict) else None
        # 누락/파싱 실패와 명시적인 groups=[]를 구분한다. 중복 ID/겹치는 그룹은
        # 순환 연결을 만들 수 있으므로 국가 응답 전체를 거부하고 기존 결과를 유지한다.
        seen = set()
        valid = isinstance(groups, list)
        if valid:
            for group in groups:
                if (not isinstance(group, list) or len(group) < 2
                        or any(type(aid) is not int or aid not in scores for aid in group)
                        or len(set(group)) != len(group) or seen.intersection(group)):
                    valid = False
                    break
                seen.update(group)
        if not valid:
            failed += len(scores)
            log.warning("AI 중복판정 응답 무효 — 국가=%s 기존 결과 보존, 후보=%d", cid, len(scores))
            continue
        # API 호출 중에는 기존 연결을 유지한다. 결과 검증 후에만 같은 트랜잭션에서 교체.
        with conn:
            conn.executemany(
                "UPDATE articles_raw SET duplicate_of=NULL, dup_by_ai=0 "
                "WHERE article_id=? AND dup_by_ai=1", [(aid,) for aid in scores])
            for group in groups:
                rep = max(group, key=lambda aid: (scores[aid], -aid))
                conn.executemany(
                    "UPDATE articles_raw SET duplicate_of=?, dup_by_ai=1 WHERE article_id=?",
                    [(rep, aid) for aid in group if aid != rep])
                marked += len(group) - 1
        reviewed += len(scores)

    log.info("AI 중복판정 완료 — 국가=%d 검사=%d 실패보존=%d 중복마킹=%d건",
             len(requests), reviewed, failed, marked)
    return {"countries": len(requests), "marked": marked,
            "reviewed": reviewed, "failed": failed}
