"""
본문 추출 (fulltext.py)

prefilter 통과(keep) 기사의 원문 URL을 열어 **본문 전체**를 추출 → articles_raw.full_text 저장.
얕은 RSS 스니펫(평균 ~141B) 대신 본문으로 rank 분석 품질을 끌어올린다. 추가 API 비용 0(무료).

파이프라인 위치: prefilter(스니펫으로 keep/drop) → **fulltext(keep만 본문 추출)** → rank(본문으로 분석).
  · 살아남은 keep 집합만 fetch → 부하·차단 위험 최소화.
  · Google News 리다이렉트 링크는 googlenewsdecoder 로 실제 URL 해소(구식 redirect-follow 보완).
  · 본문 추출 = trafilatura. 실패 시 스니펫 유지(full_text 비움, rank 는 자동 폴백).
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
    ])


def _resolve(url: str) -> str | None:
    """Google News 리다이렉트 → 실제 기사 URL. 일반 URL은 그대로. 해소 실패 시 None."""
    if not url:
        return None
    if _GNEWS not in url:
        return url
    # 1) googlenewsdecoder (현행 batchexecute 방식)
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(url, interval=1)
        if r and r.get("status") and r.get("decoded_url") and _GNEWS not in r["decoded_url"]:
            return r["decoded_url"]
    except Exception as e:  # noqa: BLE001
        log.debug("gnewsdecoder 실패: %s", e)
    # 2) 폴백: 리다이렉트 추적
    try:
        import requests
        resp = requests.get(url, allow_redirects=True, timeout=config.FULLTEXT_TIMEOUT,
                            headers={"User-Agent": config.USER_AGENT}, stream=True)
        resp.close()
        if _GNEWS not in resp.url:
            return resp.url
    except Exception as e:  # noqa: BLE001
        log.debug("redirect 폴백 실패: %s", e)
    return None  # 미해소


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


def _process(article_id: int, link: str):
    """(article_id) → (article_id, 해소된_link|None, 본문|None)."""
    real = _resolve(link)
    if not real:
        return article_id, None, None
    text = _extract(real)
    if text:
        text = text[: config.FULLTEXT_MAXLEN]
    new_link = real if real != link else None
    return article_id, new_link, text


def run_fulltext(conn, limit: int | None = None, days: int | None = None,
                 workers: int | None = None) -> dict:
    """keep·본문미보유 기사의 원문 본문을 병렬 추출·저장.

    days: 지정 시 최근 N일 게시분만(전체 백로그 대신 최신치 — 부하 절감).
    """
    ensure_columns(conn)
    limit = limit or config.FULLTEXT_LIMIT
    workers = workers or config.FULLTEXT_WORKERS

    date_clause, params = "", []
    if days:
        date_clause = " AND substr(a.published_at, 1, 10) >= date('now', ?)"
        params.append(f"-{int(days)} days")

    rows = conn.execute(
        f"""
        SELECT a.article_id, a.link
        FROM articles_raw a
        WHERE a.llm_prefilter = 'keep'
          AND a.duplicate_of IS NULL
          AND COALESCE(a.full_text, '') = ''{date_clause}
        ORDER BY a.filter_score DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    stats = dict(total=len(rows), extracted=0, resolved=0, failed=0)
    if not rows:
        log.info("본문 추출 대상 없음")
        return stats

    cur = conn.cursor()
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_process, r["article_id"], r["link"]) for r in rows]
        done = 0
        for fut in cf.as_completed(futs):
            aid, new_link, text = fut.result()
            if new_link:
                cur.execute("UPDATE articles_raw SET link = ? WHERE article_id = ?",
                            (new_link[:2000], aid))
                stats["resolved"] += 1
            if text:
                cur.execute("UPDATE articles_raw SET full_text = ? WHERE article_id = ?",
                            (text, aid))
                stats["extracted"] += 1
            else:
                stats["failed"] += 1
            done += 1
            if done % 50 == 0:
                conn.commit()
                log.info("본문 추출 진행 %d/%d (성공=%d)", done, len(rows), stats["extracted"])
    conn.commit()

    log.info("본문 추출 완료 — 대상=%d  본문=%d  URL해소=%d  실패=%d",
             stats["total"], stats["extracted"], stats["resolved"], stats["failed"])
    return stats
