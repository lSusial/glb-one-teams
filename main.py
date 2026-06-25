"""
glb-one-teams 수집 파이프라인 CLI

  python main.py init              # DB 초기화 + sources.yaml 동기화
  python main.py fetch             # 피드 수집
  python main.py filter            # 키워드 필터 + 중복 탐지
  python main.py filter --refilter # 전체 기사 재필터링
  python main.py dedup             # 중복 탐지만 별도 실행
  python main.py run               # fetch → filter → dedup 한 번에
  python main.py report            # 매체 가용성 리포트
  python main.py list [--limit N]  # 최근 수집 기사 출력
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import collector
import keyword_filter

ROOT      = Path(__file__).resolve().parent
DATA_DIR  = ROOT / "data"
DB_PATH   = DATA_DIR / "news.db"
SCHEMA    = ROOT / "schema.sql"
SOURCES   = ROOT / "sources.yaml"
REPORT_PATH = DATA_DIR / "availability_report.md"


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -8000")
    return conn


def cmd_init(_args):
    conn = collector.init_db(DB_PATH, SCHEMA)
    collector.sync_sources(conn, SOURCES)
    n_sources = conn.execute("SELECT COUNT(*) AS c FROM media_sources").fetchone()["c"]
    n_feeds   = conn.execute("SELECT COUNT(*) AS c FROM media_source_feeds").fetchone()["c"]
    print(f"[init] db={DB_PATH}  sources={n_sources}  feeds={n_feeds}")


def cmd_fetch(_args):
    if not DB_PATH.exists():
        print("DB가 없습니다. 먼저 `python main.py init` 실행하세요.", file=sys.stderr)
        sys.exit(1)
    conn = _open()
    collector.sync_sources(conn, SOURCES)
    run_id = collector.run_fetch_all(conn)
    run = conn.execute("SELECT * FROM fetch_runs WHERE run_id = ?", (run_id,)).fetchone()
    print(f"[fetch] run_id={run_id}  feeds={run['feeds_total']} "
          f"(ok={run['feeds_ok']} fail={run['feeds_failed']})  "
          f"new={run['new_articles']} dup={run['dup_articles']}")


def cmd_filter(args):
    conn = _open()
    refilter = getattr(args, "refilter", False)
    stats = keyword_filter.run_keyword_filter(conn, refilter_all=refilter)
    t = stats["total"]
    if t == 0:
        print("[filter] 처리할 기사 없음")
        return
    p, r = stats["passed"], stats["rejected"]
    print(f"[filter] 처리={t:,}  통과={p:,}({p/t*100:.1f}%)  거부={r:,}({r/t*100:.1f}%)")


def cmd_dedup(args):
    conn = _open()
    recheck = getattr(args, "recheck", False)
    keyword_filter.ensure_dedup_column(conn)
    stats = keyword_filter.run_dedup(conn, recheck=recheck)
    c, d = stats["checked"], stats["duplicates"]
    if c == 0:
        print("[dedup] 처리할 기사 없음")
        return
    print(f"[dedup] 검사={c:,}  중복표시={d:,}({d/c*100:.1f}%)")


def cmd_run(_args):
    """fetch → filter → dedup 순서 실행."""
    print("=" * 50)
    print("[run] 수집 파이프라인 시작")
    print("=" * 50)

    conn = collector.init_db(DB_PATH, SCHEMA) if not DB_PATH.exists() else _open()
    collector.sync_sources(conn, SOURCES)

    print("\n▶ [1/3] 피드 수집...")
    run_id = collector.run_fetch_all(conn)
    run = conn.execute("SELECT * FROM fetch_runs WHERE run_id = ?", (run_id,)).fetchone()
    print(f"   feeds={run['feeds_total']} (ok={run['feeds_ok']} fail={run['feeds_failed']})  "
          f"new={run['new_articles']} dup={run['dup_articles']}")

    conn = _open()

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
    conn = _open()
    text = collector.build_availability_report(conn)
    REPORT_PATH.write_text(text, encoding="utf-8")
    print(f"[report] wrote {REPORT_PATH}")


def cmd_list(args):
    conn = _open()
    rows = conn.execute("""
        SELECT m.media_name, a.title, a.published_at, a.filter_decision
        FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
        ORDER BY a.fetched_at DESC LIMIT ?
    """, (args.limit,)).fetchall()
    for r in rows:
        print(f"[{r['filter_decision']:8s}] [{r['media_name']}] {r['published_at'] or '-'}  {r['title'][:70]}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="glb-one-teams 수집 파이프라인")
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

    args = p.parse_args()
    {"init": cmd_init, "fetch": cmd_fetch, "filter": cmd_filter,
     "dedup": cmd_dedup, "run": cmd_run, "report": cmd_report,
     "list": cmd_list}[args.cmd](args)


if __name__ == "__main__":
    main()
