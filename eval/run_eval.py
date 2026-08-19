"""
eval/run_eval.py — 프리필터 및 ai_score 루브릭 평가

사용법:
  # ── 프리필터 평가 ──────────────────────────────────────────────────────
  python eval/run_eval.py --mode prefilter                    # keep/drop vs 정답 비교
  python eval/run_eval.py --mode prefilter --sync             # 동기 호출 (디버깅)

  # ── 랭커 루브릭 평가 ───────────────────────────────────────────────────
  python eval/run_eval.py --mode ranker                       # 현재+개정 동시 채점 + 비교 리포트
  python eval/run_eval.py --mode ranker --rubric new          # 개정 루브릭만
  python eval/run_eval.py --mode ranker --results PATH        # 기존 결과로 리포트만
  python eval/run_eval.py --mode ranker --sync                # 동기 호출 (디버깅)
  python eval/run_eval.py --mode ranker --threshold 50        # ACTIVE 임계 변경

기본 eval 파일: eval/eval_set_v2.jsonl  (grade 0-3 확정 라벨)
  --eval-file eval/eval_set.jsonl  로 구버전(이진 label) 사용 가능

출력:
  --mode prefilter  → eval/results_prefilter_YYYYMMDD.json
  --mode ranker     → eval/results_ranker_YYYYMMDD.jsonl

측정 지표 (프리필터):
  - TP/FP/FN/TN 혼동행렬 / Precision / Recall / F1
  - positive 기준: grade≥2 (v2) 또는 label=1 (v1)

측정 지표 (랭커):
  - ai_score 분포 (0-24/25-49/50-74/75-100)
  - grade별 평균 점수 + 분포 교차표
  - Recall@threshold / Precision@threshold / F1  (positive = grade≥2)
  - grade 3건 ai_score 분포 (핵심 기사 식별률)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os

def _load_dotenv(path: Path | None = None) -> None:
    path = path or Path(__file__).parent.parent / ".env"
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_dotenv()

from llm_provider import get_provider

EVAL_FILE = Path(__file__).parent / "eval_set_v2.jsonl"   # 기본: v2 (grade 0-3)
RESULTS_DIR = Path(__file__).parent

# ── 공통 시스템 프롬프트 앞부분 ──────────────────────────────────────────────
_SYS_PREFIX = (
    "You are a global intelligence analyst at KB Financial Group. "
    "KB has branches/subsidiaries in: GB, US, HK, CN, JP, SG, IN, VN, MM, ID, KH.\n"
    "Analyze ONE overseas news article and output ONLY this JSON (nothing else):\n"
    '{"ai_score": <integer 0-100>}\n\n'
)

# ── 루브릭 A: 현재 ──────────────────────────────────────────────────────────
RUBRIC_OLD = (
    "ai_score guide: direct impact on KB branch lending/risk/funding = 80+, "
    "indirect/background = 40-60, weak = under 40."
)

# ── 루브릭 B: 개정 (4계층) ──────────────────────────────────────────────────
RUBRIC_NEW = """\
ai_score rubric — assign the highest tier that applies:

75-100  DIRECT · IMMEDIATE
  KB branch/subsidiary directly affected today.
  Examples: host-country central bank rate decision, capital controls imposed,
  KB entity under regulatory action/sanction, sovereign rating downgrade
  in a KB-presence market, FX convertibility crisis.

50-74   CONTEXTUAL · IMPORTANT
  A KB banker at this hub should read this within the day.
  Examples: major currency move in a KB country (VND/IDR/MMK/CNY/INR…),
  Fed/BoJ/ECB rate path shift, oil shock affecting local inflation,
  geopolitical event in KB market (coup, sanctions risk, capital-flow
  restriction), banking-sector M&A or stress in KB geography,
  sovereign debt stress, significant trade/tariff change.

25-49   BACKGROUND · CONTEXT
  Useful context; no near-term KB action needed.
  Examples: global fintech/ESG trends, developed-market macro that only
  indirectly reaches KB geographies, general industry research.

0-24    NOISE / UNRELATED
  Sports, entertainment, stock tips for unrelated sectors, crime gossip,
  local events with no macro or financial relevance to KB overseas operations."""


def load_eval() -> list[dict]:
    items = []
    with open(EVAL_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _pos(item: dict) -> int:
    """확정 라벨에서 이진 positive(1) / negative(0) 반환.

    v2 (grade 필드 있음): grade >= 2 → 1 (ACTIVE-worthy)
    v1 (label 필드만)   : label == 1 → 1
    """
    if "grade" in item:
        return 1 if item["grade"] >= 2 else 0
    return int(item.get("label", 0) == 1)


def _grade_of(item: dict) -> int | None:
    """grade 필드 반환. v1 items는 label→grade 근사 변환."""
    if "grade" in item:
        return item["grade"]
    lbl = item.get("label")
    if lbl == 1:
        return 2   # v1 keep → CONTEXTUAL 근사
    if lbl == 0:
        return 0   # v1 drop → NOISE 근사
    return None


# ────────────────────────────────────────────────────────────────────────────
# 프리필터 평가 (--mode prefilter)
# ────────────────────────────────────────────────────────────────────────────

from llm_prefilter import _SYSTEM as _PREFILTER_SYSTEM  # noqa: E402


def run_prefilter_eval(items: list[dict], use_batch: bool) -> list[bool | None]:
    """각 eval 항목에 llm_prefilter._SYSTEM 프롬프트를 적용해 keep/drop 예측."""
    provider = get_provider("fast", use_batch=None if use_batch else False)
    reqs = []
    for i, e in enumerate(items):
        user = (
            f"[국가:{e.get('cc', 'GLOBAL')}] 제목: {e.get('title', '')}\n"
            f"요약: {(e.get('summary') or '')[:600]}"
        )
        reqs.append((str(i), _PREFILTER_SYSTEM, user, 200))

    results = provider.complete_json_batch(reqs)
    preds: list[bool | None] = []
    for i in range(len(items)):
        data = results.get(str(i)) or {}
        keep = data.get("keep")
        if keep is None:
            preds.append(None)  # 파싱 실패 → 보수적 keep
        else:
            preds.append(bool(keep))
    return preds


def report_prefilter(items: list[dict], preds: list[bool | None]) -> dict:
    """혼동행렬 + Precision/Recall/F1 출력 및 반환.

    positive 기준: grade≥2 (v2 eval_set) 또는 label=1 (v1 eval_set).
    """
    tp = fp = fn = tn = 0
    failed = 0
    for e, p in zip(items, preds):
        lbl = _pos(e)
        if p is None:
            failed += 1
            p = True  # 보수적 keep
        if lbl == 1 and p:
            tp += 1
        elif lbl == 0 and p:
            fp += 1
        elif lbl == 1 and not p:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1        = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    n1 = sum(_pos(e) == 1 for e in items)
    n0 = sum(_pos(e) == 0 for e in items)
    pos_label = "grade≥2" if "grade" in items[0] else "label=1"
    neg_label = "grade≤1" if "grade" in items[0] else "label=0"

    print(f"\n{'='*62}")
    print(f"  프리필터 평가  — {len(items)}건  "
          f"(pos/{pos_label}={n1} / neg/{neg_label}={n0})")
    if failed:
        print(f"  ⚠ 응답 파싱 실패 {failed}건 → 보수적 keep 처리")
    print(f"{'='*62}")
    print(f"\n  혼동행렬")
    print(f"  {'':16s}  예측 keep  예측 drop")
    print(f"  pos ({pos_label})    TP={tp:3d}      FN={fn:3d}")
    print(f"  neg ({neg_label})    FP={fp:3d}      TN={tn:3d}")
    print(f"\n  Precision : {precision:.3f}  ({tp}/{tp+fp})")
    print(f"  Recall    : {recall:.3f}  ({tp}/{tp+fn})")
    print(f"  F1        : {f1:.3f}")

    fp_items = [(e, p) for e, p in zip(items, preds) if _pos(e) == 0 and (p is None or p)]
    if fp_items:
        print(f"\n── 오탐(FP) — neg인데 keep 예측 ({len(fp_items)}건) ──")
        for e, _ in fp_items:
            g = e.get("grade", e.get("label", "?"))
            print(f"  [g={g}][{e.get('cc'):6s}] {e.get('title','')[:58]}")

    fn_items = [(e, p) for e, p in zip(items, preds) if _pos(e) == 1 and p is False]
    if fn_items:
        print(f"\n── 누락(FN) — pos인데 drop 예측 ({len(fn_items)}건) ──")
        for e, _ in fn_items:
            g = e.get("grade", e.get("label", "?"))
            print(f"  [g={g}][{e.get('cc'):6s}] {e.get('title','')[:58]}")

    return dict(tp=tp, fp=fp, fn=fn, tn=tn, precision=round(precision, 4),
                recall=round(recall, 4), f1=round(f1, 4), parse_failed=failed)


def save_prefilter_results(items: list[dict], preds: list[bool | None], metrics: dict) -> Path:
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = RESULTS_DIR / f"results_prefilter_{date}.json"
    records = []
    for e, p in zip(items, preds):
        records.append({
            "id": e.get("id"), "cc": e.get("cc"), "title": e.get("title"),
            "grade": e.get("grade"), "label": e.get("label"),  # v2: grade, v1: label
            "pos": _pos(e), "predicted_keep": p,
        })
    out = {"date": date, "mode": "prefilter", "eval_file": EVAL_FILE.name,
           "metrics": metrics, "items": records}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장: {path}")
    return path


# ────────────────────────────────────────────────────────────────────────────
# 랭커 루브릭 평가 (기존 코드)
# ────────────────────────────────────────────────────────────────────────────

def build_requests(items: list[dict], rubric: str) -> list[tuple]:
    system = _SYS_PREFIX + rubric
    reqs = []
    for i, e in enumerate(items):
        user = (
            f"[cc: {e.get('cc', 'GLOBAL')}]\n"
            f"title: {e.get('title', '')}\n"
            f"summary: {(e.get('summary') or '')[:600]}"
        )
        reqs.append((str(i), system, user, 100))
    return reqs


def run_scoring(items: list[dict], rubric: str, use_batch: bool) -> list[int | None]:
    provider = get_provider("fast", use_batch=None if use_batch else False)
    reqs = build_requests(items, rubric)
    results = provider.complete_json_batch(reqs)
    scores = []
    for i in range(len(items)):
        data = results.get(str(i)) or {}
        try:
            s = int(data.get("ai_score"))
            scores.append(max(0, min(100, s)))
        except (TypeError, ValueError):
            scores.append(None)
    return scores


def bucket(score: int | None) -> str:
    if score is None:
        return "N/A"
    if score <= 24:
        return "0-24"
    if score <= 49:
        return "25-49"
    if score <= 74:
        return "50-74"
    return "75-100"


def report(items: list[dict], scores_old: list, scores_new: list, threshold: int = 55):
    """랭커 평가 리포트.

    정답지: grade≥2 → positive (v2), label=1 → positive (v1 폴백).
    """
    has_v2 = "grade" in items[0] if items else False
    n_pos = sum(_pos(e) == 1 for e in items)
    n_neg = sum(_pos(e) == 0 for e in items)
    pos_label = "grade≥2" if has_v2 else "label=1"

    print(f"\n{'='*66}")
    print(f"  랭커 eval  — {len(items)}건  "
          f"(pos/{pos_label}={n_pos} / neg={n_neg})  ACTIVE임계={threshold}")
    if has_v2:
        from collections import Counter
        gd = Counter(_grade_of(e) for e in items)
        print(f"  grade분포: 3={gd.get(3,0)}건  2={gd.get(2,0)}건  "
              f"1={gd.get(1,0)}건  0={gd.get(0,0)}건")
    print(f"{'='*66}")

    pairs: list[tuple[str, list]] = []
    if any(s is not None for s in scores_old):
        pairs.append(("현재 루브릭", scores_old))
    if any(s is not None for s in scores_new):
        pairs.append(("개정 루브릭", scores_new))

    for tag, scores in pairs:
        valid = [s for s in scores if s is not None]
        if not valid:
            continue
        print(f"\n── {tag} ──")

        # 1. 전체 점수 분포
        for b in ["0-24", "25-49", "50-74", "75-100"]:
            n = sum(1 for s in valid if bucket(s) == b)
            pct = n / len(valid) * 100
            bar = "█" * int(pct / 3)
            print(f"  {b:7s}: {n:3d}건 ({pct:5.1f}%) {bar}")

        # 2. grade별 평균 점수 (v2) 또는 label별 (v1)
        if has_v2:
            print(f"\n  grade별 평균 ai_score:")
            from collections import defaultdict
            by_grade: dict[int, list] = defaultdict(list)
            for e, s in zip(items, scores):
                g = _grade_of(e)
                if g is not None and s is not None:
                    by_grade[g].append(s)
            for g in [3, 2, 1, 0]:
                label_g = {3:"핵심", 2:"중요", 1:"참고", 0:"무관"}[g]
                lst = by_grade.get(g, [])
                avg = sum(lst)/len(lst) if lst else 0
                print(f"    grade {g} {label_g:4s}: {avg:5.1f}pt  (n={len(lst)})")
        else:
            l1 = [s for e, s in zip(items, scores) if _pos(e) == 1 and s is not None]
            l0 = [s for e, s in zip(items, scores) if _pos(e) == 0 and s is not None]
            avg1 = sum(l1)/len(l1) if l1 else 0
            avg0 = sum(l0)/len(l0) if l0 else 0
            print(f"  pos 평균: {avg1:.1f}pt  neg 평균: {avg0:.1f}pt  격차: {avg1-avg0:+.1f}pt")

        # 3. Precision / Recall / F1  (positive = grade≥2 or label=1)
        l_pos = [s for e, s in zip(items, scores) if _pos(e) == 1 and s is not None]
        tp = sum(1 for e, s in zip(items, scores)
                 if _pos(e) == 1 and s is not None and s >= threshold)
        fp = sum(1 for e, s in zip(items, scores)
                 if _pos(e) == 0 and s is not None and s >= threshold)
        recall    = tp / len(l_pos) if l_pos else 0
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        f1        = 2*precision*recall / (precision+recall) if (precision+recall) > 0 else 0
        print(f"\n  Recall@{threshold}: {recall:.3f}  Precision@{threshold}: {precision:.3f}"
              f"  F1@{threshold}: {f1:.3f}  (TP={tp} FP={fp})")

        # 4. grade 교차표 (v2 전용): grade × bucket
        if has_v2:
            print(f"\n  grade × ai_score 교차표:")
            print(f"  {'grade':6s}  {'0-24':>6s}  {'25-49':>6s}  {'50-74':>6s}  {'75-100':>7s}  {'avg':>6s}")
            from collections import defaultdict
            by_g: dict[int, list] = defaultdict(list)
            for e, s in zip(items, scores):
                if s is not None:
                    by_g[_grade_of(e) or 0].append(s)
            for g in [3, 2, 1, 0]:
                lst = by_g.get(g, [])
                if not lst:
                    continue
                b0  = sum(1 for s in lst if s <= 24)
                b25 = sum(1 for s in lst if 25 <= s <= 49)
                b50 = sum(1 for s in lst if 50 <= s <= 74)
                b75 = sum(1 for s in lst if s >= 75)
                avg = sum(lst)/len(lst)
                print(f"  grade {g}:  {b0:6d}  {b25:6d}  {b50:6d}  {b75:7d}  {avg:6.1f}")

    # 5. 개별 기사 변화 (both 모드 + pos 기사만)
    if any(s is not None for s in scores_old) and any(s is not None for s in scores_new):
        print(f"\n── pos 기사 점수 변화 (현재→개정, grade 높은 순) ──")
        triples = [(e, so, sn) for e, so, sn in zip(items, scores_old, scores_new)
                   if _pos(e) == 1]
        triples.sort(key=lambda t: -(_grade_of(t[0]) or 0) * 1000 - (t[2] or 0))
        for e, so, sn in triples:
            arrow = "↑" if (sn or 0) > (so or 0) else ("↓" if (sn or 0) < (so or 0) else "=")
            hit_old = "✓" if (so or 0) >= threshold else "✗"
            hit_new = "✓" if (sn or 0) >= threshold else "✗"
            g = _grade_of(e)
            print(f"  g{g} {so or '?':>3}→{sn or '?':>3} {arrow}  {hit_old}→{hit_new}"
                  f"  [{e.get('cc'):6s}] {e.get('title','')[:48]}")

        fp_items = [(e, so, sn) for e, so, sn in zip(items, scores_old, scores_new)
                    if _pos(e) == 0 and (sn or 0) >= threshold]
        print(f"\n── neg 기사 중 {threshold}점 이상 오탐 ({len(fp_items)}건) ──")
        for e, so, sn in fp_items:
            g = _grade_of(e)
            print(f"  g{g} {so or '?':>3}→{sn or '?':>3}  [{e.get('cc'):6s}] {e.get('title','')[:53]}")
        if not fp_items:
            print("  없음")


def save_results(items, scores_old, scores_new) -> Path:
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = RESULTS_DIR / f"results_ranker_{date}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for e, so, sn in zip(items, scores_old, scores_new):
            f.write(json.dumps({
                "id":        e.get("id"),
                "cc":        e.get("cc"),
                "title":     e.get("title"),
                "grade":     e.get("grade"),      # v2 확정 라벨
                "label":     e.get("label"),      # v1 폴백
                "pos":       _pos(e),
                "score_old": so,
                "score_new": sn,
            }, ensure_ascii=False) + "\n")
    print(f"\n결과 저장: {path}")
    return path


def main():
    ap = argparse.ArgumentParser(description="eval 실행: 프리필터 또는 ai_score 루브릭 평가")
    ap.add_argument("--mode", choices=["prefilter", "ranker"], default="ranker",
                    help="평가 모드: prefilter(keep/drop) 또는 ranker(ai_score 분포) (기본: ranker)")
    ap.add_argument("--rubric", choices=["old", "new", "both"], default="both",
                    help="[ranker] 채점할 루브릭 선택 (기본: both)")
    ap.add_argument("--results", metavar="PATH",
                    help="[ranker] 기존 결과 JSONL 경로 — 재채점 없이 리포트만 출력")
    ap.add_argument("--eval-file", metavar="PATH", default=None,
                    help="eval_set JSONL 경로 (기본: eval/eval_set_v2.jsonl)")
    ap.add_argument("--sync", action="store_true",
                    help="배치 API 대신 동기 호출 (디버깅용, 속도 느림)")
    ap.add_argument("--threshold", type=int, default=55,
                    help="[ranker] ACTIVE 판정 임계값 (기본: 55)")
    args = ap.parse_args()

    global EVAL_FILE
    if args.eval_file:
        EVAL_FILE = Path(args.eval_file)
    items = load_eval()
    print(f"eval_set 로드: {len(items)}건  ({EVAL_FILE.name})")

    # ── 프리필터 모드 ──────────────────────────────────────────────────────
    if args.mode == "prefilter":
        use_batch = not args.sync
        print(f"\n[프리필터 eval] {len(items)}건 판정 중 ({'동기' if args.sync else '배치'})...")
        preds = run_prefilter_eval(items, use_batch)
        ok = sum(p is not None for p in preds)
        print(f"  완료: 성공={ok}건  파싱실패={len(items)-ok}건")
        metrics = report_prefilter(items, preds)
        save_prefilter_results(items, preds, metrics)
        return

    # ── 랭커 모드 (기본) ──────────────────────────────────────────────────
    # 기존 결과 파일로 리포트만
    if args.results:
        rows = []
        with open(args.results, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
        scores_old = [r.get("score_old") for r in rows]
        scores_new = [r.get("score_new") for r in rows]
        report(items, scores_old, scores_new, threshold=args.threshold)
        return

    use_batch = not args.sync
    scores_old: list[int | None] = [None] * len(items)
    scores_new: list[int | None] = [None] * len(items)

    if args.rubric in ("old", "both"):
        print(f"\n[현재 루브릭] {len(items)}건 채점 중...")
        scores_old = run_scoring(items, RUBRIC_OLD, use_batch)
        ok = sum(s is not None for s in scores_old)
        print(f"  완료: 성공={ok}건  실패={len(items)-ok}건")

    if args.rubric in ("new", "both"):
        print(f"\n[개정 루브릭] {len(items)}건 채점 중...")
        scores_new = run_scoring(items, RUBRIC_NEW, use_batch)
        ok = sum(s is not None for s in scores_new)
        print(f"  완료: 성공={ok}건  실패={len(items)-ok}건")

    report(items, scores_old, scores_new, threshold=args.threshold)

    if args.rubric == "both":
        save_results(items, scores_old, scores_new)


if __name__ == "__main__":
    main()
