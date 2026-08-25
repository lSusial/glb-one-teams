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

# 진출국 11개 한/영 국가명 — country_signals(국가별 시장 신호 보드)에서 사용.
_PRESENCE_NAMES_KO = {"GB": "영국", "US": "미국", "HK": "홍콩", "CN": "중국", "JP": "일본",
                       "SG": "싱가포르", "IN": "인도", "VN": "베트남", "MM": "미얀마",
                       "ID": "인도네시아", "KH": "캄보디아"}
_PRESENCE_NAMES_EN = {"GB": "UK", "US": "US", "HK": "Hong Kong", "CN": "China", "JP": "Japan",
                       "SG": "Singapore", "IN": "India", "VN": "Vietnam", "MM": "Myanmar",
                       "ID": "Indonesia", "KH": "Cambodia"}

# MMK/KHR — 시장환율이 아닌 별도 체계임을 보드에 짧게 표시.
_FX_NOTE = {"MMK": "공식", "KHR": "페그"}


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


def _country_indicators(conn) -> dict:
    """국가별 최신 거시지표 스냅샷(indicators.py) — {cc: [{kind,symbol,label,value,change,change_pct,note}, ...]}."""
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(indicators)")]
    except Exception:
        return {}
    if "country" not in cols:
        return {}
    rows = conn.execute(
        """SELECT country, kind, symbol, label, value, change, change_pct, note
           FROM indicators WHERE date = (SELECT MAX(date) FROM indicators)
           ORDER BY country, kind ASC"""   # 'fx' < 'index' 알파벳 순 → 환율 카드가 먼저 나오도록
    ).fetchall()
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["country"], []).append({
            "kind": r["kind"], "symbol": r["symbol"], "label": r["label"],
            "value": r["value"], "change": r["change"], "change_pct": r["change_pct"],
            "note": r["note"],
        })
    return out


def _daily_highlights(conn) -> list:
    """오늘의 글로벌 핵심 3줄(briefing.generate_daily_highlights) 최신본. 없으면 빈 리스트."""
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(daily_highlights)")]
    except Exception:
        return []
    if "items" not in cols:
        return []
    row = conn.execute(
        "SELECT items FROM daily_highlights ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["items"]) or []
    except Exception:
        return []


def _compute_non_presence(conn, days: int = 1, limit: int = 40) -> list[dict]:
    """KB 미진출국(14개, docs/design_미진출국.md) 통합 피드 — 국가 구분 없이
    ai_score 상위 limit개. ACTIVE 임계(55점) 게이트는 적용하지 않는다 — 거점이
    없는 시장이라 점수 루브릭 자체가 낮게 잡히므로, 랭킹만 됐으면(ai_score IS
    NOT NULL) 상대적으로 중요한 순서로 노출한다.

    근접중복(의역) 클러스터링: 같은 국가(cc) 내에서 제목 토큰 겹침 비율(교집합/min,
    Jaccard와 동등한 대안 — 길이가 다른 헤드라인 쌍에서도 안정적)이
    config.NON_PRESENCE_DEDUP_SIM 이상이면 같은 사건으로 보고 대표 1건만 남긴다.
    후보를 (ai_score DESC, published_at DESC)로 뽑으므로 각 클러스터에서 가장
    먼저 나오는 기사가 곧 대표(점수 최고 → 동점이면 최신)가 된다."""
    if not config.NON_PRESENCE_CODES:
        return []
    dc, dparams = _date_clause(days)
    ph = ",".join("?" * len(config.NON_PRESENCE_CODES))
    fetch_limit = limit * 5   # 클러스터링으로 줄어들 것을 감안해 후보를 넉넉히 뽑음
    rows = conn.execute(
        f"""SELECT a.title, a.title_ko, a.summary_ko, a.summary_en, a.topics,
                   a.link, a.published_at, a.ai_score, m.primary_country_code cc, m.media_name
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score IS NOT NULL AND a.duplicate_of IS NULL AND a.ai_model LIKE '%:%'
              AND m.primary_country_code IN ({ph}){dc}
            ORDER BY a.ai_score DESC, a.published_at DESC LIMIT ?""",
        (*config.NON_PRESENCE_CODES, *dparams, fetch_limit),
    ).fetchall()

    clusters: dict[str, list[dict]] = {}   # cc -> [{"row": Row, "tk": set, "n": int}, ...]
    for r in rows:
        bucket = clusters.setdefault(r["cc"], [])
        tk = _sig_tokens(_strip_source_suffix(r["title"]))
        for c in bucket:
            if _overlap_ratio(tk, c["tk"]) >= config.NON_PRESENCE_DEDUP_SIM:
                c["n"] += 1
                break
        else:
            bucket.append({"row": r, "tk": tk, "n": 1})

    reps = [c for bucket in clusters.values() for c in bucket]
    reps.sort(key=lambda c: c["row"]["ai_score"], reverse=True)
    reps = reps[:limit]

    out = []
    for c in reps:
        r = c["row"]
        cc = r["cc"]
        meta = config.NON_PRESENCE_COUNTRIES.get(cc, {})
        codes = [x for x in (r["topics"] or "").split(",") if x]
        out.append(dict(
            cc=cc, flag=meta.get("flag", ""),
            cc_label=meta.get("name_ko", cc), cc_label_en=meta.get("name_en", cc),
            src=r["media_name"], d=(r["published_at"] or "")[:10],
            t=r["title_ko"] or r["title"], q=r["summary_ko"] or "", q_en=r["summary_en"] or "",
            c=taxonomy.ui_string(codes), score=r["ai_score"], u=r["link"],
            related_count=c["n"] - 1,
        ))
    return out


def export_countries(conn, active_only: bool = True, days: int = 1) -> dict:
    _ensure_ai_columns(conn)
    dc, dparams = _date_clause(days)   # 현지언론 = 전일+당일 (max-1일 이후)
    briefs = _daily_briefs(conn)
    indicators = _country_indicators(conn)
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
            "presence": "진출",                 # KB 진출국 — docs/design_미진출국.md
            "status": "ACTIVE" if articles else "SOURCE WATCH",
            "count": len(articles),
            "brief": briefs.get(cc),            # 전일+당일 AI 브리핑(한/영) — 없으면 null
            "articles": articles,
            "indicators": indicators.get(cc, []),  # 환율·주가지수 최신 스냅샷 — 없으면 빈 리스트
        })

    non_presence = _compute_non_presence(conn)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": _snapshot_date(),
        "mode": "active" if active_only else "passed",
        "active_threshold": config.AI_SCORE_ACTIVE_THRESHOLD if active_only else None,
        "countries": countries,
        "non_presence": {"count": len(non_presence), "articles": non_presence},
    }
    _write_json("countries", payload)
    path = config.EXPORT_DIR / "countries.json"

    _inject_html(_TEMPLATE, "countries-data", "countries", payload)

    log.info("countries.json 작성 — 국가=%d  ACTIVE 기사=%d  미진출국 기사=%d  → %s",
             len(countries), total, len(non_presence), path)
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
    date_clause, params = _date_clause(days, alias="a")
    # KB 미진출국은 온도 집계에서 제외 — 거점 없는 시장 뉴스가 카테고리 온도를 왜곡하지 않도록.
    exc, exp = db.exclude_countries_clause(config.NON_PRESENCE_CODES)
    rows = conn.execute(
        f"SELECT a.ai_score, a.topics FROM articles_raw a "
        f"JOIN media_sources m ON m.source_id = a.source_id "
        f"WHERE a.ai_score IS NOT NULL AND a.topics IS NOT NULL AND a.topics <> ''{date_clause}{exc}",
        (*params, *exp),
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


_SRC_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{2,40}$")


def _strip_source_suffix(title: str) -> str:
    """'Headline - Source Name' 패턴에서 매체명 접미사를 뗀다(유사도 계산용,
    표시용 제목은 그대로 둠). 같은 매체가 쓴 무관한 두 기사가 매체명 토큰
    때문에 유사하게 잡히는 오탐을 줄인다."""
    return _SRC_SUFFIX_RE.sub("", title or "")


def _overlap_ratio(a: set, b: set) -> float:
    """Jaccard와 동등한 대안 — 교집합/min(len). 짧은 헤드라인과 긴 헤드라인이
    같은 사건이어도 Jaccard는 낮게 나오는 문제를 보정(미진출국 피드 근접중복용)."""
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def _compute_top_news(conn, days: int | None = None, limit: int = 8) -> list[dict]:
    """전 거점 중요도 TOP = '오늘의 핵심 뉴스'.

    다양성: ① 제목 근접중복(같은 기사 재탕) 제외  ② 국가별 상한(도배 방지).
    후보 풀을 넉넉히 뽑아 위 규칙 적용 후 limit 만큼.
    """
    dc, params = _date_clause(days)
    # KB 미진출국은 제외 — 이 블록은 진출 11개 거점 횡단 요약용.
    exc, exp = db.exclude_countries_clause(config.NON_PRESENCE_CODES)
    rows = conn.execute(
        f"""SELECT a.ai_score, a.title, a.title_ko, a.summary_ko, a.summary_en,
                   a.topics, a.link, a.published_at, m.primary_country_code cc, m.media_name
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score >= ? AND a.duplicate_of IS NULL{dc}{exc}
            ORDER BY a.ai_score DESC, a.published_at DESC LIMIT 60""",
        (config.AI_SCORE_ACTIVE_THRESHOLD, *params, *exp),
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


_MOOD_TIERS = [  # (mood_level 하한, 아이콘, 라벨ko, 라벨en)
    (80, "☀️", "맑음",     "Clear"),
    (60, "⛅", "구름조금", "Partly Cloudy"),
    (40, "☁️", "흐림",     "Cloudy"),
    (20, "🌧️", "비",       "Rainy"),
    (0,  "🌀", "태풍",     "Storm"),
]


def _mood_tier(level: int) -> tuple[str, str, str]:
    for floor, icon, ko, en in _MOOD_TIERS:
        if level >= floor:
            return icon, ko, en
    return _MOOD_TIERS[-1][1:]


def _mood_level(arts: list, cc_indicators: list) -> tuple[int, str]:
    """국가별 무드 스코어(0~100, 높을수록 안정) + 추세(▲안정화/▼악화/→중립).

    country_section(top-5 이슈)·country_signals(11개 전부 신호판)가 공유하는
    핵심 계산 — 여기 로직을 바꾸면 두 화면 모두 바뀐다.
    arts 가 비어있으면(그날 ACTIVE 기사 없음) 중립값(70·→)을 반환한다.
    """
    if not arts:
        return 70, "→"
    n = len(arts)
    avg_score = sum(r["ai_score"] for r in arts) / n
    risk_n = sum(1 for r in arts if "RISK" in (r["topics"] or "").split(","))
    risk_pct = risk_n / n * 100
    score_norm = max(0.0, min(100.0, (avg_score - config.AI_SCORE_ACTIVE_THRESHOLD)
                               / (100 - config.AI_SCORE_ACTIVE_THRESHOLD) * 100))

    badness = []
    for ind in cc_indicators:
        if ind.get("change_pct") is None:
            continue
        pct = ind["change_pct"]
        # fx: 값 상승=현지통화 약세(나쁨) / index: 하락=나쁨 — 일간 변동폭이 보통
        # ±0.5%대라 ±2.5%를 0~100 스케일로 잡아야 추세가 실제로 갈린다.
        b = (50 + pct * 20) if ind["kind"] == "fx" else (50 - pct * 20)
        badness.append(max(0.0, min(100.0, b)))

    if badness:
        ind_avg = sum(badness) / len(badness)
        stress = 0.4 * risk_pct + 0.3 * score_norm + 0.3 * ind_avg
        trend = "▼" if ind_avg > 55 else ("▲" if ind_avg < 45 else "→")
    else:
        stress = 0.6 * risk_pct + 0.4 * score_norm
        trend = "→"   # 지표 없음 — 모멘텀 이력 스냅샷이 없어 중립 처리

    mood_level = round(max(0, min(100, 100 - stress)))
    return mood_level, trend


def _signal_band(mood_level: int) -> str:
    """mood_level → 3단계 신호(go/warn/stop). 임계는 config에서 조정."""
    if mood_level >= config.SIGNAL_GO_THRESHOLD:
        return "go"
    if mood_level >= config.SIGNAL_WARN_THRESHOLD:
        return "warn"
    return "stop"


def _compute_country_section(conn, days: int | None = 1, top_n: int = 5) -> list[dict]:
    """brief.html '국가별 오늘의 이슈' + '시장 무드 배지'가 공유하는 top-N 국가 블록.

    docs/신규기능_설계_20260821.md ④⑤. 대상 풀 = 진출국 11개(미진출국 제외).
    선정 = 그날 ACTIVE 기사의 ai_score 합(이슈 강도) 상위 top_n — 조용한 날은
    소형 거점도 자연히 진입한다. 신규 LLM 호출 없음: title_ko(랭킹 시 이미
    생성됨)를 이슈 문구·대표 키워드로 재활용한다.
    """
    dc, params = _date_clause(days)
    ph = ",".join("?" * len(_FLAGS))
    rows = conn.execute(
        f"""SELECT a.ai_score, a.title, a.title_ko, a.topics, m.primary_country_code cc
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score >= ? AND a.duplicate_of IS NULL
              AND m.primary_country_code IN ({ph}){dc}
            ORDER BY a.ai_score DESC""",
        (config.AI_SCORE_ACTIVE_THRESHOLD, *_FLAGS.keys(), *params),
    ).fetchall()

    by_cc: dict[str, list] = {}
    for r in rows:
        by_cc.setdefault(r["cc"], []).append(r)
    if not by_cc:
        return []

    intensity = {cc: sum(r["ai_score"] for r in arts) for cc, arts in by_cc.items()}
    top_ccs = sorted(by_cc, key=lambda cc: intensity[cc], reverse=True)[:top_n]

    indicators = _country_indicators(conn)

    out = []
    for cc in top_ccs:
        arts = by_cc[cc]
        n = len(arts)
        mood_level, trend = _mood_level(arts, indicators.get(cc, []))
        icon, mood_ko, mood_en = _mood_tier(mood_level)

        titles_ko = [r["title_ko"] or r["title"] for r in arts[:3]]
        titles_en = [r["title"] for r in arts[:3]]

        out.append(dict(
            cc=cc, flag=_FLAGS_ALL.get(cc, ""),
            mood_level=mood_level, mood_icon=icon, mood_ko=mood_ko, mood_en=mood_en,
            trend=trend,
            keyword=titles_ko[0] if titles_ko else "", keyword_en=titles_en[0] if titles_en else "",
            issues=titles_ko, issues_en=titles_en,
            basis_count=n,
        ))
    return out


def _index_short_label(symbol: str) -> str | None:
    for spec in config.INDICATOR_MAP.values():
        for idx in spec.get("indices", []):
            if idx["symbol"] == symbol:
                return idx.get("short") or idx.get("label")
    return None


def _compute_country_signals(conn, days: int | None = 1) -> list[dict]:
    """국가별 시장 신호 보드(진출국 11개 전부) — docs/mockups/국가신호_board.html.

    country_section(top-5 이슈)과 같은 _mood_level() 계산을 재사용하되, 제한
    없이 11개 전부 반환한다. 그날 기사가 없는 국가는 중립값(go)으로 표시.
    """
    dc, params = _date_clause(days)
    ph = ",".join("?" * len(_FLAGS))
    rows = conn.execute(
        f"""SELECT a.ai_score, a.title, a.title_ko, a.topics, m.primary_country_code cc
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score >= ? AND a.duplicate_of IS NULL
              AND m.primary_country_code IN ({ph}){dc}
            ORDER BY a.ai_score DESC""",
        (config.AI_SCORE_ACTIVE_THRESHOLD, *_FLAGS.keys(), *params),
    ).fetchall()

    by_cc: dict[str, list] = {cc: [] for cc in _FLAGS}
    for r in rows:
        by_cc[r["cc"]].append(r)

    indicators = _country_indicators(conn)

    out = []
    for cc, flag in _FLAGS.items():
        arts = by_cc[cc]
        cc_inds = indicators.get(cc, [])
        mood_level, _trend = _mood_level(arts, cc_inds)
        state = _signal_band(mood_level)

        idx_list, fx = [], None
        for ind in cc_inds:
            if ind["kind"] == "index":
                idx_list.append({
                    "label": _index_short_label(ind["symbol"]) or ind.get("label") or ind["symbol"],
                    "change_pct": ind.get("change_pct"),
                })
            elif ind["kind"] == "fx":
                fx = {"symbol": ind["symbol"], "value": ind.get("value"),
                      "note": _FX_NOTE.get(ind["symbol"])}

        if arts:
            keyword, keyword_en = arts[0]["title_ko"] or arts[0]["title"], arts[0]["title"]
        else:
            keyword, keyword_en = "오늘 특이사항 없음", "No notable news today"

        out.append(dict(
            cc=cc, flag=flag, name=_PRESENCE_NAMES_KO.get(cc, cc), name_en=_PRESENCE_NAMES_EN.get(cc, cc),
            state=state, mood_level=mood_level,
            index=idx_list, fx=fx,
            keyword=keyword, keyword_en=keyword_en,
        ))
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
        "daily_highlights": _daily_highlights(conn),
        "country_section": _compute_country_section(conn, days=days),
        "country_signals": _compute_country_signals(conn, days=days),
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
              "outlook_en", "keywords", "key_stat", "week_start", "week_end"]
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
            key_stat=g("key_stat"),
            week_start=g("week_start"), week_end=g("week_end"),
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


def _topic_bucket(rows: list, code: str, max_per: int, build) -> tuple[list, set]:
    """rows(이미 ai_score DESC 정렬)에서 topics 코드가 code인 기사를 최대 max_per개 골라
    (기사 dict 리스트, 국가코드 집합)으로 반환. build(row, codes)가 표시 필드를 만든다
    (진출/미진출 카드 필드가 달라 호출부에서 주입)."""
    arts, ccs = [], set()
    for r in rows:
        codes = [c.strip() for c in (r["topics"] or "").split(",") if c.strip()]
        if code not in codes:
            continue
        arts.append(build(r, codes))
        ccs.add(r["cc"])
        if len(arts) >= max_per:
            break
    return arts, ccs


def _pres_topic_article(r, codes: list) -> dict:
    return dict(cc=r["cc"], flag=_FLAGS_ALL.get(r["cc"], ""), presence="진출",
                src=r["media_name"], d=(r["published_at"] or "")[:10],
                t=r["title_ko"] or r["title"], q=r["summary_ko"] or "",
                k=r["kb_implication"] or "", q_en=r["summary_en"] or "",
                k_en=r["kb_implication_en"] or "",
                c=taxonomy.ui_string(codes), score=r["ai_score"], u=r["link"])


def _np_topic_article(r, codes: list) -> dict:
    meta = config.NON_PRESENCE_COUNTRIES.get(r["cc"], {})
    return dict(cc=r["cc"], flag=meta.get("flag", ""),
                cc_label=meta.get("name_ko", r["cc"]), cc_label_en=meta.get("name_en", r["cc"]),
                presence="미진출",
                src=r["media_name"], d=(r["published_at"] or "")[:10],
                t=r["title_ko"] or r["title"], q=r["summary_ko"] or "",
                k="", q_en=r["summary_en"] or "", k_en="",   # KB 시사점 없음(거점 없는 시장)
                c=taxonomy.ui_string(codes), score=r["ai_score"], u=r["link"])


def _compute_topics(conn, days: int | None = None, max_per: int = 15) -> list[dict]:
    """taxonomy 카테고리별로 ACTIVE 기사를 묶어 반환 — 진출국(presence=진출)·
    미진출국(presence=미진출) 카테고리를 함께 담은 하나의 리스트(같은 code가 그룹별로
    최대 2개 등장, UI는 presence로 필터). 미진출은 ACTIVE 임계 게이트 없이 ai_score
    랭킹만(비-거점 시장은 루브릭이 낮게 잡히므로 — _compute_non_presence와 동일 원칙)."""
    dc, params = _date_clause(days)

    exc, exp = db.exclude_countries_clause(config.NON_PRESENCE_CODES)
    pres_rows = conn.execute(
        f"""SELECT a.ai_score, a.title, a.title_ko, a.summary, a.summary_ko, a.kb_implication,
                   a.summary_en, a.kb_implication_en, a.topics, a.link, a.published_at,
                   m.primary_country_code cc, m.media_name
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score >= ? AND a.duplicate_of IS NULL AND a.ai_model LIKE '%:%'{dc}{exc}
            ORDER BY a.ai_score DESC""",
        (config.AI_SCORE_ACTIVE_THRESHOLD, *params, *exp),
    ).fetchall()

    np_rows = []
    if config.NON_PRESENCE_CODES:
        ph = ",".join("?" * len(config.NON_PRESENCE_CODES))
        np_rows = conn.execute(
            f"""SELECT a.ai_score, a.title, a.title_ko, a.summary_ko, a.summary_en, a.topics,
                       a.link, a.published_at, m.primary_country_code cc, m.media_name
                FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
                WHERE a.ai_score IS NOT NULL AND a.duplicate_of IS NULL AND a.ai_model LIKE '%:%'
                  AND m.primary_country_code IN ({ph}){dc}
                ORDER BY a.ai_score DESC""",
            (*config.NON_PRESENCE_CODES, *params),
        ).fetchall()

    categories = []
    for code, ui, label_ko, label_en in _TOPIC_CATS:
        arts, ccs = _topic_bucket(pres_rows, code, max_per, _pres_topic_article)
        if arts:
            categories.append(dict(code=code, ui=ui, label=label_ko, label_en=label_en,
                                   presence="진출", count=len(arts), ccs=sorted(ccs),
                                   articles=arts))
        arts_np, ccs_np = _topic_bucket(np_rows, code, max_per, _np_topic_article)
        if arts_np:
            categories.append(dict(code=code, ui=ui, label=label_ko, label_en=label_en,
                                   presence="미진출", count=len(arts_np), ccs=sorted(ccs_np),
                                   articles=arts_np))
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
