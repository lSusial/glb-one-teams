"""
본문 추출 (fulltext.py)

prefilter 통과(keep) 기사의 원문 URL을 열어 **본문 전체**를 추출 → articles_raw.full_text 저장.
얕은 RSS 스니펫(평균 ~141B) 대신 본문으로 rank 분석 품질을 끌어올린다. 추가 API 비용 0(무료).

파이프라인 위치: prefilter(스니펫으로 keep/drop) → **fulltext(keep만 본문 추출)** → rank(본문으로 분석).
  · 살아남은 keep 집합만 fetch → 부하·차단 위험 최소화.
  · Google News 리다이렉트 링크는 googlenewsdecoder 로 실제 URL 해소 — 순차 요청 + 429 연속 시 중단
    (2026-09-29: 병렬 요청·구식 redirect-follow가 Google 요청 한도를 소진해 전부 429로 막혔음).
  · resolve_display_links(): rank 후 노출 기사 중 GN 링크가 남은 것을 점수순으로 추가 해소.
  · 본문 추출 = trafilatura. 실패 시 스니펫 유지(full_text 비움, rank 는 자동 폴백).
  · 성공/URL 미해소/추출 실패와 시각을 기록하고, 실패 URL은 3일 간격으로 재시도한다.
  · 네트워크 개방 환경(맥북)에서 실행. 병렬 fetch.

필요 패키지: trafilatura, googlenewsdecoder  (requirements.txt)
"""
from __future__ import annotations

import concurrent.futures as cf
import logging

import config
import db

log = logging.getLogger("fulltext")

_GNEWS = "news.google."


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("full_text", "ALTER TABLE articles_raw ADD COLUMN full_text TEXT"),
        ("fulltext_status", "ALTER TABLE articles_raw ADD COLUMN fulltext_status TEXT"),
        ("fulltext_attempted_at", "ALTER TABLE articles_raw ADD COLUMN fulltext_attempted_at TEXT"),
        ("fulltext_failure_reason", "ALTER TABLE articles_raw ADD COLUMN fulltext_failure_reason TEXT"),
    ])
    # 상태 컬럼 도입 전 이미 확보한 본문은 성공으로 소급 표시한다.
    conn.execute(
        "UPDATE articles_raw SET fulltext_status='ok' "
        "WHERE COALESCE(full_text, '') != '' AND fulltext_status IS NULL"
    )
    conn.commit()


_BLOCK_AFTER = 3   # 429(요청 한도 초과)가 연속 이만큼 나오면 이번 실행의 디코딩을 멈춘다


def decode_serial(pairs, failed: set | None = None) -> dict:
    """[(article_id, link)] 중 Google News 링크를 실제 기사 URL로 순차 해소한다.

    병렬 요청은 Google 요청 한도를 금방 소진해 IP가 일시 차단(429)된다. 한 건씩 간격을 두고
    요청하고, 429가 연속되면 나머지는 다음 실행으로 넘겨 차단이 길어지지 않게 한다.
    반환: {article_id: 해소된 URL} — 실패·미시도는 빠진다.
    failed(선택): 확정 실패(429가 아닌 디코딩 실패) id를 담는다. 429·미시도는 일시적이라 넣지 않는다.
    """
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        log.warning("googlenewsdecoder 미설치 — Google News 링크 해소 건너뜀")
        return {}
    out, blocked = {}, 0
    for aid, url in pairs:
        if not url or _GNEWS not in url:
            continue
        try:
            r = gnewsdecoder(url, interval=1) or {}
        except Exception as e:  # noqa: BLE001
            r = {"status": False, "message": str(e)}
        decoded = r.get("decoded_url") or ""
        if r.get("status") and decoded and _GNEWS not in decoded:
            out[aid] = decoded
            blocked = 0
        elif "429" in str(r.get("message") or ""):
            blocked += 1
            if blocked >= _BLOCK_AFTER:
                log.warning("Google News 디코딩 429 연속 %d회 — 이번 실행 중단(해소 %d건)", blocked, len(out))
                break
        else:
            blocked = 0
            if failed is not None:
                failed.add(aid)
            log.debug("gnewsdecoder 실패: %s", r.get("message"))
    return out


def _extract(url: str) -> str | None:
    """trafilatura 로 본문 추출. 실패 시 None."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return text or None
    except Exception as e:  # noqa: BLE001
        log.debug("본문 추출 실패(%s): %s", url, e)
        return None


def _process(article_id: int, url: str):
    """(article_id, 실제 URL) → (article_id, 본문|None)."""
    text = _extract(url)
    if text:
        text = text[: config.FULLTEXT_MAXLEN]
    return article_id, text


def run_fulltext(conn, limit: int | None = None, days: int | None = None,
                 workers: int | None = None, min_score: int | None = None,
                 balance_countries: bool = False) -> dict:
    """keep·본문미보유 기사의 원문 본문을 병렬 추출·저장.

    days: 지정 시 최근 N일 게시분만(전체 백로그 대신 최신치 — 부하 절감).
    min_score: 지정 시 이미 채점된 기사 중 해당 점수 이상만 우선 추출한다.
    balance_countries: 국가별 1순위→2순위 순으로 뽑아 대형 국가의 한도 독점을 막는다.
    """
    ensure_columns(conn)
    limit = limit or config.FULLTEXT_LIMIT
    workers = workers or config.FULLTEXT_WORKERS

    date_clause, params = db.days_clause_now(days)
    retry_before = f"-{int(config.FULLTEXT_RETRY_DAYS)} days"

    score_clause = " AND a.ai_score >= ?" if min_score is not None else ""
    score_params = [int(min_score)] if min_score is not None else []
    order_by = "a.ai_score DESC, a.filter_score DESC" if min_score is not None else "a.filter_score DESC"
    country_rank = ""
    join_media = ""
    outer_order = "ai_score DESC, filter_score DESC" if min_score is not None else "filter_score DESC"
    if balance_countries:
        join_media = "JOIN media_sources m ON m.source_id = a.source_id"
        country_rank = (
            f", ROW_NUMBER() OVER (PARTITION BY {db.effective_country_expr()} "
            f"ORDER BY {order_by}) AS country_pos"
        )
        outer_order = "country_pos ASC, ai_score DESC, filter_score DESC"
    rows = conn.execute(
        f"""SELECT article_id, link FROM (
        SELECT a.article_id, a.link, a.ai_score, a.filter_score{country_rank}
        FROM articles_raw a {join_media}
        WHERE a.llm_prefilter = 'keep'
          AND a.duplicate_of IS NULL
          AND COALESCE(a.full_text, '') = ''
          AND (a.fulltext_status IS NULL OR a.fulltext_status = 'pending'
               OR a.fulltext_attempted_at <= datetime('now', ?)){score_clause}{date_clause}
        )
        ORDER BY {outer_order}
        LIMIT ?
        """,
        (retry_before, *score_params, *params, limit),
    ).fetchall()

    stats = dict(total=len(rows), extracted=0, extracted_ids=[], resolved=0, failed=0)
    if not rows:
        log.info("본문 추출 대상 없음")
        return stats

    cur = conn.cursor()
    # 1) Google News 링크는 먼저 순차 해소(병렬 X). 해소 못 한 GN 링크는 본문 추출을 건너뛴다.
    decode_failed: set = set()
    decoded = decode_serial([(r["article_id"], r["link"]) for r in rows], decode_failed)
    for aid, url in decoded.items():
        cur.execute("UPDATE articles_raw SET link = ? WHERE article_id = ?", (url[:2000], aid))
    stats["resolved"] = len(decoded)
    conn.commit()
    targets = [(r["article_id"], decoded.get(r["article_id"], r["link"])) for r in rows]
    unresolved = [aid for aid, url in targets if not url or _GNEWS in url]
    stats["failed"] = len(unresolved)
    # 확정 실패만 3일 재시도 제한 대상으로 기록한다. 429로 막혔거나 중단돼 시도 못 한 기사는
    # 상태를 남기지 않아 다음 실행에서 다시 뽑힌다(--days 2 창이라 3일 금지면 영구 누락).
    cur.executemany(
        "UPDATE articles_raw SET fulltext_status='unresolved_url', "
        "fulltext_attempted_at=CURRENT_TIMESTAMP, fulltext_failure_reason='google_news_decode_failed' "
        "WHERE article_id=?",
        [(aid,) for aid in unresolved if aid in decode_failed],
    )
    targets = [(aid, u) for aid, u in targets if u and _GNEWS not in u]

    # 2) 실제 URL만 병렬 본문 추출
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_process, aid, url) for aid, url in targets]
        done = 0
        for fut in cf.as_completed(futs):
            aid, text = fut.result()
            if text:
                cur.execute("UPDATE articles_raw SET full_text = ?, fulltext_status='ok', "
                            "fulltext_attempted_at=CURRENT_TIMESTAMP, fulltext_failure_reason=NULL "
                            "WHERE article_id = ?",
                            (text, aid))
                stats["extracted"] += 1
                stats["extracted_ids"].append(aid)
            else:
                cur.execute("UPDATE articles_raw SET fulltext_status='extract_failed', "
                            "fulltext_attempted_at=CURRENT_TIMESTAMP, "
                            "fulltext_failure_reason='empty_or_blocked' WHERE article_id=?", (aid,))
                stats["failed"] += 1
            done += 1
            if done % 50 == 0:
                conn.commit()
                log.info("본문 추출 진행 %d/%d (성공=%d)", done, len(targets), stats["extracted"])
    conn.commit()

    log.info("본문 추출 완료 — 대상=%d  본문=%d  URL해소=%d  실패=%d",
             stats["total"], stats["extracted"], stats["resolved"], stats["failed"])
    return stats


def resolve_display_links(conn, days: int | None = None, limit: int | None = None) -> dict:
    """채점된(화면 노출 후보) 기사와 그 형제(관련 기사 링크) 중 아직 Google News 링크인 것을 해소한다.

    본문 추출은 keep 상위 FULLTEXT_LIMIT건만 돌아, 한도 밖(미진출국·주제확장 피드 등)은 GN 링크
    그대로 화면에 나갔다(2026-09-29 노출 107건 중 46건). rank 이후 실제 노출될 기사부터 채운다.
    """
    ensure_columns(conn)
    limit = limit or config.DISPLAY_LINK_RESOLVE_LIMIT
    date_clause, params = db.days_clause_now(days)
    # 대표(채점된 비중복 기사) 먼저 점수순, 그다음 그 대표에 묶인 형제(모달 '관련 기사 링크') —
    # 형제는 미채점 키워드 중복도 있어 대표 점수로 정렬한다.
    rows = conn.execute(
        f"""
        SELECT a.article_id, a.link
        FROM articles_raw a
        LEFT JOIN articles_raw p ON p.article_id = a.duplicate_of
        WHERE a.link LIKE '%news.google.%'{date_clause}
          AND ((a.duplicate_of IS NULL AND a.ai_score IS NOT NULL)
               OR (p.duplicate_of IS NULL AND p.ai_score IS NOT NULL))
        ORDER BY (a.duplicate_of IS NULL) DESC, COALESCE(a.ai_score, p.ai_score) DESC, a.published_at DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    decoded = decode_serial([(r["article_id"], r["link"]) for r in rows])
    for aid, url in decoded.items():
        conn.execute(
            "UPDATE articles_raw SET link = ?, "
            "fulltext_status=CASE WHEN COALESCE(full_text, '')='' THEN 'pending' ELSE fulltext_status END, "
            "fulltext_attempted_at=CASE WHEN COALESCE(full_text, '')='' THEN NULL ELSE fulltext_attempted_at END, "
            "fulltext_failure_reason=CASE WHEN COALESCE(full_text, '')='' THEN NULL ELSE fulltext_failure_reason END "
            "WHERE article_id = ?",
            (url[:2000], aid),
        )
    conn.commit()
    log.info("노출 기사 링크 해소 — 대상=%d  해소=%d", len(rows), len(decoded))
    return {"total": len(rows), "resolved": len(decoded)}
