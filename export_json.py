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

# 현지언론 화면 대상 거점 9개 (자회사 ID·KH 는 subsidiaries 화면으로 분리)
_FLAGS = {
    "GB": "🇬🇧", "US": "🇺🇸", "JP": "🇯🇵", "HK": "🇭🇰", "SG": "🇸🇬",
    "CN": "🇨🇳", "VN": "🇻🇳", "IN": "🇮🇳", "MM": "🇲🇲",
}


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


def _ensure_ai_columns(conn) -> None:
    """export 가 참조하는 AI 컬럼을 멱등 보장(AI 미실행 환경에서도 export 동작)."""
    db.ensure_columns(conn, "articles_raw", [
        ("summary_ko",        "ALTER TABLE articles_raw ADD COLUMN summary_ko        TEXT"),
        ("kb_implication",    "ALTER TABLE articles_raw ADD COLUMN kb_implication    TEXT"),
        ("summary_en",        "ALTER TABLE articles_raw ADD COLUMN summary_en        TEXT"),
        ("kb_implication_en", "ALTER TABLE articles_raw ADD COLUMN kb_implication_en TEXT"),
    ])


def export_countries(conn, active_only: bool = True) -> dict:
    _ensure_ai_columns(conn)
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
            SELECT a.title, a.summary_ko, a.kb_implication, a.summary_en, a.kb_implication_en,
                   a.topics, a.link, a.published_at, a.ai_score, m.media_name
            FROM articles_raw a
            JOIN media_sources m ON m.source_id = a.source_id
            WHERE m.primary_country_code = ?
              AND {where}
            ORDER BY {order}
            LIMIT 20
            """,
            (cc, *params_tail),
        ).fetchall()

        articles = []
        for a in rows:
            codes = [c for c in (a["topics"] or "").split(",") if c]
            articles.append({
                "c": taxonomy.ui_string(codes),
                "src": a["media_name"],
                "d": (a["published_at"] or "")[:10],
                "t": a["title"],
                "q": a["summary_ko"] or "",
                "k": a["kb_implication"] or "",
                "q_en": a["summary_en"] or "",
                "k_en": a["kb_implication_en"] or "",
                "u": a["link"],
                "score": a["ai_score"],
            })
        total += len(articles)
        countries.append({
            "cc": cc,
            "flag": flag,
            "status": "ACTIVE" if articles else "SOURCE WATCH",
            "count": len(articles),
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

    # 자기완결 HTML (템플릿에 데이터 주입 — 오프라인 열람 / Pages 배포 겸용)
    if _TEMPLATE.exists():
        data_js = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
        html = _TEMPLATE.read_text(encoding="utf-8").replace(
            '<script id="countries-data" type="application/json">null</script>',
            f'<script id="countries-data" type="application/json">{data_js}</script>')
        (config.EXPORT_DIR / "countries.html").write_text(html, encoding="utf-8")

    log.info("countries.json 작성 — 국가=%d  ACTIVE 기사=%d  → %s", len(countries), total, path)
    return {"countries": len(countries), "articles": total, "path": str(path)}


# ---------------------------------------------------------------------------
# Global Pulse — 카테고리별 시장 온도 (뉴스 분석 도출: ai_score·topics 집계)
# ---------------------------------------------------------------------------
_PULSE_TEMPLATE = config.ROOT / "web" / "brief.html"

# taxonomy 코드 → 온도계 5대 카테고리(레퍼런스 색상)
_PULSE_CATS = [
    ("MARKET",  "경제",    "#2b5f9e"),
    ("BANKING", "금융",    "#2f7d4f"),
    ("DIGITAL", "디지털",  "#6a3fb5"),
    ("RISK",    "금융사고", "#b23b3b"),
    ("ESG",     "ESG",     "#3a8a6a"),
]


def _compute_pulse(conn, days: int | None = None) -> list[dict]:
    """랭킹된 기사(ai_score·topics)로 카테고리별 온도(0~100)를 계산.

    온도 = 0.6×평균중요도 + 0.4×상대물량  (주목도=강도+빈도). days 지정 시 최근 N일만.
    """
    date_clause, params = "", []
    if days:
        date_clause = " AND substr(published_at, 1, 10) >= date('now', ?)"
        params.append(f"-{int(days)} days")
    rows = conn.execute(
        f"SELECT ai_score, topics FROM articles_raw "
        f"WHERE ai_score IS NOT NULL AND topics IS NOT NULL AND topics <> ''{date_clause}",
        params,
    ).fetchall()

    buckets: dict[str, list[int]] = {code: [] for code, _, _ in _PULSE_CATS}
    for r in rows:
        codes = set((r["topics"] or "").split(","))
        for code in buckets:
            if code in codes:
                buckets[code].append(r["ai_score"])

    max_n = max((len(v) for v in buckets.values()), default=0) or 1
    cats = []
    for code, label, color in _PULSE_CATS:
        s = buckets[code]
        n = len(s)
        avg = sum(s) / n if n else 0
        temp = round(min(100, 0.6 * avg + 0.4 * (n / max_n * 100)))
        cats.append(dict(code=code, label=label, color=color, temp=temp, count=n, avg=round(avg)))
    return cats


_FLAGS_ALL = {"GB":"🇬🇧","US":"🇺🇸","JP":"🇯🇵","HK":"🇭🇰","SG":"🇸🇬","CN":"🇨🇳",
              "VN":"🇻🇳","IN":"🇮🇳","MM":"🇲🇲","ID":"🇮🇩","KH":"🇰🇭"}


def _date_clause(days, alias="a"):
    if days:
        return f" AND substr({alias}.published_at, 1, 10) >= date('now', ?)", [f"-{int(days)} days"]
    return "", []


def _compute_key_flows(conn, days: int | None = None) -> list[dict]:
    """카테고리별 최고 중요도 기사 1건 = '오늘의 핵심 흐름'."""
    dc, params = _date_clause(days)
    flows, used = [], set()
    for code, label, color in _PULSE_CATS:
        rows = conn.execute(
            f"""SELECT a.article_id, a.ai_score, a.title, a.summary_ko, a.summary_en, m.primary_country_code cc
                FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
                WHERE a.ai_score IS NOT NULL AND a.topics LIKE ?{dc}
                ORDER BY a.ai_score DESC LIMIT 6""",
            (f"%{code}%", *params),
        ).fetchall()
        pick = next((r for r in rows if r["article_id"] not in used), None)  # 카테고리 간 중복 제거
        if pick:
            used.add(pick["article_id"])
            flows.append(dict(code=code, label=label, color=color, cc=pick["cc"],
                              flag=_FLAGS_ALL.get(pick["cc"], ""), title=pick["title"],
                              summary=(pick["summary_ko"] or "")[:170],
                              summary_en=(pick["summary_en"] or "")[:170]))
    return flows


def _compute_top_news(conn, days: int | None = None, limit: int = 5) -> list[dict]:
    """전 거점 중요도 TOP = '오늘의 주요 시장 뉴스'."""
    dc, params = _date_clause(days)
    rows = conn.execute(
        f"""SELECT a.ai_score, a.title, a.summary_ko, a.kb_implication, a.summary_en, a.kb_implication_en,
                   a.topics, a.link, a.published_at, m.primary_country_code cc, m.media_name
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score >= ? AND a.duplicate_of IS NULL{dc}
            ORDER BY a.ai_score DESC, a.published_at DESC LIMIT ?""",
        (config.AI_SCORE_ACTIVE_THRESHOLD, *params, limit),
    ).fetchall()
    out = []
    for r in rows:
        codes = [c for c in (r["topics"] or "").split(",") if c]
        out.append(dict(cc=r["cc"], flag=_FLAGS_ALL.get(r["cc"], ""), src=r["media_name"],
                        d=(r["published_at"] or "")[:10], t=r["title"], q=r["summary_ko"] or "",
                        k=r["kb_implication"] or "", q_en=r["summary_en"] or "", k_en=r["kb_implication_en"] or "",
                        c=taxonomy.ui_string(codes), score=r["ai_score"], u=r["link"]))
    return out


def export_pulse(conn, days: int | None = None) -> dict:
    """pulse.json + brief.html(온도계 화면) 생성."""
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": _snapshot_date(),
        "days": days,
        "categories": _compute_pulse(conn, days=days),
        "key_flows": _compute_key_flows(conn, days=days),
        "top_news": _compute_top_news(conn, days=days),
    }
    _write_json("pulse", payload)

    if _PULSE_TEMPLATE.exists():
        data_js = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
        html = _PULSE_TEMPLATE.read_text(encoding="utf-8").replace(
            '<script id="pulse-data" type="application/json">null</script>',
            f'<script id="pulse-data" type="application/json">{data_js}</script>')
        (config.EXPORT_DIR / "brief.html").write_text(html, encoding="utf-8")

    hottest = max(payload["categories"], key=lambda c: c["temp"], default=None)
    log.info("pulse.json 작성 — 카테고리=%d  최고=%s", len(payload["categories"]),
             f"{hottest['label']} {hottest['temp']}°" if hottest else "-")
    return {"categories": len(payload["categories"]), "path": str(config.EXPORT_DIR / "pulse.json")}


# ---------------------------------------------------------------------------
# Key-man 인사·리더십 동향 (뉴스 기반 키워드 추출)
# ---------------------------------------------------------------------------
_KEYMAN_TEMPLATE = config.ROOT / "web" / "keyman.html"

# 인사·리더십 이동 신호 (제목+원문 요약 대상) — 정밀 우선
_KEYMAN_KW = [
    "appoint", "sworn in", "to succeed", "successor", "reshuffle",
    "steps down", "stepping down", "resign",
    "nominee", "nominated", "nomination",
    "new chair", "new chairman", "new governor", "new ceo", "new head", "new chief executive",
    "incoming chair", "incoming governor", "outgoing chair", "outgoing governor",
    "as chair", "as governor", "as the new", "named as",
    "취임", "사임", "임명", "내정", "후임", "선임", "인사이동",
]

# 단어 경계 매칭 — 'appoint'가 'disappoint'에 걸리는 부분문자열 오탐 방지
_KEYMAN_RE = re.compile(
    "|".join((r"\b" + re.escape(k) if k.isascii() else re.escape(k)) for k in _KEYMAN_KW),
    re.IGNORECASE,
)


def _compute_keyman(conn, days: int | None = None, limit: int = 24) -> list[dict]:
    """랭킹 기사 중 인사·리더십 이동 키워드가 걸린 기사 = Key-man 동향."""
    dc, params = _date_clause(days)
    rows = conn.execute(
        f"""SELECT a.ai_score, a.title, a.summary, a.summary_ko, a.kb_implication,
                   a.summary_en, a.kb_implication_en, a.topics, a.link, a.published_at,
                   m.primary_country_code cc, m.media_name
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score IS NOT NULL AND a.duplicate_of IS NULL
              AND a.ai_model LIKE '%:%'{dc}
            ORDER BY a.ai_score DESC""",
        params,
    ).fetchall()
    out = []
    for r in rows:
        hay = (r["title"] or "") + " " + (r["summary"] or "")
        if _KEYMAN_RE.search(hay):
            codes = [c for c in (r["topics"] or "").split(",") if c]
            out.append(dict(cc=r["cc"], flag=_FLAGS_ALL.get(r["cc"], ""), src=r["media_name"],
                            d=(r["published_at"] or "")[:10], t=r["title"], q=r["summary_ko"] or "",
                            k=r["kb_implication"] or "", q_en=r["summary_en"] or "", k_en=r["kb_implication_en"] or "",
                            c=taxonomy.ui_string(codes), score=r["ai_score"], u=r["link"]))
            if len(out) >= limit:
                break
    return out


def export_keyman(conn, days: int | None = None) -> dict:
    """keyman.json + keyman.html(Key-man 동향 화면) 생성."""
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": _snapshot_date(),
        "days": days,
        "articles": _compute_keyman(conn, days=days),
    }
    _write_json("keyman", payload)
    if _KEYMAN_TEMPLATE.exists():
        data_js = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
        html = _KEYMAN_TEMPLATE.read_text(encoding="utf-8").replace(
            '<script id="keyman-data" type="application/json">null</script>',
            f'<script id="keyman-data" type="application/json">{data_js}</script>')
        (config.EXPORT_DIR / "keyman.html").write_text(html, encoding="utf-8")
    log.info("keyman.json 작성 — 기사=%d", len(payload["articles"]))
    return {"articles": len(payload["articles"]), "path": str(config.EXPORT_DIR / "keyman.json")}


# ---------------------------------------------------------------------------
# 규제·정책 동향 (taxonomy RISK 주제 = 규제·리스크, 거점 횡단)
# ---------------------------------------------------------------------------
_REG_TEMPLATE = config.ROOT / "web" / "regulations.html"


def _compute_regulations(conn, days: int | None = None, limit: int = 24) -> list[dict]:
    """RISK(규제·리스크) 주제로 태깅된 기사를 거점 횡단 중요도 순으로."""
    dc, params = _date_clause(days)
    rows = conn.execute(
        f"""SELECT a.ai_score, a.title, a.summary_ko, a.kb_implication, a.summary_en, a.kb_implication_en,
                   a.topics, a.link, a.published_at, m.primary_country_code cc, m.media_name
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score IS NOT NULL AND a.duplicate_of IS NULL
              AND a.ai_model LIKE '%:%' AND a.topics LIKE '%RISK%'{dc}
            ORDER BY a.ai_score DESC LIMIT ?""",
        (*params, limit),
    ).fetchall()
    out = []
    for r in rows:
        codes = [c for c in (r["topics"] or "").split(",") if c]
        out.append(dict(cc=r["cc"], flag=_FLAGS_ALL.get(r["cc"], ""), src=r["media_name"],
                        d=(r["published_at"] or "")[:10], t=r["title"], q=r["summary_ko"] or "",
                        k=r["kb_implication"] or "", q_en=r["summary_en"] or "", k_en=r["kb_implication_en"] or "",
                        c=taxonomy.ui_string(codes), score=r["ai_score"], u=r["link"]))
    return out


def export_regulations(conn, days: int | None = None) -> dict:
    """regulations.json + regulations.html(규제·정책 화면) 생성."""
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": _snapshot_date(),
        "days": days,
        "articles": _compute_regulations(conn, days=days),
    }
    _write_json("regulations", payload)
    if _REG_TEMPLATE.exists():
        data_js = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
        html = _REG_TEMPLATE.read_text(encoding="utf-8").replace(
            '<script id="reg-data" type="application/json">null</script>',
            f'<script id="reg-data" type="application/json">{data_js}</script>')
        (config.EXPORT_DIR / "regulations.html").write_text(html, encoding="utf-8")
    log.info("regulations.json 작성 — 기사=%d", len(payload["articles"]))
    return {"articles": len(payload["articles"]), "path": str(config.EXPORT_DIR / "regulations.json")}
