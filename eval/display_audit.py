"""표시 감사 — export 결과(화면에 실제 나가는 카드)의 신뢰성 문제를 자동 점검한다 (2026-09-21).

"화면에서 볼 때마다 하나씩 나온다"는 문제를 사람 눈 대신 코드로 훑기 위한 읽기 전용 리포트.
LLM 호출·DB 쓰기 없음. export 직후 돌린다:

  python3 eval/display_audit.py                    # data/export + data/news.db
  python3 eval/display_audit.py --export-dir X --db Y --stale-days 3 --json

점검 항목 (카드 = export JSON 안의 기사 항목)
  1 EMPTY_SUMMARY      요약(q/expanded)이 비어 있는 카드 — 모달에 '요약 없음'이 뜬다
  2 STALE              게시 후 N일 넘은 기사가 국가 탭에 노출
  3 PREVIEW_SHOWN      '결정 임박/ahead of…' 예고 기사가 결정 후에도 노출(2일 이상 경과)
  4 COUNTRY_MISMATCH   국가 탭의 카드가 그 나라를 다룬 기사가 아님(주제국가≠탭)
  5 NEAR_DUP_IN_TAB    같은 탭 안에서 같은 사건으로 보이는 카드 3건 이상 뭉침
  6 JUNK_TITLE         'Index of /', 404, 봇차단 등 쓰레기 제목
  7 UNRELATED_LINKS    모달 '관련 기사 링크'가 본 기사와 무관해 보임(요약 겹침 낮음)
  8 TITLE_SUMMARY_GAP  제목과 요약이 거의 안 겹침(요약 오류·엉뚱한 본문 의심)
  9 HIDDEN_NEWER_REP   AI 중복판정에서 더 새롭고 점수 같거나 높은 기사가 오래된 대표 밑에 숨음
 10 THIN_TABS          노출 0~1건인 국가 탭
 11 SELF_TITLE_MISMATCH 모달 '관련 기사 링크'의 자기참조(rl[0])가 카드 헤드라인과 다른 제목으로
                        표시됨 — url은 같은데 원문(raw) 제목을 써서 다른 기사처럼 보이는 버그
                        (2026-09-22 인도 탭 '러시아 제재법' 기사에서 실제 발견)
'점검 통과'가 '정확함'을 뜻하진 않는다 — 규칙으로 잡히는 결함만 센다.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import llm_dedup as L  # noqa: E402

JUNK_RE = re.compile(
    r"^index of /|wp-content|^404\b|not found|just a moment|access denied|attention required|"
    r"enable javascript|captcha|cloudflare|are you a robot|^subscribe|^sign in|^log in|page unavailable",
    re.I)


def walk(o, path=""):
    """export JSON 안의 카드({t, u} 를 가진 dict)를 (경로, 카드)로 순회."""
    if isinstance(o, dict):
        if "t" in o and "u" in o:
            yield path, o
            return
        for k, v in o.items():
            yield from walk(v, f"{path}/{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from walk(v, f"{path}[{i}]")


def load_cards(export_dir: Path):
    """[(file, where, cc_tab_or_None, card)] — countries.json은 국가 탭별로 cc를 붙인다."""
    out = []
    for name in ("countries", "pulse", "topics"):
        p = export_dir / f"{name}.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        if name == "countries":
            for c in d.get("countries", []):
                for a in c.get("articles", []):
                    out.append((name, f"국가탭 {c['cc']}", c["cc"], a))
            for sec in ("non_presence", "korean_fi", "personnel"):
                for a in (d.get(sec) or {}).get("articles", []):
                    out.append((name, sec, None, a))
        else:
            for path, card in walk(d):
                out.append((name, path.split("[")[0], card.get("cc"), card))
    return out


def snapshot(export_dir: Path) -> date:
    try:
        d = json.loads((export_dir / "countries.json").read_text(encoding="utf-8"))
        return datetime.strptime(d["snapshot_date"], "%Y-%m-%d").date()
    except Exception:
        return date.today()


def short(card, n=52):
    return f"{card.get('d', '')} {(card.get('t') or card.get('t_en') or '')[:n]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export-dir", default=str(config.EXPORT_DIR))
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--stale-days", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="요약 집계를 JSON 한 줄로도 출력")
    args = ap.parse_args()

    export_dir = Path(args.export_dir)
    cards = load_cards(export_dir)
    today = snapshot(export_dir)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    tabs = [(w, cc, a) for f, w, cc, a in cards if f == "countries" and w.startswith("국가탭")]

    issues: dict[str, list[str]] = {}

    def add(key, msg):
        issues.setdefault(key, []).append(msg)

    def db_row(link):
        return conn.execute(
            "SELECT article_id, primary_country, summary_en, title, published_at, ai_score "
            "FROM articles_raw WHERE link=?", (link,)).fetchone()

    # 1 EMPTY_SUMMARY  (모든 파일의 카드)
    seen = set()
    for f, w, cc, a in cards:
        if not (a.get("q") or a.get("q_en") or a.get("expanded_summary") or a.get("expanded_summary_en")):
            k = (a["u"], w)
            if k not in seen:
                seen.add(k)
                add("EMPTY_SUMMARY", f"[{f}:{w}] {short(a)} (score={a.get('score')})")

    # 2 STALE / 3 PREVIEW_SHOWN (국가 탭)
    for w, cc, a in tabs:
        try:
            age = (today - datetime.strptime(a["d"], "%Y-%m-%d").date()).days
        except Exception:
            continue
        if age >= args.stale_days:
            add("STALE", f"[{w}] {age}일 전 · {short(a)}")
        if age >= 2 and L.is_preview(a.get("t_en"), a.get("t")):
            add("PREVIEW_SHOWN", f"[{w}] 예고성 기사가 {age}일째 노출 · {short(a)}")

    # 4 COUNTRY_MISMATCH (국가 탭 vs 주제국가)
    for w, cc, a in tabs:
        r = db_row(a["u"])
        if r and r["primary_country"] and r["primary_country"] not in (cc,):
            tag = "GLOBAL" if r["primary_country"] == "GLOBAL" else r["primary_country"]
            add("COUNTRY_MISMATCH", f"[{w}] 주제국가={tag} · {short(a)}")

    # 5 NEAR_DUP_IN_TAB (같은 탭 안 근접중복 뭉침: 겹침 ≥ 0.3인 카드끼리 연결요소 3건+)
    by_tab: dict[str, list] = {}
    for w, cc, a in tabs:
        by_tab.setdefault(w, []).append(a)
    for w, arts in by_tab.items():
        toks = [L._tokens((a.get("t_en") or "") + " " + (a.get("q_en") or "")) for a in arts]
        parent = list(range(len(arts)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for i in range(len(arts)):
            for j in range(i + 1, len(arts)):
                if L.overlap(toks[i], toks[j]) >= 0.3:
                    parent[find(i)] = find(j)
        comps: dict[int, list] = {}
        for i in range(len(arts)):
            comps.setdefault(find(i), []).append(i)
        for comp in comps.values():
            if len(comp) >= 3:
                add("NEAR_DUP_IN_TAB", f"[{w}] 같은 사건 {len(comp)}건 · " +
                    " | ".join((arts[i].get("t_en") or arts[i].get("t") or "")[:34] for i in comp[:3]))

    # 6 JUNK_TITLE / 8 TITLE_SUMMARY_GAP / 7 UNRELATED_LINKS
    for f, w, cc, a in cards:
        title = a.get("t_en") or a.get("t") or ""
        if JUNK_RE.search(title.strip()) or JUNK_RE.search(a.get("t") or ""):
            add("JUNK_TITLE", f"[{f}:{w}] {short(a, 80)}")
        summ = a.get("q_en") or a.get("expanded_summary_en") or ""
        tt, st = L._tokens(title), L._tokens(summ)
        if len(tt) >= 3 and len(st) >= 5 and L.overlap(tt, st) < 0.1:
            add("TITLE_SUMMARY_GAP", f"[{f}:{w}] 제목↔요약 겹침 {L.overlap(tt, st):.2f} · {short(a)}")
        rl0 = (a.get("rl") or [None])[0]
        if rl0 and rl0.get("u") == a.get("u"):
            head_tk = L._tokens((a.get("t") or "") + " " + (a.get("t_en") or ""))
            self_tk = L._tokens(rl0.get("t") or "")
            if len(head_tk) >= 3 and len(self_tk) >= 3 and L.overlap(head_tk, self_tk) < 0.15:
                add("SELF_TITLE_MISMATCH",
                    f"[{f}:{w}] 카드 제목 '{short(a, 40)}' ↔ 관련링크 자기참조 '{(rl0.get('t') or '')[:40]}'")
    for w, cc, a in tabs:
        rl = a.get("rl") or []
        base = L._tokens(a.get("q_en") or a.get("t_en") or "")
        for x in rl[1:]:
            r = db_row(x["u"])
            if not r:
                continue
            ov = L.overlap(base, L._tokens((r["title"] or "") + " " + (r["summary_en"] or "")))
            if ov < 0.12:
                add("UNRELATED_LINKS", f"[{w}] {short(a, 34)} ↔ 관련: {(x.get('t') or '')[:40]} (겹침 {ov:.2f})")

    # 9 HIDDEN_NEWER_REP (AI 중복판정에서 더 새롭고 점수 같거나 높은 기사가 오래된 대표 밑에 숨음)
    for r in conn.execute(
            """SELECT c.article_id cid, c.title ct, c.published_at cp, c.ai_score cs,
                      r.article_id rid, r.title rt, r.published_at rp, r.ai_score rs
               FROM articles_raw c JOIN articles_raw r ON r.article_id = c.duplicate_of
               WHERE c.dup_by_ai = 1 AND r.duplicate_of IS NULL AND r.ai_score >= ?
                 AND r.published_at >= date(?, '-6 day')""",
            (config.AI_SCORE_ACTIVE_THRESHOLD, today.isoformat())):
        if (r["cp"] or "")[:10] > (r["rp"] or "")[:10] and (r["cs"] or 0) >= (r["rs"] or 0):
            add("HIDDEN_NEWER_REP", f"대표 {r['rp'][:10]} '{(r['rt'] or '')[:40]}' 밑에 더 새 {r['cp'][:10]} '{(r['ct'] or '')[:40]}'")

    # 10 THIN_TABS
    counts = {}
    for w, cc, a in tabs:
        counts[cc] = counts.get(cc, 0) + 1
    try:
        d = json.loads((export_dir / "countries.json").read_text(encoding="utf-8"))
        for c in d.get("countries", []):
            n = counts.get(c["cc"], 0)
            if n <= 1:
                add("THIN_TABS", f"{c['cc']} 노출 {n}건")
    except Exception:
        pass

    order = ["EMPTY_SUMMARY", "STALE", "PREVIEW_SHOWN", "COUNTRY_MISMATCH", "NEAR_DUP_IN_TAB",
             "JUNK_TITLE", "UNRELATED_LINKS", "TITLE_SUMMARY_GAP", "HIDDEN_NEWER_REP", "THIN_TABS",
             "SELF_TITLE_MISMATCH"]
    print(f"표시 감사 — 기준일 {today} · 카드 {len(cards)}장 (국가탭 {len(tabs)}장) · export={export_dir}")
    print("-" * 78)
    for k in order:
        v = issues.get(k, [])
        print(f"{'✔' if not v else '✖'} {k:<18} {len(v):>3}건")
    print("-" * 78)
    for k in order:
        v = issues.get(k, [])
        if not v:
            continue
        print(f"\n[{k}] {len(v)}건")
        for m in v[:12]:
            print("  -", m)
        if len(v) > 12:
            print(f"  … 외 {len(v) - 12}건")
    if args.json:
        print(json.dumps({k: len(issues.get(k, [])) for k in order}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
