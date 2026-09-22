"""
금액 검증 공용 유틸 (numeric_guard.py)

LLM이 생성·번역한 텍스트가 원문의 금액(USD)을 그대로 보존했는지 확인한다.
단위 오변환(예: $133 billion → "133억 달러", 정답은 "1,330억 달러")을 잡기 위해
briefing.py(글로벌 핵심 출처검증)와 llm_translate.py(기사 번역)가 공용으로 쓴다.
"""
from __future__ import annotations

import re

_UNITS = {"": 1, "m": 1e6, "million": 1e6, "bn": 1e9,
          "billion": 1e9, "trillion": 1e12}


def usd_values(text: str) -> list[float]:
    """텍스트에서 달러 금액을 전부 뽑아 절대값(USD) 리스트로 반환. 영어(달러기호·billion 등
    단위어)와 한국어(조/억 달러) 표기를 모두 인식한다."""
    text = text or ""
    values: list[float] = []
    for m in re.finditer(r"\$\s*([\d,.]+)\s*(trillion|billion|million|bn|m)?\b", text, re.I):
        values.append(float(m.group(1).replace(",", "")) * _UNITS[(m.group(2) or "").lower()])
    for m in re.finditer(
            r"([\d,.]+)\s*[- ]?(trillion|billion|million|bn|m)\s*[- ]?(?:USD|US dollars?)\b",
            text, re.I):
        values.append(float(m.group(1).replace(",", "")) * _UNITS[m.group(2).lower()])
    for m in re.finditer(r"([\d,.]+)\s*(조|억)\s*달러", text):
        values.append(float(m.group(1).replace(",", "")) * (1e12 if m.group(2) == "조" else 1e8))
    return values


def usd_mismatch(source_text: str, output_text: str) -> bool:
    """output_text의 금액 중 source_text 어느 금액과도 2% 이내로 맞지 않는 게 있으면 True.
    둘 중 하나라도 금액이 없으면(비교 불가) False."""
    src = usd_values(source_text)
    out = usd_values(output_text)
    if not src or not out:
        return False
    return any(not any(abs(o - s) <= max(1, abs(s)) * 0.02 for s in src) for o in out)
