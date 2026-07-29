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
  python main.py prefilter [--days N]  # LLM 1차 관문 (keep/drop)
  python main.py rank [--days N]       # AI 분석[영어] (score/summary_en/topics/kb_implication_en)
  python main.py translate [--days N]  # 영어 기준본 → 한국어 번역 (표시분만, 저비용)
  python main.py brief                 # 국가별 브리핑 생성
  python main.py ai [--days N]         # prefilter → rank → translate → brief 한 번에
  python main.py export            # DB → data/export/countries.json (ACTIVE)
  python main.py export --passed   # AI 없이 필터 통과 기사로 export (Phase 1)
  python main.py admin             # 관리자 페이지 생성 (data/export/admin.html)
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


def _batch_flag(args):
    """--sync 지정 시 False(동기), 아니면 None(config 기본=배치·50%할인)."""
    return False if getattr(args, "sync", False) else None


def cmd_prefilter(args):
    import llm_prefilter
    conn = db.open_conn()
    ub = _batch_flag(args)
    s = _ai_guard(lambda: llm_prefilter.run_prefilter(conn, days=getattr(args, "days", None), use_batch=ub), "prefilter")
    print(f"[prefilter] 처리={s['total']}  keep={s['keep']}  drop={s['drop']}")


def cmd_fulltext(args):
    import fulltext
    conn = db.open_conn()
    s = _ai_guard(lambda: fulltext.run_fulltext(conn, days=getattr(args, "days", None)), "fulltext")
    print(f"[fulltext] 대상={s['total']}  본문={s['extracted']}  URL해소={s['resolved']}  실패={s['failed']}")


def cmd_rank(args):
    import llm_ranker
    conn = db.open_conn()
    ub = _batch_flag(args)
    s = _ai_guard(lambda: llm_ranker.run_rank(conn, days=getattr(args, "days", None), use_batch=ub), "rank")
    print(f"[rank] 처리={s['ranked']}  ACTIVE={s['active']}")


def cmd_translate(args):
    import llm_translate
    conn = db.open_conn()
    ub = _batch_flag(args)
    s = _ai_guard(lambda: llm_translate.run_translate(conn, days=getattr(args, "days", None), use_batch=ub), "translate")
    print(f"[translate] 대상={s['total']}  KO채움={s['ko']}  EN채움={s['en']}")


def cmd_brief(args):
    import briefing
    conn = db.open_conn()
    ub = _batch_flag(args)
    s = _ai_guard(
        lambda: briefing.run_briefing(conn, briefing_type=getattr(args, "type", "weekly"),
                                      days=getattr(args, "days", None), use_batch=ub),
        "brief",
    )
    print(f"[brief] 국가={s['countries']}  작성={s['written']}")


def cmd_ai(args):
    """prefilter → rank → translate → brief 순서 실행."""
    import briefing
    import llm_prefilter
    import llm_ranker
    import llm_translate
    import fulltext
    conn = db.open_conn()
    days = getattr(args, "days", None)
    ub = _batch_flag(args)
    print("▶ [1/5] LLM 프리필터...")
    s1 = _ai_guard(lambda: llm_prefilter.run_prefilter(conn, days=days, use_batch=ub), "ai")
    print(f"   keep={s1['keep']} drop={s1['drop']}")
    print("▶ [2/5] 본문 추출(keep 원문)...")
    sf = _ai_guard(lambda: fulltext.run_fulltext(conn, days=days), "ai")
    print(f"   본문={sf['extracted']} URL해소={sf['resolved']} 실패={sf['failed']}")
    print("▶ [3/5] AI 분석[영어]...")
    s2 = _ai_guard(lambda: llm_ranker.run_rank(conn, days=days, use_batch=ub), "ai")
    print(f"   ranked={s2['ranked']} ACTIVE={s2['active']}")
    print("▶ [4/5] 한국어 번역(표시분)...")
    st = _ai_guard(lambda: llm_translate.run_translate(conn, days=days, use_batch=ub), "ai")
    print(f"   KO채움={st['ko']} EN채움={st['en']}")
    print("▶ [5/5] 국가 일일 브리핑(현지언론 상단, 전일+당일)...")
    s3 = _ai_guard(lambda: briefing.run_briefing(conn, briefing_type="daily", days=days, use_batch=ub), "ai")
    print(f"   written={s3['written']}")


def cmd_export(args):
    import export_json
    conn = db.open_conn()
    active_only = not getattr(args, "passed", False)
    s = export_json.export_countries(conn, active_only=active_only)
    mode = "ACTIVE(ai_score≥임계)" if active_only else "passed(AI 없음)"
    print(f"[export] mode={mode}  countries={s['countries']}  articles={s['articles']}  → {s['path']}")
    p = export_json.export_pulse(conn)
    print(f"[export] pulse categories={p['categories']}  → {p['path']}")
    k = export_json.export_keyman(conn)
    print(f"[export] keyman articles={k['articles']}  → {k['path']}")
    t = export_json.export_topics(conn)
    print(f"[export] topics clusters={t['clusters']}  → {t['path']}")


def cmd_admin(_args):
    import admin_export
    conn = db.open_conn()
    r = admin_export.export_admin(conn)
    print(f"[admin] articles={r['articles']}  sources={r['sources']}  → {r['html'] or r['json']}")


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

    # AI 단계 (--sync: 배치 대신 동기 호출. 기본은 Message Batches, 토큰 50% 할인)
    _SYNC_HELP = "배치 대신 동기 호출(즉시 결과·디버깅용, 할인 없음)"
    pre = sub.add_parser("prefilter", help="LLM 1차 관문 (keep/drop)")
    pre.add_argument("--days", type=int, help="최근 N일 게시 기사만 처리")
    pre.add_argument("--sync", action="store_true", help=_SYNC_HELP)
    ftx = sub.add_parser("fulltext", help="keep 기사 원문 본문 추출 (rank 품질↑, 무료)")
    ftx.add_argument("--days", type=int, help="최근 N일 게시 기사만 처리")
    rnk = sub.add_parser("rank",      help="AI 분석[영어] (score/summary_en/topics/kb_implication_en)")
    rnk.add_argument("--days", type=int, help="최근 N일 게시 기사만 처리")
    rnk.add_argument("--sync", action="store_true", help=_SYNC_HELP)
    trn = sub.add_parser("translate", help="영어 기준본 → 한국어 번역 (표시분, 저비용)")
    trn.add_argument("--days", type=int, help="최근 N일만")
    trn.add_argument("--sync", action="store_true", help=_SYNC_HELP)
    brf = sub.add_parser("brief", help="국가별 브리핑 생성")
    brf.add_argument("--type", default="weekly", help="브리핑 유형 (weekly|daily). daily=현지언론 상단 전일+당일 종합")
    brf.add_argument("--days", type=int, help="최근 N일 게시분만 (daily 기본 1=전일+당일)")
    brf.add_argument("--sync", action="store_true", help=_SYNC_HELP)
    aip = sub.add_parser("ai",        help="prefilter → rank → brief 일괄")
    aip.add_argument("--days", type=int, help="최근 N일 게시 기사만 처리")
    aip.add_argument("--sync", action="store_true", help=_SYNC_HELP)
    exp = sub.add_parser("export", help="DB → data/export/*.json (UI 데이터)")
    exp.add_argument("--passed", action="store_true",
                     help="AI 점수 없이 필터 통과 기사로 export (Phase 1)")
    sub.add_parser("admin",     help="관리자 페이지 데이터 생성 (data/export/admin.html)")

    args = p.parse_args()
    {
        "init": cmd_init, "fetch": cmd_fetch, "filter": cmd_filter,
        "dedup": cmd_dedup, "run": cmd_run, "report": cmd_report, "list": cmd_list,
        "prefilter": cmd_prefilter, "fulltext": cmd_fulltext, "rank": cmd_rank,
        "translate": cmd_translate, "brief": cmd_brief,
        "ai": cmd_ai, "export": cmd_export, "admin": cmd_admin,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
