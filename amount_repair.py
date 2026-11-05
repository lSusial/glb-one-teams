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
_BRIEF_FIELDS = {"daily": ("summary",), "weekly": ("summary", "outlook", "key_stat", "issues", "keywords")}

SYSTEM = ("You fix monetary amounts in a Korean text so they match the English source. "
          "Use the exact Korean amount notations given. Change only the wrong amount expressions and keep "
          "every other part of the Korean text exactly as it is. Output only the corrected Korean text.")

_BRIEF_SYSTEM = (
    "Translate the provided English briefing fields faithfully into Korean. English is canonical. "
    "The current Korean text has missing/extra monetary amounts or mismatched array items. "
    "Add or omit no facts or figures from English; remove Korean-only facts and amounts. "
    "Keep already matching Korean array items unchanged. Preserve array length and item order. "
    "Use formal Korean and the supplied monetary hints; keep English monetary notation if unsure. "
    "Return ONLY JSON with the requested field names (no _ko/_en suffixes). "
    "Never return or modify English fields."
)


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
        fields = _BRIEF_FIELDS.get(typ, ())
        english, korean = {}, {}
        for field in fields:
            for target, column in ((english, field + '_en'), (korean, field)):
                raw = row[column] or ''
                target[field] = json.loads(raw or '[]') if field in ('issues', 'keywords') else raw
        differences = numeric_guard.bilingual_amount_issues(english, korean)
        bad_fields = [field for field in fields if numeric_guard.bilingual_amount_issues(
            {field: english[field]}, {field: korean[field]})]
        if not bad_fields:
            continue
        source = {field: english[field] for field in bad_fields}
        user = json.dumps({'english': source,
                           'current_korean': {field: korean[field] for field in bad_fields},
                           'validation_errors': differences}, ensure_ascii=False)
        user += '\n' + numeric_guard.korean_amount_hints(json.dumps(source, ensure_ascii=False))
        result = provider.complete_json(_BRIEF_SYSTEM, user, max_tokens=6000) or {}
        candidate = dict(korean)
        valid = isinstance(result, dict)
        for field in bad_fields:
            value = result.get(field) if isinstance(result, dict) else None
            if isinstance(english[field], list):
                valid = valid and isinstance(value, list) and all(isinstance(item, str) for item in value)
                if valid and isinstance(korean[field], list) and len(value) == len(korean[field]) == len(english[field]):
                    # 같은 배열에서 이미 검증된 항목은 모델이 다시 써도 기존 문구를 유지한다.
                    value = [old if not numeric_guard.bilingual_amount_issues({'item': en}, {'item': old}) else new
                             for en, old, new in zip(english[field], korean[field], value)]
            else:
                valid = valid and isinstance(value, str)
            candidate[field] = value
        if not valid or numeric_guard.bilingual_amount_issues(english, candidate):
            log.append(f"실패 {typ} {cc} — 재번역 검증 불일치, 기존 브리핑 보존")
            continue
        for field in bad_fields:
            value = candidate[field]
            stored = json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
            conn.execute(f"UPDATE country_briefings SET {field}=? WHERE briefing_id=?", (stored, row['briefing_id']))
            log.append(f"수정 {typ} {cc} {field} — 영문 기준 재번역·전체 필드 검증 통과")
    conn.commit()
    return log
