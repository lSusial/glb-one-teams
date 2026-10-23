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
import re

import config
import db
import kb_network
from llm_provider import LLMProvider, get_provider

log = logging.getLogger("llm_prefilter")

_SPARSE_RESCUE_HARD_REJECT = re.compile(
    r"홍보|광고|스포츠|연예|범죄|생활정보|지역\s*(?:행사|축제)|"
    r"promot(?:ion|ional)|advert|sponsored|sports?|entertainment|crime|local event|press release",
    re.I,
)

_SYSTEM = (
    "당신은 KB금융그룹 글로벌 거점 뉴스 인텔리전스의 1차 선별 담당이다.\n"
    "KB 해외 거점(GB·US·HK·CN·JP·SG·IN·VN·MM·ID·KH)의 경영에 유의미한지 판단한다.\n"
    "의심스러우면 keep.\n\n"
    "★ keep (아래 하나라도 해당하면 keep):\n"
    "  1. 거시경제: 거점 국가 금리·환율·물가·성장률\n"
    "  2. 에너지·원자재: 유가·가스·곡물·비철금속 가격; 원자재 대형 기업 실적·전략\n"
    "  3. 자본시장·기업실적: 주요 지수·채권, 거점 관련 대형 기업 실적·배당·M&A·IPO\n"
    "  4. 은행·금융규제: 은행 M&A·건전성·중앙은행 결정·자본통제·외환 규제\n"
    "  5. ESG: 대형 기업 기후·탄소·공급망 환경 리스크\n"
    "  6. 디지털금융: 핀테크·AI·빅테크 대형 M&A·IPO·규제\n"
    "  6-1. 사이버보안: 은행·결제망·금융당국 등 금융권 IT 장애·해킹·데이터 유출, "
    "또는 금융권 서버에 널리 쓰이는 기술(PHP·OpenSSL·Java 등)의 심각한 취약점·긴급 패치. "
    "거점 국가와 무관해 보여도 keep\n"
    "  7. 지정학(좁게): KB 거점 국가에 직접 파급되는 사건 "
    "(제재·자본통제·국가신용 변화·쿠데타·무역분쟁)만.\n"
    "     ✗ 제외: G7/G20 외교 가십, 중동·유럽 분쟁 일반 보도, 군사 교전 단신\n\n"
    "✗ drop (아래에 명확히 해당하는 것만):\n"
    "  • 스포츠·연예·범죄·지역 생활정보\n"
    "  • 개별 소형주 추천·주가 단신, 소형 기업 분기실적 단신\n"
    "  • 순수 홍보·광고성 보도\n\n"
    "JSON만 출력(설명 없이): "
    '{"keep": true|false, "reason": "15자 이내"}'
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

    date_clause, params = db.days_clause_now(days)

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
            f"요약: {(r['summary'] or '')[:config.PREFILTER_SUMMARY_MAXLEN]}"
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


def rescue_sparse_countries(conn, days: int | None = None) -> dict:
    """최근 창에서 keep가 하나도 없는 진출국의 drop 상위 후보를 rank까지 보낸다.

    전체 점수 임계는 낮추지 않는다. 키워드 필터를 통과했고 실제로 해당 국가를
    언급하는 기사만 소수 재검토해, 라오스·미얀마처럼 prefilter 단계에서 0건이 되는
    경우에도 rank가 최종 판단할 기회를 준다.
    """
    ensure_columns(conn)
    date_clause, params = db.days_clause_now(days)
    rows = conn.execute(
        f"""SELECT a.article_id, a.title, a.summary, a.filter_score,
                   {db.effective_country_expr()} AS cc, a.llm_prefilter, a.llm_reject_reason
            FROM articles_raw a JOIN media_sources m ON m.source_id=a.source_id
            WHERE a.filter_decision='passed' AND a.duplicate_of IS NULL{date_clause}
              AND {db.effective_country_expr()} IN ({','.join('?' * len(kb_network.KB_NETWORK))})
            ORDER BY a.filter_score DESC, a.published_at DESC""",
        (*params, *kb_network.KB_NETWORK.keys()),
    ).fetchall()
    keep_cc = {r["cc"] for r in rows if r["llm_prefilter"] == "keep"}
    picked: dict[str, list[int]] = {}
    for r in rows:
        cc = r["cc"]
        if (cc in keep_cc or r["llm_prefilter"] != "drop"
                or (r["filter_score"] or 0) < config.SPARSE_RESCUE_MIN_FILTER_SCORE):
            continue
        if _SPARSE_RESCUE_HARD_REJECT.search(r["llm_reject_reason"] or ""):
            continue
        text = f"{r['title']} {r['summary'] or ''}"
        if not kb_network.mentions_country(cc, text):
            continue
        slot = picked.setdefault(cc, [])
        if len(slot) < config.SPARSE_RESCUE_PER_COUNTRY:
            slot.append(int(r["article_id"]))
    ids = [aid for values in picked.values() for aid in values]
    if ids:
        conn.executemany(
            "UPDATE articles_raw SET llm_prefilter='keep', llm_reject_reason='sparse-country rescue' "
            "WHERE article_id=?", [(aid,) for aid in ids])
        conn.commit()
    return {"countries": len(picked), "rescued": len(ids), "article_ids": ids}
