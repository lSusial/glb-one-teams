"""
LLM 1차 관문 (llm_prefilter.py)

키워드 필터를 통과(passed)했으나 실제로는 KB 글로벌 인텔리전스와 무관한
노이즈(스포츠·연예·범죄가십·단순 지역행사 등)를 LLM으로 한 번 더 걸러낸다.

입력: filter_decision='passed' AND duplicate_of IS NULL AND llm_prefilter IS NULL
순서: filter_score DESC (고점수 우선 — 한도 내에서 중요 기사부터)
출력: llm_prefilter ('keep'|'drop'), llm_reject_reason

* 저비용 모델(role='fast') 사용.
* provider 생성 시 API 키가 없으면 RuntimeError → 키 없이 자동 호출되지 않음.
* 불확실/응답 파싱 실패 시 보수적으로 keep(보존).
"""
from __future__ import annotations

import logging

import config
import db
from llm_provider import LLMProvider, get_provider

log = logging.getLogger("llm_prefilter")

_SYSTEM = (
    "당신은 KB금융그룹 글로벌 거점 뉴스 인텔리전스의 1차 선별 담당이다. "
    "주어진 기사가 KB의 해외 지점·법인·자회사 경영(거시경제·금융시장·은행산업·"
    "규제·리스크·디지털금융·ESG)에 유의미한지 판단한다. "
    "스포츠·연예·범죄가십·단순 지역행사·순수 홍보성 기사는 제외한다.\n"
    '반드시 JSON만 출력: {"keep": true|false, "reason": "간단한 근거(한국어)"}'
)


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("llm_prefilter",     "ALTER TABLE articles_raw ADD COLUMN llm_prefilter     TEXT"),
        ("llm_reject_reason", "ALTER TABLE articles_raw ADD COLUMN llm_reject_reason TEXT"),
    ])


def run_prefilter(conn, provider: LLMProvider | None = None,
                  limit: int | None = None, days: int | None = None,
                  use_batch: bool | None = None) -> dict:
    """prefilter 미처리(passed·비중복) 기사를 LLM으로 keep/drop 판정.

    days: 지정 시 최근 N일 게시 기사만 처리(전체 백로그 대신 최신치만 — 비용 절감).
    use_batch: None=배치(50% 할인, 기본) / False=동기 호출(디버깅).
    """
    ensure_columns(conn)
    provider = provider or get_provider("fast", use_batch=use_batch)
    limit = limit or config.PREFILTER_LIMIT

    date_clause, params = "", []
    if days:
        date_clause = " AND substr(a.published_at, 1, 10) >= date('now', ?)"
        params.append(f"-{int(days)} days")

    rows = conn.execute(
        f"""
        SELECT a.article_id, a.title, a.summary, m.primary_country_code AS cc
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.filter_decision = 'passed'
          AND a.duplicate_of IS NULL
          AND a.llm_prefilter IS NULL{date_clause}
        ORDER BY a.filter_score DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    stats = dict(total=len(rows), keep=0, drop=0)

    # 요청 일괄 구성 → 배치 제출(50% 할인) 또는 동기 폴백
    requests, row_by_id = [], {}
    for i, r in enumerate(rows):
        cid = str(i)
        user = (
            f"[국가:{r['cc']}] 제목: {r['title']}\n"
            f"요약: {(r['summary'] or '')[:600]}"
        )
        requests.append((cid, _SYSTEM, user, 200))
        row_by_id[cid] = r

    results = provider.complete_json_batch(requests) if requests else {}

    cur = conn.cursor()
    for cid, r in row_by_id.items():
        data = results.get(cid) or {}
        keep = bool(data.get("keep", True))  # 폴백: 불확실/누락이면 보존
        reason = str(data.get("reason", ""))[:300]
        decision = "keep" if keep else "drop"
        stats[decision] += 1
        cur.execute(
            "UPDATE articles_raw SET llm_prefilter = ?, llm_reject_reason = ? WHERE article_id = ?",
            (decision, None if keep else reason, r["article_id"]),
        )
    conn.commit()

    log.info("프리필터 완료 — 전체=%d  keep=%d  drop=%d", stats["total"], stats["keep"], stats["drop"])
    return stats
