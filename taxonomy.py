"""
taxonomy.yaml 로더 + 주제 분류 헬퍼 (축 C / 축 D).

- AI(llm_ranker)가 topics 를 채울 때 허용 코드 집합·시드 후보를 제공
- export(UI)가 코드 → ui 필터 키 매핑에 사용

설계: 데이터_AI_카테고리_설계.md §3
"""
from __future__ import annotations

import functools
import re

import yaml

import config


@functools.lru_cache(maxsize=1)
def load() -> dict:
    """taxonomy.yaml 파싱 결과(캐시)."""
    with open(config.TAXONOMY, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@functools.lru_cache(maxsize=1)
def _topics() -> list[dict]:
    return load().get("topics", [])


def codes() -> list[str]:
    """유효 주제 코드 목록. 예: ['MARKET','BANKING','DIGITAL','ESG','RISK']."""
    return [t["code"] for t in _topics()]


def code_set() -> set[str]:
    return set(codes())


def label(code: str, lang: str = "ko") -> str:
    for t in _topics():
        if t["code"] == code:
            return t.get("label", {}).get(lang, code)
    return code


def ui_key(code: str) -> str:
    """주제 코드 → UI 현지언론 필터 키(finance/digital/esg/risk)."""
    for t in _topics():
        if t["code"] == code:
            return t.get("ui", code.lower())
    return code.lower()


def ui_string(codes_in: list[str]) -> str:
    """코드 리스트 → UI c 문자열. 예: ['ESG','RISK'] → 'esg risk' (중복 제거)."""
    seen: list[str] = []
    for c in codes_in:
        k = ui_key(c)
        if k not in seen:
            seen.append(k)
    return " ".join(seen)


def max_topics() -> int:
    return int(load().get("max_topics_per_article", 3))


def validate(codes_in: list[str]) -> list[str]:
    """유효 코드만 남기고(대문자 정규화) 최대 개수로 제한."""
    valid = code_set()
    out: list[str] = []
    for c in codes_in or []:
        cu = str(c).strip().upper()
        if cu in valid and cu not in out:
            out.append(cu)
    return out[: max_topics()]


def seed_candidates(text: str) -> list[str]:
    """시드 키워드 매칭으로 후보 주제 코드 산출(1차 규칙, LLM 보조/폴백용)."""
    low = (text or "").lower()
    hits: list[str] = []
    for t in _topics():
        for kw in t.get("seeds", []):
            if re.search(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])", low):
                hits.append(t["code"])
                break
    return hits[: max_topics()]


def prompt_reference() -> str:
    """LLM 프롬프트에 넣을 주제 코드 설명 블록."""
    lines = []
    for t in _topics():
        seeds = ", ".join(t.get("seeds", [])[:6])
        lines.append(f"- {t['code']} ({t.get('label', {}).get('ko', '')}): {seeds}")
    return "\n".join(lines)
