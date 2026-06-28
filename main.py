"""
glb-one-teams 파이프라인 CLI

수집(AI 없음):
  python main.py init              # DB 초기화 + sources.yaml 동기화
  python main.py fetch             # 피드 수집
  python main.py filter            # 키워드 필터 + 중복 탐지
  python main.py filter --refilter # 전체 기사 재필터링
  python main.py dedup             # 중복 탐지만 별도 실행
  python main.py run               # fetch → filter → dedup 한 번에
  python main.py report            # 매체 가용성 리포트
  python main.py list [--limit N]  # 최근 수집 기사 출력

AI 레이어(별도 — ANTHROPIC_API_KEY 필요, 미설정 시 즉시 안내 후 중단):
  python main.py prefilter         # LLM 1차 관문 (keep/drop)
  python main.py rank              # AI 분석 (score/summary/topics/kb_implication)
  python main.py brief             # 국가별 브리핑 생성
  python main.py ai                # prefilter → rank → brief 한 번에
  python main.py export            # DB → data/export/*.json (UI 데이터 계약)
"""
from __future__ import annotations

import argparse
import logging
import sys

import collector
import config
import db
import keyword_filter

REPORT_PATH = config.REPORT_PATH


# ---------------------------------------------------------------------------
# 수집 단계
# ---------------------------------------------------------------------------
def cmd_init(_args):
    conn = collector.init_db(config.DB_PATH, config.SCHEMA)
    collector.sync_sources(conn, config.SOURCES)
    n_sources = conn.execute("SELECT COUNT(*) AS c FROM media_sources").fetchone()["c"]
    n_feeds   = conn.execute("SELECT COUNT(*) AS c FROM media_source_feeds").fetchone()["c"]
    print(f"[init] db={config.DB_PATH}  sources={n_sources}  feeds={n_feeds}")


def cmd_fetch(_args):
    if not config.DB_PATH.exists():
        print("DB가 없습니다. 먼저 `python main.py init` 실행하세요.", file=sys.stderr)
        sys.exit(1)
    conn = db.open_conn()
    collector.sync_sources(conn, config.SOURCES)
    run_id = collector.run_fetch_all(conn)
    run = conn.execute("SELECT * FROM fetch_runs WHERE run_id = ?", (run_id,)).fetchone()
    print(f"[fetch] run_id={run_id}  feeds={run['feeds_total']} "
          f"(ok={run['feeds_ok']} fail={run['feeds_failed']})  "
          f"new={run['new_articles']} dup={run['dup_articles']}")


def cmd_filter(args):
    conn = db.open_conn()
    stats = keyword_filter.run_keyword_filter(conn, refilter_all=getattr(args, "refilter", False))
    t = stats["total"]
    if t == 0:
        print("[filter] 처리할 기사 없음")
        return
    p, r = stats["passed"], stats["rejected"]
    print(f"[filter] 처리={t:,}  통과={p:,}({p/t*100:.1f}%)  거부={r:,}({r/t*100:.1f}%)")


def cmd_dedup(args):
    conn = db.open_conn()
    keyword_filter.ensure_dedup_column(conn)
    stats = keyword_filter.run_dedup(conn, recheck=getattr(args, "recheck", False))
    c, d = stats["checked"], stats["duplicates"]
    if c == 0:
        print("[dedup] 처리할 기사 없음")
        return
    print(f"[dedup] 검사={c:,}  중복표시={d:,}({d/c*100:.1f}%)")


def cmd_run(_args):
    """fetch → filter → dedup 순서 실행 (수집 전용, AI 없음)."""
    print("=" * 50)
    print("[run] 수집 파이프라인 시작")
    print("=" * 50)

    conn = collector.init_db(config.DB_PATH, config.SCHEMA) if not config.DB_PATH.exists() else db.open_conn()
    collector.sync_sources(conn, config.SOURCES)

    print("\n▶ [1/3] 피드 수집...")
    run_id = collector.run_fetch_all(conn)
    run = conn.execute("SELECT * FROM fetch_runs WHERE run_id = ?", (run_id,)).fetchone()
    print(f"   feeds={run['feeds_total']} (ok={run['feeds_ok']} fail={run['feeds_failed']})  "
          f"new={run['new_articles']} dup={run['dup_articles']}")

    conn = db.open_conn()

    print("\n▶ [2/3] 키워드 필터...")
    stats = keyword_filter.run_keyword_filter(conn)
    if stats["total"] > 0:
        print(f"   처리={stats['total']:,}  통과={stats['passed']:,}  거부={stats['rejected']:,}")
    else:
        print("   처리할 기사 없음")

    print("\n▶ [3/3] 중복 제거...")
    keyword_filter.ensure_dedup_column(conn)
    stats = keyword_filter.run_dedup(conn)
    if stats["checked"] > 0:
        print(f"   검사={stats['checked']:,}  중복={stats['duplicates']:,}")
    else:
        print("   처리할 기사 없음")

    print("\n" + "=" * 50)
    print("[run] 완료")
    print("=" * 50)


def cmd_report(_args):
    conn = db.open_conn()
    REPORT_PATH.write_text(collector.build_availability_report(conn), encoding="utf-8")
    print(f"[report] wrote {REPORT_PATH}")


def cmd_list(args):
    conn = db.open_conn()
    rows = conn.execute("""
        SELECT m.media_name, a.title, a.published_at, a.filter_decision
        FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
        ORDER BY a.fetched_at DESC LIMIT ?
    """, (args.limit,)).fetchall()
    for r in rows:
        print(f"[{r['filter_decision']:8s}] [{r['media_name']}] {r['published_at'] or '-'}  {r['title'][:70]}")


# ---------------------------------------------------------------------------
# AI 단계 (지연 임포트 — anthropic 미설치 환경에서도 수집 명령은 동작)
# ---------------------------------------------------------------------------
def _ai_guard(fn, label):
    """AI 명령 공통 래퍼 — 키/패키지 누락을 친절히 안내."""
    try:
        return fn()
    except RuntimeError as e:
        print(f"[{label}] 중단: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_prefilter(_args):
    import llm_prefilter
    conn = db.open_conn()
    s = _ai_guard(lambda: llm_prefilter.run_prefilter(conn), "prefilter")
    print(f"[prefilter] 처리={s['total']}  keep={s['keep']}  drop={s['drop']}")


def cmd_rank(_args):
    import llm_ranker
    conn = db.open_conn()
    s = _ai_guard(lambda: llm_ranker.run_rank(conn), "rank")
    print(f"[rank] 처리={s['ranked']}  ACTIVE={s['active']}")


def cmd_brief(args):
    import briefing
    conn = db.open_conn()
    s = _ai_guard(
        lambda: briefing.run_briefing(conn, briefing_type=getattr(args, "type", "weekly")),
        "brief",
    )
    print(f"[brief] 국가={s['countries']}  작성={s['written']}")


def cmd_ai(_args):
    """prefilter → rank → brief 순서 실행."""
    import briefing
    import llm_prefilter
    import llm_ranker
    conn = db.open_conn()
    print("▶ [1/3] LLM 프리필터...")
    s1 = _ai_guard(lambda: llm_prefilter.run_prefilter(conn), "ai")
    print(f"   keep={s1['keep']} drop={s1['drop']}")
    print("▶ [2/3] AI 분석...")
    s2 = _ai_guard(lambda: llm_ranker.run_rank(conn), "ai")
    print(f"   ranked={s2['ranked']} ACTIVE={s2['active']}")
    print("▶ [3/3] 국가 브리핑...")
    s3 = _ai_guard(lambda: briefing.run_briefing(conn), "ai")
    print(f"   written={s3['written']}")


def cmd_export(_args):
    import export_json
    conn = db.open_conn()
    s = export_json.export_countries(conn)
    print(f"[export] countries={s['countries']}  articles={s['articles']}  → {s['path']}")


# ---------------------------------------------------------------------------
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="glb-one-teams 파이프라인")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init",   help="DB 초기화 및 sources.yaml 동기화")
    sub.add_parser("fetch",  help="전체 활성 피드 1회 수집")
    sub.add_parser("report", help="매체 가용성 리포트 생성")
    sub.add_parser("run",    help="fetch → filter → dedup 순서 실행")

    flt = sub.add_parser("filter", help="키워드 필터 실행")
    flt.add_argument("--refilter", action="store_true", help="전체 기사 재처리")

    ded = sub.add_parser("dedup", help="중복 기사 탐지")
    ded.add_argument("--recheck", action="store_true", help="전체 재탐지")

    lst = sub.add_parser("list", help="최근 수집 기사 출력")
    lst.add_argument("--limit", type=int, default=20)

    # AI 단계
    sub.add_parser("prefilter", help="LLM 1차 관문 (keep/drop)")
    sub.add_parser("rank",      help="AI 분석 (score/summary/topics/kb_implication)")
    brf = sub.add_parser("brief", help="국가별 브리핑 생성")
    brf.add_argument("--type", default="weekly", help="브리핑 유형 (weekly|daily)")
    sub.add_parser("ai",        help="prefilter → rank → brief 일괄")
    sub.add_parser("export",    help="DB → data/export/*.json (UI 데이터)")

    args = p.parse_args()
    {
        "init": cmd_init, "fetch": cmd_fetch, "filter": cmd_filter,
        "dedup": cmd_dedup, "run": cmd_run, "report": cmd_report, "list": cmd_list,
        "prefilter": cmd_prefilter, "rank": cmd_rank, "brief": cmd_brief,
        "ai": cmd_ai, "export": cmd_export,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
