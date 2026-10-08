"""
KB 글로벌 거점 네트워크 정의.

KB 시사점(kb_implication) 및 국가 브리핑 생성 시, LLM 프롬프트에
"이 국가는 KB의 어떤 거점인가"(지점/법인/자회사)를 주입하는 용도.

주의(CLAUDE.md): 국가 추가/변경 시 sources.yaml 과 이 파일을 함께 수정한다.
(prototype 레포에도 동명 모듈이 있으며, 본 레포는 AI 레이어 자체 구동을 위해 보유.)
"""
from __future__ import annotations

import re

# country_code → 거점 정보
KB_NETWORK: dict[str, dict] = {
    "GB": {"city": "런던",      "type": "지점",   "entity": "KB 런던지점"},
    "US": {"city": "뉴욕",      "type": "지점",   "entity": "KB 뉴욕지점"},
    "HK": {"city": "홍콩",      "type": "지점",   "entity": "KB 홍콩지점"},
    "CN": {"city": "베이징",    "type": "법인",   "entity": "KB 중국법인"},
    "JP": {"city": "도쿄",      "type": "지점",   "entity": "KB 도쿄지점"},
    "SG": {"city": "싱가포르",  "type": "지점",   "entity": "KB 싱가포르지점"},
    "IN": {"city": "구르구람",  "type": "지점",   "entity": "KB 구르구람지점"},
    "VN": {"city": "하노이",    "type": "법인",   "entity": "KB 베트남법인"},
    "MM": {"city": "양곤",      "type": "사무소", "entity": "KB 양곤사무소"},
    "ID": {"city": "자카르타",  "type": "자회사", "entity": "PT Bank KB Indonesia Tbk (KBI은행)"},
    "KH": {"city": "프놈펜",    "type": "자회사", "entity": "KB 프라삭은행 (KB Prasac Bank)"},
    # TH·LA: 2026-08-28 진출국 편입(제품 기준) — 실제 KB 지점·법인 없음, 관심시장으로 관찰만.
    "TH": {"city": "방콕",      "type": "관심시장", "entity": "KB 태국 관심시장(지점 없음)"},
    "LA": {"city": "비엔티안",  "type": "관심시장", "entity": "KB 라오스 관심시장(지점 없음)"},
}

# 자회사(별도 IR·경영공시 대상) 국가 코드
SUBSIDIARY_COUNTRIES = tuple(cc for cc, v in KB_NETWORK.items() if v["type"] == "자회사")


def context_for(cc: str | None) -> str:
    """단일 국가 거점 설명 한 줄. 알 수 없으면 글로벌로 처리."""
    info = KB_NETWORK.get((cc or "").upper())
    if not info:
        return "KB 글로벌 본점 관점(특정 거점 없음)"
    return f"{info['entity']} — {info['city']} 소재 {info['type']}"


def all_context() -> str:
    """전체 거점 요약(프롬프트 주입용)."""
    return "; ".join(
        f"{cc}={v['entity']}({v['type']})" for cc, v in KB_NETWORK.items()
    )


# 국가 언급 앵커(국명·형용사·수도·대표 기관·통화·지수) — 영문 기준본(title_en·summary_en)에서 찾는다.
# 주제국가가 비어 매체 국적으로 떨어진 기사가 그 나라를 한 번도 언급하지 않으면 해당 국가 기사가 아니다
# (2026-10-08 Straits Times '호르무즈 유조선 공격'이 SG 탭에 노출). 9/23~10/8 배포본 134건 측정: 오탐 0.
_MENTION = {
    "GB": r"brit|\bu\.?k\.?\b|united kingdom|england|london|scotland|\bboe\b|\bfca\b|\bpra\b|ftse|sterling|\bgilts?\b",
    "US": (r"\bu\.?s\.?\b|united states|america|washington|new york|wall street|\bfed\b|federal|treasury|"
           r"\bsec\b|fdic|\bocc\b|\bhud\b|cfpb|nasdaq|s&p|\bdow\b|trump|white house|congress"),
    "HK": r"hong kong|hkma|hang seng|hkex",
    "CN": r"china|chinese|beijing|shanghai|shenzhen|pboc|yuan|renminbi|\brmb\b",
    "JP": r"japan|tokyo|\bboj\b|bank of japan|nikkei|topix|\byen\b",
    "SG": r"singapore|\bmas\b|\bsgx\b|straits",
    "IN": r"india|\brbi\b|sebi|mumbai|delhi|rupee|sensex|nifty|\bnse\b|\bbse\b",
    "VN": r"vietnam|viet nam|hanoi|ho chi minh|\bsbv\b",
    "MM": r"myanmar|burma|yangon|naypyi",
    "ID": r"indonesia|jakarta|\bojk\b|rupiah|\bidx\b|bukopin|\bkbi\b|danantara",
    "KH": r"cambodia|phnom penh|prasac|\briel",
    "TH": r"thai|bangkok|baht",
    "LA": r"\blaos?\b|\blao\b|vientiane",
}


def mentions_country(cc: str | None, text: str | None) -> bool:
    """text가 진출국 cc를 언급하는지. 앵커가 없는 국가(미진출국·GLOBAL)는 판단하지 않고 True."""
    pat = _MENTION.get((cc or "").upper())
    return True if pat is None else bool(re.search(pat, text or "", re.I))


def route_country(cc: str | None, text: str | None) -> str | None:
    """표시용 국가 = cc, 단 진출국인데 text가 그 나라를 언급하지 않으면 GLOBAL.
    주제국가가 비어 매체 국가로 폴백된 GLOBAL 사건(Reuters UK의 이탈리아 은행 M&A, Straits Times의
    호르무즈 기사)이 매체 국가 이름으로 홈·국가 블록에 뜨지 않게 한다. text가 None이면 판단하지 않는다."""
    if text is None or mentions_country(cc, text):
        return cc
    return "GLOBAL"
