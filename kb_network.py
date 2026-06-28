"""
KB 글로벌 거점 네트워크 정의.

KB 시사점(kb_implication) 및 국가 브리핑 생성 시, LLM 프롬프트에
"이 국가는 KB의 어떤 거점인가"(지점/법인/자회사)를 주입하는 용도.

주의(CLAUDE.md): 국가 추가/변경 시 sources.yaml 과 이 파일을 함께 수정한다.
(prototype 레포에도 동명 모듈이 있으며, 본 레포는 AI 레이어 자체 구동을 위해 보유.)
"""
from __future__ import annotations

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
