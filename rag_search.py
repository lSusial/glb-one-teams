"""
RAG 의미검색 (rag_search.py)

rag_embed.py로 쌓은 article_embeddings에서, 질문을 같은 모델로 임베딩한 뒤
코사인 유사도 top-k 기사를 찾아 보여준다. 규모(수만~10만 건 선)에서는 ANN
인덱스 없이 numpy 브루트포스로 충분히 빠르다(< 1초).

*** 로컬 Ollama 필요 — rag_embed.py와 동일 모델(기본 bge-m3)로 질문도 임베딩 ***

사용법:
    python3 rag_search.py "베트남 중앙은행 금리 관련 최근 기사"
    python3 rag_search.py "최근 3개월 Fed 정책 전환" --topk 15 --synthesize
"""
from __future__ import annotations

import json
import logging
import urllib.request

import numpy as np

import config
import db
from rag_embed import _embed_batch, ensure_schema

log = logging.getLogger("rag_search")


def _load_matrix(conn, model: str) -> tuple[list[int], np.ndarray]:
    rows = conn.execute(
        "SELECT article_id, embedding, dim FROM article_embeddings WHERE model = ?", (model,)
    ).fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    dim = rows[0]["dim"]
    ids = [r["article_id"] for r in rows]
    mat = np.frombuffer(b"".join(r["embedding"] for r in rows), dtype=np.float32).reshape(len(rows), dim)
    return ids, mat


def search(conn, query: str, *, model: str | None = None, topk: int | None = None) -> list[dict]:
    """질문 → 코사인 유사도 top-k 기사 리스트([{article_id, score, title, ...}, ...])."""
    ensure_schema(conn)
    model = model or config.RAG_EMBED_MODEL
    topk = topk or config.RAG_SEARCH_TOPK

    ids, mat = _load_matrix(conn, model)
    if not ids:
        return []

    q_vec = np.asarray(_embed_batch([query], model=model)[0], dtype=np.float32)

    mat_norm = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8)
    q_norm = q_vec / (np.linalg.norm(q_vec) + 1e-8)
    sims = mat_norm @ q_norm

    top_idx = np.argsort(-sims)[:topk]
    top_ids = [ids[i] for i in top_idx]
    top_scores = {ids[i]: float(sims[i]) for i in top_idx}

    placeholders = ",".join("?" * len(top_ids))
    meta_rows = conn.execute(
        f"""SELECT article_id, title, title_ko, summary_ko, summary_en, primary_country,
                   published_at, ai_score, link
            FROM articles_raw WHERE article_id IN ({placeholders})""",
        top_ids,
    ).fetchall()
    meta_by_id = {r["article_id"]: r for r in meta_rows}

    results = []
    for aid in top_ids:
        r = meta_by_id.get(aid)
        if r is None:
            continue
        results.append({
            "article_id": aid,
            "score": top_scores[aid],
            "title": r["title_ko"] or r["title"],
            "summary": r["summary_ko"] or r["summary_en"] or "",
            "country": r["primary_country"],
            "published_at": r["published_at"],
            "ai_score": r["ai_score"],
            "link": r["link"],
        })
    return results


def synthesize_answer(query: str, results: list[dict], *, model: str = "qwen3.6:27b", timeout: int = 120) -> str:
    """검색된 기사들을 근거로 로컬 LLM이 질문에 답변(출처 article_id 명시)."""
    context = "\n\n".join(
        f"[{r['article_id']}] ({r['published_at'] or '-'}, {r['country'] or '-'}) {r['title']}\n{r['summary']}"
        for r in results
    )
    system = (
        "당신은 KB금융그룹 글로벌 뉴스 데이터베이스 검색 어시스턴트다. "
        "아래 제공된 기사들만 근거로 질문에 답하라. 없는 내용은 지어내지 말고 "
        "'제공된 자료에 없음'이라고 밝혀라. 답변 끝에 참고한 기사 번호를 [id] 형식으로 표기하라."
    )
    user = f"질문: {query}\n\n[참고 기사 목록]\n{context}"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "think": False,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(config.OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("message", {}).get("content", "")


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser(description="뉴스DB 의미검색(RAG)")
    p.add_argument("query", help="자연어 질문")
    p.add_argument("--topk", type=int, help=f"반환 건수(기본 {config.RAG_SEARCH_TOPK})")
    p.add_argument("--model", help=f"임베딩 모델(기본 {config.RAG_EMBED_MODEL})")
    p.add_argument("--synthesize", action="store_true", help="검색 결과 기반 LLM 답변 생성까지 수행")
    p.add_argument("--llm-model", default="qwen3.6:27b", dest="llm_model", help="synthesize용 생성모델")
    args = p.parse_args()

    conn = db.open_conn()
    results = search(conn, args.query, model=args.model, topk=args.topk)
    if not results:
        print("검색 결과 없음(임베딩이 아직 없을 수 있음 — 먼저 rag_embed.py 실행 필요)")
        raise SystemExit(0)

    print(f"[검색결과 {len(results)}건]")
    for r in results:
        print(f"  [{r['score']:.3f}] id={r['article_id']} ({r['published_at'] or '-'}, {r['country'] or '-'}, "
              f"ai_score={r['ai_score']})  {r['title']}")
        if r["summary"]:
            print(f"           {r['summary'][:100]}")

    if args.synthesize:
        print("\n[LLM 답변 생성 중...]\n")
        answer = synthesize_answer(args.query, results, model=args.llm_model)
        print(answer)
