"""
DB → UI 데이터 계약(JSON) export (export_json.py)

화면 코딩과 무관한 백엔드 export. 설계: 화면분석_개발가이드.md / 데이터_AI_카테고리_설계.md
현재: countries.json(현지언론 Intelligence 화면) 구현. 나머지 화면(subs/topics/brief)은
동일 패턴으로 확장한다.

UI 매핑 (현지언론 기사 카드):
  c=topics→ui키, src=매체, d=날짜, t=제목, q=summary_ko, k=kb_implication, u=link
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import config
import db
import taxonomy

log = logging.getLogger("export_json")

_TEMPLATE = config.ROOT / "web" / "countries.html"   # 현지언론 화면 템플릿

# 거점 국기 이모지. GLOBAL은 국가 화면(11개 거점)에서는 제외하고, 횡단 화면(Pulse 등)에서만 사용.
_FLAGS_ALL = {"GB": "🇬🇧", "US": "🇺🇸", "JP": "🇯🇵", "HK": "🇭🇰", "SG": "🇸🇬", "CN": "🇨🇳",
              "VN": "🇻🇳", "IN": "🇮🇳", "MM": "🇲🇲", "ID": "🇮🇩", "KH": "🇰🇭", "GLOBAL": "🌐"}
_FLAGS = {cc: f for cc, f in _FLAGS_ALL.items() if cc != "GLOBAL"}


def _snapshot_date(date: str | None = None) -> str:
    """스냅샷 라벨 날짜 (기본: 오늘, 로컬)."""
    return date or datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def _write_json(name: str, payload: dict) -> None:
    """최신본 + 날짜 아카이브(archive/YYYY-MM-DD/) 저장 + 날짜 인덱스(dates.json) 갱신."""
    (config.EXPORT_DIR / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    date = payload.get("snapshot_date") or _snapshot_date()
    adir = config.EXPORT_DIR / "archive" / date
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    idx = config.EXPORT_DIR / "archive" / "dates.json"
    dates = json.loads(idx.read_text(encoding="utf-8")) if idx.exists() else []
    if date not in dates:
        dates.append(date)
        dates.sort(reverse=True)
        idx.write_text(json.dumps(dates, ensure_ascii=False), encoding="utf-8")


def _inject_html(template, tag_id: str, out_name: str, payload: dict) -> None:
    """템플릿(web/*.html)의 <script id="{tag_id}">null</script>에 payload를 주입해
    자기완결 HTML(data/export/{out_name}.html)로 저장 — 오프라인 열람/Pages 배포 겸용."""
    if not template.exists():
        return
    data_js = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    html = template.read_text(encoding="utf-8").replace(
        f'<script id="{tag_id}" type="application/json">null</script>',
        f'<script id="{tag_id}" type="application/json">{data_js}</script>')
    (config.EXPORT_DIR / f"{out_name}.html").write_text(html, encoding="utf-8")


def _ensure_ai_columns(conn) -> None:
    """export 가 참조하는 AI 컬럼을 멱등 보장(AI 미실행 환경에서도 export 동작)."""
    db.ensure_columns(conn, "articles_raw", [
        ("summary_ko",        "ALTER TABLE articles_raw ADD COLUMN summary_ko        TEXT"),
        ("kb_implication",    "ALTER TABLE articles_raw ADD COLUMN kb_implication    TEXT"),
        ("summary_en",        "ALTER TABLE articles_raw ADD COLUMN summary_en        TEXT"),
        ("kb_implication_en", "ALTER TABLE articles_raw ADD COLUMN kb_implication_en TEXT"),
    ])


def _daily_briefs(conn) -> dict:
    """country_briefings(daily) 최신본을 {cc: {ko, en, date}} 로 반환. 없으면 {}."""
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(country_briefings)")]
    except Exception:
        return {}
    if "cc" not in cols:
        return {}
    has_en = "summary_en" in cols
    sel = "cc, summary, briefing_date" + (", summary_en" if has_en else "")
    rows = conn.execute(
        f"SELECT {sel} FROM country_briefings WHERE briefing_type='daily' "
        "ORDER BY briefing_date DESC, generated_at DESC"
    ).fetchall()
    out = {}
    for r in rows:
        cc = r["cc"]
        if cc in out:            # 국가별 최신 1건만
            continue
        out[cc] = {
            "ko": r["summary"] or "",
            "en": (r["summary_en"] if has_en else "") or "",
            "date": r["briefing_date"],
        }
    return out


def export_countries(conn, active_only: bool = True, days: int = 1) -> dict:
    _ensure_ai_columns(conn)
    dc, dparams = _date_clause(days)   # 현지언론 = 전일+당일 (max-1일 이후)
    briefs = _daily_briefs(conn)
    """국가별 기사를 UI 데이터 계약(countries.json)으로 내보낸다.

    active_only=True  (기본, AI 실행 후): ai_score >= AI_SCORE_ACTIVE_THRESHOLD 인 ACTIVE 기사만.
    active_only=False (Phase 1, AI 없이): 키워드 필터 통과(passed) 기사. 요약(q)·시사점(k)은 비어있을 수 있음.
    """
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if active_only:
        where, params_tail = "a.ai_score >= ? AND a.duplicate_of IS NULL", (config.AI_SCORE_ACTIVE_THRESHOLD,)
        order = "a.ai_score DESC, a.published_at DESC"
    else:
        where, params_tail = "a.filter_decision = 'passed' AND a.duplicate_of IS NULL", ()
        order = "a.published_at DESC"

    countries = []
    total = 0

    for cc, flag in _FLAGS.items():
        rows = conn.execute(
            f"""
            SELECT a.title, a.title_ko, a.summary_ko, a.kb_implication, a.summary_en, a.kb_implication_en,
                   a.topics, a.link, a.published_at, a.ai_score, m.media_name
            FROM articles_raw a
            JOIN media_sources m ON m.source_id = a.source_id
            WHERE m.primary_country_code = ?
              AND {where}{dc}
            ORDER BY {order}
            LIMIT 20
            """,
            (cc, *params_tail, *dparams),
        ).fetchall()

        articles = []
        for i, a in enumerate(rows):
            codes = [c for c in (a["topics"] or "").split(",") if c]
            my_topics = set(codes)
            # related links: 같은 cc 내 topics 겹치는 다른 기사 (없으면 순번 다음 기사)
            rl = [{"t": a["title"][:100], "u": a["link"]}]
            for j, b in enumerate(rows):
                if j == i:
                    continue
                b_topics = set((b["topics"] or "").split(","))
                if (not my_topics) or (my_topics & b_topics):
                    rl.append({"t": b["title"][:100], "u": b["link"]})
                    break
            if len(rl) < 2:
                for j, b in enumerate(rows):
                    if j != i:
                        rl.append({"t": b["title"][:100], "u": b["link"]})
                        break
            articles.append({
                "c": taxonomy.ui_string(codes),
                "src": a["media_name"],
                "d": (a["published_at"] or "")[:10],
                "t": a["title_ko"] or a["title"],
                "q": a["summary_ko"] or "",
                "q_en": a["summary_en"] or "",
                "u": a["link"],
                "rl": rl[:2],
                "score": a["ai_score"],
            })
        total += len(articles)
        countries.append({
            "cc": cc,
            "flag": flag,
            "status": "ACTIVE" if articles else "SOURCE WATCH",
            "count": len(articles),
            "brief": briefs.get(cc),            # 전일+당일 AI 브리핑(한/영) — 없으면 null
            "articles": articles,
        })

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": _snapshot_date(),
        "mode": "active" if active_only else "passed",
        "active_threshold": config.AI_SCORE_ACTIVE_THRESHOLD if active_only else None,
        "countries": countries,
    }
    _write_json("countries", payload)
    path = config.EXPORT_DIR / "countries.json"

    _inject_html(_TEMPLATE, "countries-data", "countries", payload)

    log.info("countries.json 작성 — 국가=%d  ACTIVE 기사=%d  → %s", len(countries), total, path)
    return {"countries": len(countries), "articles": total, "path": str(path)}


# ---------------------------------------------------------------------------
# Global Pulse — 카테고리별 시장 온도 (뉴스 분석 도출: ai_score·topics 집계)
# ---------------------------------------------------------------------------
_PULSE_TEMPLATE = config.ROOT / "web" / "brief.html"

# taxonomy 코드 → 온도계 5대 카테고리(배지·필터와 라벨·색상 일치: 경제/금융/디지털/ESG/리스크)
_PULSE_CATS = [
    ("MARKET",  "경제",   "Economy", "#2b5f9e"),
    ("BANKING", "금융",   "Banking", "#2f7d4f"),
    ("DIGITAL", "디지털", "Digital", "#6a3fb5"),
    ("ESG",     "ESG",    "ESG",     "#3a8a6a"),
    ("RISK",    "리스크", "Risk",    "#b23b3b"),
]


def _compute_pulse(conn, days: int | None = None) -> list[dict]:
    """랭킹된 기사(ai_score·topics)로 카테고리별 온도(0~100)를 계산.

    온도 = 0.6×평균중요도 + 0.4×상대물량  (주목도=강도+빈도). days 지정 시 최근 N일만.
    """
    date_clause, params = _date_clause(days, alias="")
    rows = conn.execute(
        f"SELECT ai_score, topics FROM articles_raw "
        f"WHERE ai_score IS NOT NULL AND topics IS NOT NULL AND topics <> ''{date_clause}",
        params,
    ).fetchall()

    buckets: dict[str, list[int]] = {code: [] for code, _, _, _ in _PULSE_CATS}
    for r in rows:
        codes = set((r["topics"] or "").split(","))
        for code in buckets:
            if code in codes:
                buckets[code].append(r["ai_score"])

    max_n = max((len(v) for v in buckets.values()), default=0) or 1
    cats = []
    for code, label, label_en, color in _PULSE_CATS:
        s = buckets[code]
        n = len(s)
        avg = sum(s) / n if n else 0
        temp = round(min(100, 0.6 * avg + 0.4 * (n / max_n * 100)))
        cats.append(dict(code=code, label=label, label_en=label_en, color=color,
                         temp=temp, count=n, avg=round(avg)))
    return cats


def _date_clause(days, alias="a"):
    """최신 데이터일 기준 최근 N일 창 (스냅샷·실시간 모두 안전). days 없으면 전체."""
    return db.days_clause_data(days, alias)


def _compute_key_flows(conn, days: int | None = None, limit: int = 6) -> list[dict]:
    """중요도 상위 기사 = '오늘의 핵심 흐름' (카테고리 무관·국가 무관, 카테고리는 배지로만 표시).

    한 카테고리가 여러 개여도 OK(경제 2~3, 금융사고 4…), 없는 카테고리는 노출 안 함.
    """
    dc, params = _date_clause(days)
    rows = conn.execute(
        f"""SELECT a.ai_score, a.title, a.summary_ko, a.summary_en, a.topics, m.primary_country_code cc
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score >= ? AND a.duplicate_of IS NULL AND a.ai_model LIKE '%:%'{dc}
            ORDER BY a.ai_score DESC LIMIT ?""",
        (config.AI_SCORE_ACTIVE_THRESHOLD, *params, limit),
    ).fetchall()
    flows = []
    for r in rows:
        codes = [c for c in (r["topics"] or "").split(",") if c]
        flows.append(dict(cc=r["cc"], flag=_FLAGS_ALL.get(r["cc"], ""), title=r["title"],
                          summary=(r["summary_ko"] or "")[:170], summary_en=(r["summary_en"] or "")[:170],
                          c=taxonomy.ui_string(codes), score=r["ai_score"]))
    return flows


_TN_STOP = set("a an the of to in on for and or with at by from as is are be after just against "
               "over into up out will its news com vs amid".split())


def _sig_tokens(text: str) -> set:
    """제목 유사도용 유의미 토큰(소문자·불용어 제거·3자+)."""
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in _TN_STOP and len(w) > 2}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _compute_top_news(conn, days: int | None = None, limit: int = 8) -> list[dict]:
    """전 거점 중요도 TOP = '오늘의 핵심 뉴스'.

    다양성: ① 제목 근접중복(같은 기사 재탕) 제외  ② 국가별 상한(도배 방지).
    후보 풀을 넉넉히 뽑아 위 규칙 적용 후 limit 만큼.
    """
    dc, params = _date_clause(days)
    rows = conn.execute(
        f"""SELECT a.ai_score, a.title, a.title_ko, a.summary_ko, a.summary_en,
                   a.topics, a.link, a.published_at, m.primary_country_code cc, m.media_name
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score >= ? AND a.duplicate_of IS NULL{dc}
            ORDER BY a.ai_score DESC, a.published_at DESC LIMIT 60""",
        (config.AI_SCORE_ACTIVE_THRESHOLD, *params),
    ).fetchall()

    out, seen_tokens, per_cc = [], [], {}
    for r in rows:
        tk = _sig_tokens(r["title"])
        if any(_jaccard(tk, s) >= config.TOP_NEWS_SIM for s in seen_tokens):
            continue                                   # 근접 중복(같은 기사 재탕)
        cc = r["cc"]
        if per_cc.get(cc, 0) >= config.TOP_NEWS_PER_COUNTRY:
            continue                                   # 국가별 상한
        codes = [c for c in (r["topics"] or "").split(",") if c]
        my_topics = set(codes)
        # related: 전체 후보에서 같은 cc + topics 겹치는 기사 1개
        rl = [{"t": r["title"][:100], "u": r["link"]}]
        for b in rows:
            if b["link"] == r["link"]:
                continue
            b_topics = set((b["topics"] or "").split(","))
            if b["cc"] == cc and (not my_topics or my_topics & b_topics):
                rl.append({"t": b["title"][:100], "u": b["link"]})
                break
        t_ko = r["title_ko"] if "title_ko" in r.keys() else None
        out.append(dict(cc=cc, flag=_FLAGS_ALL.get(cc, ""), src=r["media_name"],
                        d=(r["published_at"] or "")[:10],
                        t=t_ko or r["title"], q=r["summary_ko"] or "",
                        q_en=r["summary_en"] or "",
                        c=taxonomy.ui_string(codes), score=r["ai_score"], u=r["link"], rl=rl[:2]))
        seen_tokens.append(tk)
        per_cc[cc] = per_cc.get(cc, 0) + 1
        if len(out) >= limit:
            break
    return out


def export_pulse(conn, days: int | None = None) -> dict:
    """pulse.json + brief.html(온도계 화면) 생성."""
    if days is None: days = 1          # 첫 화면 = 전일+당일 ('오늘의 ...')
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": _snapshot_date(),
        "days": days,
        "categories": _compute_pulse(conn, days=days),
        "top_news": _compute_top_news(conn, days=days, limit=8),
    }
    _write_json("pulse", payload)

    _inject_html(_PULSE_TEMPLATE, "pulse-data", "brief", payload)

    hottest = max(payload["categories"], key=lambda c: c["temp"], default=None)
    log.info("pulse.json 작성 — 카테고리=%d  최고=%s", len(payload["categories"]),
             f"{hottest['label']} {hottest['temp']}°" if hottest else "-")
    return {"categories": len(payload["categories"]), "path": str(config.EXPORT_DIR / "pulse.json")}


# ---------------------------------------------------------------------------
# 주간 브리핑 (국가별) — country_briefings(weekly) 기반
# ---------------------------------------------------------------------------
_WEEKLY_TEMPLATE = config.ROOT / "web" / "weekly.html"


def _weekly_briefs(conn) -> list[dict]:
    """country_briefings(weekly) 최신본을 국가별 1건으로 반환(한/영)."""
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(country_briefings)")]
    except Exception:
        return []
    if "cc" not in cols:
        return []
    wanted = ["cc", "summary", "summary_en", "issues", "issues_en", "outlook",
              "outlook_en", "keywords", "key_stat", "briefing_date", "article_count"]
    sel = ", ".join(c for c in wanted if c in cols)
    rows = conn.execute(
        f"SELECT {sel} FROM country_briefings WHERE briefing_type='weekly' "
        "ORDER BY briefing_date DESC, generated_at DESC"
    ).fetchall()

    def jl(v):
        try:
            return json.loads(v) if v else []
        except Exception:
            return []

    seen, out = set(), []
    for r in rows:
        cc = r["cc"]
        if cc in seen or cc in ("KR", "GLOBAL"):
            continue
        seen.add(cc)
        k = r.keys()
        g = lambda c: (r[c] if c in k else "") or ""
        out.append(dict(
            cc=cc, flag=_FLAGS_ALL.get(cc, ""),
            summary=g("summary"), summary_en=g("summary_en"),
            issues=jl(r["issues"] if "issues" in k else None),
            issues_en=jl(r["issues_en"] if "issues_en" in k else None),
            outlook=g("outlook"), outlook_en=g("outlook_en"),
            keywords=jl(r["keywords"] if "keywords" in k else None),
            key_stat=g("key_stat"), date=g("briefing_date"),
            n=(r["article_count"] if "article_count" in k else None),
        ))
    return out


def export_weekly(conn) -> dict:
    """weekly.json + weekly.html(국가별 주간 브리핑) 생성."""
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": _snapshot_date(),
        "countries": _weekly_briefs(conn),
    }
    _write_json("weekly", payload)
    _inject_html(_WEEKLY_TEMPLATE, "weekly-data", "weekly", payload)
    log.info("weekly.json 작성 — 국가=%d", len(payload["countries"]))
    return {"countries": len(payload["countries"]), "path": str(config.EXPORT_DIR / "weekly.json")}


# ---------------------------------------------------------------------------
# TopicWatch — taxonomy 카테고리별 주간 뉴스
# ---------------------------------------------------------------------------
_TOPICS_TEMPLATE = config.ROOT / "web" / "topics.html"

_TOPIC_CATS = [
    ("MARKET",  "economy",  "경제",       "Economy"),
    ("BANKING", "finance",  "금융산업",    "Banking"),
    ("DIGITAL", "digital",  "디지털",      "Digital"),
    ("RISK",    "risk",     "규제·리스크", "Regulation·Risk"),
    ("GEO",     "geo",      "지정학",      "Geopolitics"),
    ("ESG",     "esg",      "ESG",         "ESG"),
]


def _compute_topics(conn, days: int | None = None, max_per: int = 15) -> list[dict]:
    """taxonomy 카테고리별로 ACTIVE 기사를 묶어 반환."""
    dc, params = _date_clause(days)
    rows = conn.execute(
        f"""SELECT a.ai_score, a.title, a.summary, a.summary_ko, a.kb_implication,
                   a.summary_en, a.kb_implication_en, a.topics, a.link, a.published_at,
                   m.primary_country_code cc, m.media_name
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score >= ? AND a.duplicate_of IS NULL AND a.ai_model LIKE '%:%'{dc}
            ORDER BY a.ai_score DESC""",
        (config.AI_SCORE_ACTIVE_THRESHOLD, *params),
    ).fetchall()

    categories = []
    for code, ui, label_ko, label_en in _TOPIC_CATS:
        arts, ccs = [], set()
        for r in rows:
            codes = [c.strip() for c in (r["topics"] or "").split(",") if c.strip()]
            if code not in codes:
                continue
            arts.append(dict(cc=r["cc"], flag=_FLAGS_ALL.get(r["cc"], ""),
                             src=r["media_name"],
                             d=(r["published_at"] or "")[:10],
                             t=r["title"], q=r["summary_ko"] or "",
                             k=r["kb_implication"] or "",
                             q_en=r["summary_en"] or "",
                             k_en=r["kb_implication_en"] or "",
                             c=taxonomy.ui_string(codes),
                             score=r["ai_score"], u=r["link"]))
            ccs.add(r["cc"])
            if len(arts) >= max_per:
                break
        if arts:
            categories.append(dict(code=code, ui=ui,
                                   label=label_ko, label_en=label_en,
                                   count=len(arts), ccs=sorted(ccs),
                                   articles=arts))
    return categories


def export_topics(conn, days: int | None = None) -> dict:
    """topics.json + topics.html(TopicWatch 화면) 생성."""
    if days is None: days = 7
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    cats = _compute_topics(conn, days=days)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": _snapshot_date(),
        "days": days,
        "categories": cats,
    }
    _write_json("topics", payload)
    _inject_html(_TOPICS_TEMPLATE, "topics-data", "topics", payload)
    log.info("topics.json 작성 — 카테고리=%d", len(cats))
    return {"categories": len(cats), "path": str(config.EXPORT_DIR / "topics.json")}
