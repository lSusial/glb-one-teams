"""뉴스 파이프라인 일일 품질 리포트(읽기 전용, 외부 API 비용 없음)."""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import config
import db
import kb_network
import ranking


def evidence_score(row, independent_others: int = 0) -> int:
    """본문·매체·독립 출처·충돌 여부로 근거 강도를 0~100으로 계산한다."""
    sources = min(40, round(10 + 15 * math.log2(1 + max(0, independent_others))))
    body = 25 if (row["full_text"] or "").strip() else 0
    tier = {0: 20, 1: 12, 2: 5}.get(int(row["tier"] or 2), 0)
    conflict = 0 if (row["source_conflict"] or "").strip() else 10
    summary = 5 if (row["summary_en"] or "").strip() else 0
    return min(100, sources + body + tier + conflict + summary)


def _pct(n: int, d: int) -> float:
    return round(n * 100.0 / d, 1) if d else 0.0


def build_report(conn, days: int = 1, export_dir: Path | None = None) -> dict:
    """DB 최신 게시일 기준 전일+당일 품질·소스 수율·경보를 계산한다."""
    latest = conn.execute(
        "SELECT MAX(substr(published_at,1,10)) FROM articles_raw"
    ).fetchone()[0]
    if not latest:
        return {"snapshot_date": None, "window_days": days + 1, "summary": {},
                "countries": [], "sources": [], "alerts": [
                    {"severity": "critical", "code": "NO_DATA", "message": "수집 데이터가 없습니다."}
                ]}

    cols = db.table_columns(conn)
    def col(name: str, fallback: str = "NULL") -> str:
        return f"a.{name}" if name in cols else fallback

    rows = conn.execute(
        f"""SELECT a.article_id, a.duplicate_of, a.filter_decision, a.llm_prefilter,
                   a.ai_score, a.published_at, {col('full_text', "''")} AS full_text,
                   {col('summary_en', "''")} AS summary_en,
                   {col('source_conflict', "''")} AS source_conflict,
                   {col('publisher_name', "''")} AS publisher_name,
                   {col('event_type', "''")} AS event_type,
                   {col('korean_fi', "''")} AS korean_fi,
                   {col('personnel_move', '0')} AS personnel_move,
                   {db.effective_country_expr()} AS cc,
                   m.media_name, m.tier, m.primary_country_code
            FROM articles_raw a JOIN media_sources m ON m.source_id=a.source_id
            WHERE substr(a.published_at,1,10) >= date(?, ?)""",
        (latest, f"-{int(days)} days"),
    ).fetchall()
    roots = [r for r in rows if r["duplicate_of"] is None]
    scored = [r for r in roots if r["ai_score"] is not None]
    active = [r for r in scored if r["ai_score"] >= config.AI_SCORE_ACTIVE_THRESHOLD]
    watch = [r for r in scored if config.COUNTRY_WATCH_FLOOR <= r["ai_score"] < config.AI_SCORE_ACTIVE_THRESHOLD]

    active_scores = Counter(int(r["ai_score"]) for r in active)
    concentration = max(active_scores.values(), default=0)
    clusters = ranking.cluster_sizes(conn)
    evidence = [evidence_score(r, clusters.get(int(r["article_id"]), 0)) for r in active]

    summary = {
        "raw": len(rows),
        "roots": len(roots),
        "passed": sum(r["filter_decision"] == "passed" for r in roots),
        "kept": sum(r["llm_prefilter"] == "keep" for r in roots),
        "scored": len(scored),
        "active": len(active),
        "watch_band": len(watch),
        "scored_fulltext": sum(bool((r["full_text"] or "").strip()) for r in scored),
        "active_fulltext": sum(bool((r["full_text"] or "").strip()) for r in active),
        "active_fulltext_pct": _pct(sum(bool((r["full_text"] or "").strip()) for r in active), len(active)),
        "score_mode": active_scores.most_common(1)[0][0] if active_scores else None,
        "score_mode_count": concentration,
        "score_mode_pct": _pct(concentration, len(active)),
        "evidence_avg": round(sum(evidence) / len(evidence), 1) if evidence else 0.0,
        "evidence_below_50": sum(v < 50 for v in evidence),
    }

    displayed_counts = {}
    if export_dir:
        try:
            exported = json.loads((export_dir / "countries.json").read_text(encoding="utf-8"))
            if exported.get("snapshot_date") == latest:
                displayed_counts = {
                    item["cc"]: len(item.get("articles", []))
                    for item in exported.get("countries", [])
                }
        except (OSError, ValueError, KeyError):
            pass

    by_country = defaultdict(list)
    for r in roots:
        by_country[r["cc"]].append(r)
    countries = []
    for cc in kb_network.KB_NETWORK:
        group = by_country.get(cc, [])
        cc_active = [r for r in group if r["ai_score"] is not None and r["ai_score"] >= config.AI_SCORE_ACTIVE_THRESHOLD]
        cc_watch = [r for r in group if r["ai_score"] is not None and config.COUNTRY_WATCH_FLOOR <= r["ai_score"] < config.AI_SCORE_ACTIVE_THRESHOLD]
        eligible = len(cc_active) or min(len(cc_watch), config.COUNTRY_WATCH_MAX)
        displayed = displayed_counts.get(cc, eligible)
        countries.append({
            "cc": cc, "raw": len(group),
            "passed": sum(r["filter_decision"] == "passed" for r in group),
            "kept": sum(r["llm_prefilter"] == "keep" for r in group),
            "scored": sum(r["ai_score"] is not None for r in group),
            "active": len(cc_active), "watch": len(cc_watch),
            "eligible": eligible, "displayed": displayed,
            "fulltext": sum(bool((r["full_text"] or "").strip()) for r in group if r["ai_score"] is not None),
        })

    by_source = defaultdict(list)
    for r in rows:
        publisher = (r["publisher_name"] or r["media_name"] or "unknown").strip()
        by_source[publisher].append(r)
    sources = []
    for name, group in by_source.items():
        root_group = [r for r in group if r["duplicate_of"] is None]
        sources.append({
            "source": name, "raw": len(group),
            "passed": sum(r["filter_decision"] == "passed" for r in root_group),
            "kept": sum(r["llm_prefilter"] == "keep" for r in root_group),
            "fulltext": sum(bool((r["full_text"] or "").strip()) for r in root_group),
            "active": sum(r["ai_score"] is not None and r["ai_score"] >= config.AI_SCORE_ACTIVE_THRESHOLD for r in root_group),
        })
    sources.sort(key=lambda x: (x["active"], x["kept"], x["raw"]), reverse=True)

    feed_failures = conn.execute(
        "SELECT COUNT(*) FROM media_source_feeds WHERE is_active=1 "
        "AND (last_status IS NULL OR last_status < 200 OR last_status >= 300)"
    ).fetchone()[0]
    alerts = []
    if len(active) < 20:
        alerts.append({"severity": "critical", "code": "ACTIVE_LOW",
                       "message": f"ACTIVE가 {len(active)}건으로 최소 기준 20건보다 적습니다."})
    if summary["active_fulltext_pct"] < 60:
        alerts.append({"severity": "warning", "code": "FULLTEXT_LOW",
                       "message": f"ACTIVE 본문 확보율이 {summary['active_fulltext_pct']}%입니다."})
    if summary["score_mode_pct"] > 30:
        alerts.append({"severity": "warning", "code": "SCORE_CONCENTRATION",
                       "message": f"ACTIVE의 {summary['score_mode_pct']}%가 {summary['score_mode']}점에 집중됐습니다."})
    empty = [c["cc"] for c in countries if c["displayed"] == 0]
    thin = [c["cc"] for c in countries if c["displayed"] <= 1]
    if empty:
        alerts.append({"severity": "warning", "code": "EMPTY_COUNTRIES",
                       "message": f"노출 0건 국가: {', '.join(empty)}"})
    if thin:
        alerts.append({"severity": "info", "code": "THIN_COUNTRIES",
                       "message": f"노출 0~1건 국가: {', '.join(thin)}"})
    if feed_failures:
        alerts.append({"severity": "warning", "code": "FEED_FAILURES",
                       "message": f"마지막 수집 실패 또는 미확인 활성 피드가 {feed_failures}개입니다."})
    if summary["evidence_below_50"]:
        alerts.append({"severity": "warning", "code": "WEAK_EVIDENCE",
                       "message": f"ACTIVE 중 근거 강도 50 미만이 {summary['evidence_below_50']}건입니다."})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": latest,
        "window_days": days + 1,
        "summary": summary,
        "score_distribution": dict(sorted(active_scores.items())),
        "countries": countries,
        "sources": sources,
        "feed_failures": feed_failures,
        "alerts": alerts,
    }


def to_markdown(report: dict) -> str:
    s = report.get("summary", {})
    lines = [f"# 뉴스 품질 리포트 — {report.get('snapshot_date') or '데이터 없음'}", ""]
    if s:
        lines += [
            f"- ACTIVE: **{s['active']}건**, 관심구간: {s['watch_band']}건",
            f"- ACTIVE 본문 확보: **{s['active_fulltext_pct']}%** ({s['active_fulltext']}/{s['active']})",
            f"- 최빈 점수: **{s['score_mode']}점 {s['score_mode_count']}건 ({s['score_mode_pct']}%)**",
            f"- 평균 근거 강도: **{s['evidence_avg']}점**, 50점 미만 {s['evidence_below_50']}건",
            "", "## 경보", "",
        ]
    for a in report.get("alerts", []):
        lines.append(f"- **{a['severity'].upper()} · {a['code']}** — {a['message']}")
    lines += ["", "## 국가별 수율", "",
              "| 국가 | 수집 | 통과 | keep | 채점 | ACTIVE | 관심 | 노출 | 본문 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for c in report.get("countries", []):
        lines.append(f"| {c['cc']} | {c['raw']} | {c['passed']} | {c['kept']} | {c['scored']} | "
                     f"{c['active']} | {c['watch']} | {c['displayed']} | {c['fulltext']} |")
    lines += ["", "## 소스 수율 상위 30", "",
              "| 소스 | 수집 | 통과 | keep | 본문 | ACTIVE |",
              "|---|---:|---:|---:|---:|---:|"]
    for source in report.get("sources", [])[:30]:
        name = source["source"].replace("|", "/")
        lines.append(f"| {name} | {source['raw']} | {source['passed']} | {source['kept']} | "
                     f"{source['fulltext']} | {source['active']} |")
    return "\n".join(lines) + "\n"


def write_report(report: dict, output_dir: Path | None = None) -> dict:
    out = output_dir or config.DATA_DIR / "quality"
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "latest.json"
    md_path = out / "latest.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}
