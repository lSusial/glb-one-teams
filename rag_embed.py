"""
RAG 벡터 저장소 구축 (rag_embed.py)

점수/판정이 완료된 기사(ai_score 또는 llm_prefilter 보유)를 "청크"(제목+요약+분석결과
종합 텍스트)로 만들어 로컬 Ollama 임베딩 모델(bge-m3, 다국어)로 벡터화하고
article_embeddings 테이블에 저장한다. rag_search.py가 이걸로 의미검색을 수행한다.

*** 로컬 Ollama(localhost:11434)를 호출하므로 Ollama가 설치·실행 중인 머신(맥북)에서
    직접 실행해야 한다. 먼저 모델을 받아둔다: `ollama pull bge-m3` ***

청크 대상: ai_score IS NOT NULL OR llm_prefilter IS NOT NULL
          (= 뭔가 판정이 내려진 기사 전체 — 단순 키워드필터 거부분은 제외)
증분 처리: article_embeddings에 이미 있고 model이 같으면 건너뜀(재실행 안전).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

import numpy as np

import config
import db

log = logging.getLogger("rag_embed")


def ensure_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS article_embeddings (
            article_id  INTEGER PRIMARY KEY REFERENCES articles_raw(article_id),
            chunk_text  TEXT NOT NULL,
            embedding   BLOB NOT NULL,
            dim         INTEGER NOT NULL,
            model       TEXT NOT NULL,
            embedded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_article_embeddings_model ON article_embeddings(model)")
    conn.commit()


def build_chunk_text(row) -> str:
    """기사 한 건 → 임베딩용 종합 텍스트. 제목·요약·분석결과를 한 덩어리로."""
    parts = []
    title = row["title_ko"] or row["title"]
    parts.append(f"제목: {title}")

    if row["summary_ko"]:
        parts.append(f"요약: {row['summary_ko']}")
    elif row["summary_en"]:
        parts.append(f"요약(영문): {row['summary_en']}")

    if row["expanded_summary"]:
        parts.append(f"상세: {row['expanded_summary']}")

    if row["kb_implication"]:
        parts.append(f"KB 시사점: {row['kb_implication']}")

    if row["primary_country"]:
        parts.append(f"관련국가: {row['primary_country']}")

    if row["published_at"]:
        parts.append(f"게시일: {row['published_at'][:10]}")

    if row["ai_score"] is not None:
        parts.append(f"중요도점수(ai_score): {row['ai_score']}")

    decision_bits = []
    if row["llm_prefilter"]:
        decision_bits.append(f"프리필터={row['llm_prefilter']}")
    if row["local_rescue_grade"] is not None:
        decision_bits.append(f"안전망복구등급={row['local_rescue_grade']}")
    if decision_bits:
        parts.append(" / ".join(decision_bits))

    return "\n".join(parts)


def _embed_batch(texts: list[str], *, model: str, timeout: int = 120) -> list[list[float]]:
    payload = {"model": model, "input": texts}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(config.RAG_EMBED_URL, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    embeddings = body.get("embeddings")
    if not embeddings or len(embeddings) != len(texts):
        raise RuntimeError(f"임베딩 응답 이상(기대 {len(texts)}건, 수신 {len(embeddings) if embeddings else 0}건)")
    return embeddings


def run_embed(conn, *, model: str | None = None, batch_size: int | None = None,
             limit: int | None = None) -> dict:
    """미임베딩 대상(ai_score 또는 llm_prefilter 보유, 해당 model로 아직 임베딩 안 됨)을 벡터화.

    Ollama가 localhost:11434에서 응답해야 한다. 연결 불가 시 urllib.error.URLError를 올린다.
    """
    ensure_schema(conn)
    model = model or config.RAG_EMBED_MODEL
    batch_size = batch_size or config.RAG_EMBED_BATCH

    rows = conn.execute(
        f"""
        SELECT a.article_id, a.title, a.title_ko, a.summary_ko, a.summary_en,
               a.expanded_summary, a.kb_implication, a.primary_country,
               a.published_at, a.ai_score, a.llm_prefilter, a.local_rescue_grade
        FROM articles_raw a
        WHERE (a.ai_score IS NOT NULL OR a.llm_prefilter IS NOT NULL)
          AND a.article_id NOT IN (
              SELECT article_id FROM article_embeddings WHERE model = ?
          )
        ORDER BY a.published_at DESC
        {"LIMIT ?" if limit else ""}
        """,
        (model, limit) if limit else (model,),
    ).fetchall()

    stats = dict(total=len(rows), embedded=0, failed=0)
    if not rows:
        log.info("rag_embed — 신규 대상 없음(model=%s)", model)
        return stats

    cur = conn.cursor()
    for i in range(0, len(rows), batch_size):
        chunk_rows = rows[i : i + batch_size]
        texts = [build_chunk_text(r) for r in chunk_rows]
        try:
            vectors = _embed_batch(texts, model=model)
        except urllib.error.URLError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("배치 임베딩 실패(%d건, idx=%d): %s", len(chunk_rows), i, e)
            stats["failed"] += len(chunk_rows)
            continue

        for r, text, vec in zip(chunk_rows, texts, vectors):
            arr = np.asarray(vec, dtype=np.float32)
            cur.execute(
                """INSERT OR REPLACE INTO article_embeddings
                   (article_id, chunk_text, embedding, dim, model, embedded_at)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (r["article_id"], text, arr.tobytes(), arr.shape[0], model),
            )
            stats["embedded"] += 1

        conn.commit()
        log.info("rag_embed 진행 %d/%d", min(i + batch_size, len(rows)), len(rows))

    log.info("rag_embed 완료 — 전체=%d 임베딩=%d 실패=%d", stats["total"], stats["embedded"], stats["failed"])
    return stats


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s | %(message)s", datefmt="%H:%M:%S")
    p = argparse.ArgumentParser(description="판정완료 기사 임베딩 → article_embeddings")
    p.add_argument("--limit", type=int, help="처리할 최대 기사 수(기본 전체)")
    p.add_argument("--model", help=f"임베딩 모델(기본 {config.RAG_EMBED_MODEL})")
    args = p.parse_args()

    conn = db.open_conn()
    s = run_embed(conn, model=args.model, limit=args.limit)
    print(f"[rag-embed] 전체={s['total']}  임베딩={s['embedded']}  실패={s['failed']}")
