"""
배포 차단 금액 불일치 AI 재보정 (amount_repair.py)

display_audit --hold-out이 남긴 차단 항목 중 한국어 금액이 영문과 다른 필드만, 코드가 계산한
정확한 한국어 금액(numeric_guard.korean_amount_hints)을 지정해 AI에 1회 고치게 한다. 고친 결과가
금액 검증을 통과할 때만 저장하고, 못 고친 건은 deploy_web.sh가 보류(제외) export로 넘긴다.
(2026-10-26 — 금액 감사 1~2건 때문에 화면 전체 배포가 사흘 멈춤)
"""
from __future__ import annotations

import json
import re

import numeric_guard

# 카드 필드 → DB 한국어 컬럼. (q_en, t_en)처럼 영어끼리 비교한 건 고칠 한국어가 없어 제외.
_KO_COL = {"t": "title_ko", "q": "summary_ko", "expanded_summary": "expanded_summary"}
_BRIEF_FIELDS = {"daily": ("summary",), "weekly": ("summary", "outlook", "key_stat", "issues")}

SYSTEM = ("You fix monetary amounts in a Korean text so they match the English source. "
          "Use the exact Korean amount notations given. Change only the wrong amount expressions and keep "
          "every other part of the Korean text exactly as it is. Output only the corrected Korean text.")


def fix_korean(provider, en: str, ko: str) -> str | None:
    """금액만 고친 한국어. 결과가 여전히 영문과 금액이 다르거나 본문이 크게 달라지면 None."""
    user = f"English source:\n{en}\n\n{numeric_guard.korean_amount_hints(en)}\n\nKorean text:\n{ko}"
    out = (provider.complete(SYSTEM, user, max_tokens=max(512, len(ko) * 3)) or "").strip()
    if not out or not re.search(r"[가-힣]", out) or not 0.7 <= len(out) / max(1, len(ko)) <= 1.3:
        return None
    return None if numeric_guard.amount_mismatch(en, out) else out


def repair(conn, hold: dict, provider=None) -> list[str]:
    """hold의 기사·브리핑 금액 불일치를 고쳐 DB에 저장하고 처리 로그(줄 목록)를 반환."""
    if provider is None:
        from llm_provider import get_provider
        provider = get_provider("fast", use_batch=False)
    log = []

    def attempt(label, en, ko):
        new = fix_korean(provider, en, ko)
        detail = ""
        if new:   # 바뀐 곳 앞뒤만 보여준다
            i = next((n for n, (a, b) in enumerate(zip(ko, new)) if a != b), min(len(ko), len(new)))
            detail = f"\n    전: …{ko[max(0, i - 40):i + 60]}\n    후: …{new[max(0, i - 40):i + 60]}"
        log.append(f"{'수정' if new else '실패'} {label}{detail}")
        return new

    for link, pairs in (hold.get("links") or {}).items():
        for en_key, ko_key, en, ko in pairs:
            col = _KO_COL.get(ko_key)
            if not col or not conn.execute(
                    f"SELECT 1 FROM articles_raw WHERE link=? AND {col}=?", (link, ko)).fetchone():
                log.append(f"건너뜀 {en_key}↔{ko_key}(고칠 한국어 없음) · {link[:70]}")
                continue
            new = attempt(f"{col} · {link[:70]}", en, ko)
            if new:
                conn.execute(f"UPDATE articles_raw SET {col}=? WHERE link=? AND {col}=?", (new, link, ko))

    for cc, typ in hold.get("briefs") or []:
        row = conn.execute("SELECT * FROM country_briefings WHERE cc=? AND briefing_type=? "
                           "ORDER BY briefing_date DESC, generated_at DESC LIMIT 1", (cc, typ)).fetchone()
        if not row:
            continue
        for f in _BRIEF_FIELDS.get(typ, ()):
            ko, en = row[f] or "", row[f + "_en"] or ""
            if f == "issues":
                ko_l, en_l = json.loads(ko or "[]"), json.loads(en or "[]")
                fixed = [attempt(f"{typ} {cc} issues[{i}]", e, k)
                         if numeric_guard.amount_mismatch(e, k) else None for i, (k, e) in enumerate(zip(ko_l, en_l))]
                if any(fixed):
                    new = json.dumps([x or k for x, k in zip(fixed, ko_l)] + ko_l[len(fixed):], ensure_ascii=False)
                else:
                    new = None
            else:
                new = attempt(f"{typ} {cc} {f}", en, ko) if numeric_guard.amount_mismatch(en, ko) else None
            if new:
                conn.execute(f"UPDATE country_briefings SET {f}=? WHERE briefing_id=?", (new, row["briefing_id"]))
    conn.commit()
    return log
