"""AI 중복판정(llm_dedup) 가드·대표선정 평가 (2026-09-21).

`eval/dedup_pairs_labeled.json` — 저장된 dup_by_ai=1 쌍 70건을 사람이 읽고 붙인 정답
(label=1 같은 사건 / 0 별개 사건, unsure=경계)에 대해, 겹침 가드(`llm_dedup.split_by_overlap`)가
임계값별로 오묶음을 얼마나 풀고 진짜 중복은 얼마나 남기는지 잰다. LLM 호출 없음(읽기 전용).

  python3 eval/eval_dedup_guard.py                 # data/news.db, 기본 임계값 표
  python3 eval/eval_dedup_guard.py --db ~/x.db --thresholds 0.2,0.25,0.3
  python3 eval/eval_dedup_guard.py --live --days 8   # 새 프롬프트+가드를 실제 LLM으로 돌려 평가
                                                     # (DB는 안 바꾼다. 맥에서 실행, ANTHROPIC_API_KEY 필요, 소액)
  python3 eval/eval_dedup_guard.py --labeled-live    # 라벨 70쌍만 LLM 판정(가장 저렴한 회귀 평가)

읽는 법
  · precision = 가드 후에도 묶여 있는 쌍 중 진짜 같은 사건 비율 (높을수록 기사가 덜 사라짐)
  · recall    = 진짜 같은 사건 쌍 중 계속 묶여 있는 비율 (낮을수록 중복이 화면에 더 남음)
  · 오묶음(FP) 비용 > 놓친 중복 비용이므로, precision을 우선하되 recall이 너무 깎이지 않는 지점을 고른다.
  · 표본은 겹침도 십분위 균등추출이라 '전체 443쌍의 오묶음률'의 근사치(baseline precision)로도 읽는다.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import llm_dedup as L  # noqa: E402


def load_groups(conn):
    """저장된 AI 그룹 {rep_id: [rep, member, ...]} + 메타."""
    rows = conn.execute(
        """SELECT c.article_id cid, c.duplicate_of rid FROM articles_raw c
           WHERE c.dup_by_ai = 1 AND c.duplicate_of IS NOT NULL""").fetchall()
    groups: dict[int, list[int]] = {}
    for r in rows:
        groups.setdefault(r["rid"], []).append(r["cid"])
    ids = set(groups) | {c for ch in groups.values() for c in ch}
    meta = {}
    for aid in ids:
        a = conn.execute(
            "SELECT article_id, title, summary_en, ai_score, published_at "
            "FROM articles_raw WHERE article_id=?", (aid,)).fetchone()
        if not a:
            continue
        meta[aid] = {"score": a["ai_score"], "pub": a["published_at"] or "",
                     "titles": [a["title"]], "media_cc": None,
                     "tok": L._tokens((a["title"] or "") + " " + (a["summary_en"] or ""))}
    return {r: [r] + ch for r, ch in groups.items() if r in meta}, meta


def live_eval(conn, labels, days, country=None) -> None:
    """현행 프롬프트+가드+대표선정을 실제 LLM으로 돌려(dry_run) 라벨 쌍의 묶임을 채점한다."""
    import os
    env = ROOT / ".env"
    if env.exists() and not os.environ.get("ANTHROPIC_API_KEY"):
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip("\"'")
    res = L.run_dedup(conn, days=days, use_batch=False, only_cc=country, dry_run=True)
    together: set[frozenset] = set()
    for groups in res.get("groups", {}).values():
        for g in groups:
            for i, a in enumerate(g):
                for b in g[i + 1:]:
                    together.add(frozenset((a, b)))
    kept = [p for p in labels if frozenset((p["rep"], p["child"])) in together]
    tp = sum(1 for p in kept if p["label"] == 1)
    pos = sum(1 for p in labels if p["label"] == 1)
    print(f"[live] 국가={res['countries']} 검사={res['reviewed']} 실패={res['failed']} "
          f"가드해제={res['released']} 마킹(예정)={res['marked']}")
    print(f"[live] 라벨 {len(labels)}쌍 중 여전히 묶임 {len(kept)} — 같은사건 {tp}/{pos}(recall {tp/pos:.2f}), "
          f"별개 {len(kept) - tp}/{len(labels) - pos} 남음 → precision {tp/max(1,len(kept)):.2f}")
    print("        (기존 저장분 baseline precision 0.56, 가드만 0.25 적용 시 0.77/recall 0.95)")
    if country:
        for group in res.get("groups", {}).get(country, []):
            print(f"  {country} 그룹 {group}")
            for aid in group:
                row = conn.execute("SELECT COALESCE(title_en,title) FROM articles_raw WHERE article_id=?",
                                   (aid,)).fetchone()
                print(f"    {aid}: {(row[0] if row else '')[:100]}")


def labeled_live_eval(conn, labels) -> None:
    """사람이 라벨링한 쌍만 현행 pair 프롬프트로 판정해 저비용으로 회귀 평가한다."""
    import os
    from llm_provider import get_provider
    env = ROOT / ".env"
    if env.exists() and not os.environ.get("ANTHROPIC_API_KEY"):
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip("\"'")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles_raw)")}
    title_en_sql = "title_en" if "title_en" in cols else "NULL"
    ids = {int(p[k]) for p in labels for k in ("rep", "child")}
    meta = {}
    for aid in ids:
        a = conn.execute(
            f"SELECT title, {title_en_sql} AS title_en, published_at FROM articles_raw WHERE article_id=?",
            (aid,),
        ).fetchone()
        if not a:
            continue
        title = a["title_en"] or a["title"] or ""
        toks = L._tokens(" ".join(x or "" for x in (a["title_en"], a["title"])))
        meta[aid] = {"display_title": title, "pub": a["published_at"] or "",
                     "tok": toks, "title_tok": L._tokens(title)}
    pairs = [(int(p["rep"]), int(p["child"]), 0.0) for p in labels
             if int(p["rep"]) in meta and int(p["child"]) in meta]
    chunks = L._pair_chunks("LABEL", pairs, meta)
    requests = [(cid, L._PAIR_SYSTEM, user, max(512, min(4096, len(lookup) * 80)))
                for cid, lookup, user in chunks]
    results = get_provider("fast", use_batch=False).complete_json_batch(requests, temperature=0)
    raw_kept = set()
    kept = set()
    failed = 0
    for cid, lookup, _user in chunks:
        same = L._valid_same_pairs(results.get(cid), lookup)
        if same is None:
            failed += len(lookup)
            continue
        raw_kept.update(tuple(sorted(pair)) for pair in same)
        kept.update(tuple(sorted(pair)) for pair in same if L.pair_passes_guard(*pair, meta))
    scored = [p for p in labels if int(p["rep"]) in meta and int(p["child"]) in meta]
    tp = sum(1 for p in scored if p["label"] == 1 and tuple(sorted((p["rep"], p["child"]))) in kept)
    fp = sum(1 for p in scored if p["label"] == 0 and tuple(sorted((p["rep"], p["child"]))) in kept)
    pos = sum(1 for p in scored if p["label"] == 1)
    neg = len(scored) - pos
    raw_tp = sum(1 for p in scored if p["label"] == 1 and tuple(sorted((p["rep"], p["child"]))) in raw_kept)
    raw_fp = sum(1 for p in scored if p["label"] == 0 and tuple(sorted((p["rep"], p["child"]))) in raw_kept)
    print(f"[labeled-live] 요청={len(requests)} 실패쌍={failed} "
          f"LLM SAME={raw_tp + raw_fp}(TP {raw_tp}/FP {raw_fp}) → 가드 후={tp + fp}")
    print(f"[labeled-live] 같은사건 {tp}/{pos} (recall {tp/max(1,pos):.2f}), "
          f"별개 오병합 {fp}/{neg}, precision {tp/max(1,tp+fp):.2f}")
    misses = [p for p in scored if p["label"] == 1
              and tuple(sorted((p["rep"], p["child"]))) not in kept]
    false_positives = [p for p in scored if p["label"] == 0
                       and tuple(sorted((p["rep"], p["child"]))) in kept]
    for label, items in (("놓침", misses[:12]), ("오병합", false_positives[:12])):
        for p in items:
            reason = "가드" if tuple(sorted((p["rep"], p["child"]))) in raw_kept else "LLM"
            print(f"  {label}({reason}): {meta[p['rep']]['display_title'][:70]} | "
                  f"{meta[p['child']]['display_title'][:70]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--thresholds", default="0.08,0.12,0.16,0.2,0.25,0.3,0.35")
    ap.add_argument("--live", action="store_true", help="LLM을 실제 호출해 현행 프롬프트+가드 전체를 평가(DB 무변경)")
    ap.add_argument("--labeled-live", action="store_true", help="라벨 70쌍만 현행 pair 프롬프트로 실제 판정")
    ap.add_argument("--days", type=int, default=8, help="--live: 최근 N일 후보 (라벨 쌍이 다 들어오게 8 권장)")
    ap.add_argument("--country", help="--live 대상 국가 코드(예: JP)")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    labels = json.loads((ROOT / "eval" / "dedup_pairs_labeled.json").read_text(encoding="utf-8"))["pairs"]
    if args.labeled_live:
        return labeled_live_eval(conn, labels)
    if args.live:
        return live_eval(conn, labels, args.days, args.country)
    groups, meta = load_groups(conn)
    n_children = sum(len(m) - 1 for m in groups.values())
    n_pos = sum(1 for p in labels if p["label"] == 1)
    n_neg = len(labels) - n_pos
    print(f"저장된 AI 그룹 {len(groups)}개 · 묶인 자식 {n_children}건 · 라벨 {len(labels)}쌍(같은사건 {n_pos} / 별개 {n_neg})")
    print(f"현재(가드 없음) 표본 precision = {n_pos}/{len(labels)} = {n_pos/len(labels):.2f}  "
          f"→ 저장분의 오묶음률 추정 약 {n_neg/len(labels):.0%}\n")
    print(f"{'임계':>5} | {'풀린 자식':>9} | {'별개 제거':>9} | {'같은사건 유지':>13} | {'precision':>9} | {'recall':>6} | (sure-only P/R)")

    for t in [float(x) for x in args.thresholds.split(",")]:
        same: dict[tuple[int, int], bool] = {}
        released = 0
        for rep, members in groups.items():
            comps = L.split_by_overlap(members, meta, t)
            comp_of = {m: i for i, c in enumerate(comps) for m in c}
            for m in members:
                if m != rep and comp_of[m] != comp_of[rep]:
                    released += 1
                if m != rep:
                    same[(rep, m)] = comp_of[m] == comp_of[rep]

        def score(pairs):
            kept = [p for p in pairs if same.get((p["rep"], p["child"]), True)]
            tp = sum(1 for p in kept if p["label"] == 1)
            pos = sum(1 for p in pairs if p["label"] == 1)
            neg = len(pairs) - pos
            fp = len(kept) - tp
            prec = tp / len(kept) if kept else 1.0
            rec = tp / pos if pos else 1.0
            return tp, fp, neg, prec, rec

        tp, fp, neg, prec, rec = score(labels)
        s_tp, s_fp, s_neg, s_prec, s_rec = score([p for p in labels if not p["unsure"]])
        print(f"{t:>5.2f} | {released:>4}/{n_children:<4} | {neg - fp:>4}/{neg:<4} | {tp:>6}/{n_pos:<6} | "
              f"{prec:>9.2f} | {rec:>6.2f} | {s_prec:.2f}/{s_rec:.2f}")


if __name__ == "__main__":
    main()
