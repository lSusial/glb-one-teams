"""
카테고리 후처리 공용 유틸 (category_guard.py)

지정학(GEO) 분류인데 실제로는 지정학 신호(전쟁·제재·관세·안보 등)가 본문에
하나도 없는 기사를 걸러낸다. eval/display_audit.py(사후 감사, CATEGORY_MISMATCH)와
fix_ungrounded_geo()(DB 보정)가 같은 판정 로직을 공유한다.

실측(9/23~10/15 누적): 1MDB-도이치뱅크 소송·제인스트리트 SEBI 조사·KB 협동조합·
기린 공정위 압수수색·중국 디지털대출업체 회장 구금·OCC 디뱅킹 조사 등 — 전부 국가 간
갈등·제재·안보·무역분쟁과 무관한 기업소송·국내규제·부패수사 뉴스에 GEO가 붙었다.
"""
from __future__ import annotations

import re

GEO_SIGNAL_RE = re.compile(
    r"\b(?:wars?|conflicts?|sanction\w*|tariff\w*|election\w*|military|army|troops|coup|geopolit\w*|"
    r"diplomat\w*|missiles?|invasion|invad\w*|borders?|terror\w*|nuclear|embargo|unrest|protest\w*|summit|"
    r"bilateral|trade (?:war|tension\w*|deal|talks|dispute)|tensions?|attacks?|ceasefire|hostages?|riots?|"
    r"national security|sovereign\w*|iran\w*|israel\w*|russia\w*|ukrain\w*|gaza|hormuz|"
    r"cyberattacks?|espionage|crackdown|scams?|money.laundering|export (?:controls?|restrictions?|rules|bans?)|"
    r"truce|extraterritorial)\b", re.I)
GOVERNANCE_RE = re.compile(
    r"\b(?:takeover|stake|shareholding|shareholders?|ownership|controlling|acquisition|acquire|"
    r"governance|buyback|repurchase|retake)\b", re.I)


def ungrounded_geo_reason(text: str) -> str | None:
    """GEO로 분류하기에 영문 본문에 지정학 신호가 없으면 사유 문자열, 있으면(또는 본문이
    비어 판단 불가하면) None."""
    text = text or ""
    if not text.strip() or GEO_SIGNAL_RE.search(text):
        return None
    if GOVERNANCE_RE.search(text):
        return "신호 없음 — 지분·지배구조 성격(policy/markets 검토)"
    return "신호 없음"


def strip_ungrounded_geo(topics_str: str, text: str) -> str | None:
    """topics(예: 'GEO,ECONOMY')에서 근거 없는 GEO를 뗀다. 바뀔 게 없으면 None, 바뀌면
    새 topics 문자열(GEO 뺀 나머지, 다 빠지면 ECONOMY로 폴백 — 빈 topics는 화면에서
    카테고리 미표시로 깨짐)을 반환."""
    codes = [c for c in (topics_str or "").split(",") if c]
    if "GEO" not in codes or ungrounded_geo_reason(text) is None:
        return None
    rest = [c for c in codes if c != "GEO"]
    return ",".join(rest) if rest else "ECONOMY"


def fix_ungrounded_geo(conn, days: int | None = None) -> dict:
    """ai_score가 있고 topics에 GEO가 있는 기사를 스캔해 근거 없는 GEO를 뗀다.
    랭킹 직후(main.py ai) 자동 보정 + 기존 백로그 1회성 정리 양쪽에 쓴다."""
    import db
    dc, params = db.days_clause_now(days) if days else ("", [])
    rows = conn.execute(
        f"""SELECT a.article_id, a.topics, a.title_en, a.summary_en, a.expanded_summary_en
            FROM articles_raw a
            WHERE a.ai_score IS NOT NULL AND a.topics LIKE '%GEO%'{dc}""",
        params,
    ).fetchall()
    cur = conn.cursor()
    fixed = 0
    for r in rows:
        text = " ".join((r["title_en"] or "", r["summary_en"] or "", r["expanded_summary_en"] or ""))
        new_topics = strip_ungrounded_geo(r["topics"], text)
        if new_topics is None:
            continue
        cur.execute("UPDATE articles_raw SET topics = ? WHERE article_id = ?",
                     (new_topics, r["article_id"]))
        fixed += 1
    conn.commit()
    return {"checked": len(rows), "fixed": fixed}
