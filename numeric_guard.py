"""
금액 검증 공용 유틸 (numeric_guard.py)

LLM이 생성·번역한 텍스트가 원문의 금액(USD·INR·거점 통화)을 그대로 보존했는지 확인한다.
단위 오변환(예: $133 billion → "133억 달러", 정답은 "1,330억 달러")을 잡기 위해
briefing.py(글로벌 핵심 출처검증)와 llm_translate.py(기사 번역)가 공용으로 쓴다.
"""
from __future__ import annotations

import re

_UNITS = {"": 1, "m": 1e6, "million": 1e6, "bn": 1e9,
          "billion": 1e9, "trillion": 1e12}
_KO_UNITS = {"조": 1e12, "억": 1e8, "천만": 1e7, "백만": 1e6, "만": 1e4}
# 숫자는 반드시 숫자로 시작·끝나야 한다("US$, " 같은 문장부호만 잡혀 float 변환이 터지는 것 방지).
# 뒤 lookahead는 되추적으로 숫자 일부만 잡는 것을 막는다("$186.5억"이 186으로 읽히던 문제).
_NUM = r"\d+(?:[,.]\d+)*(?![,.]?\d)"
_KO_PART = rf"({_NUM})\s*(조|억|천만|백만|만)"


def usd_values(text: str) -> list[float]:
    """텍스트에서 달러 금액을 전부 뽑아 절대값(USD) 리스트로 반환. 영어(달러기호·USD·billion 등
    단위어)와 한국어(조/억/만 달러, "1억9천만 달러" 같은 복합) 표기를 모두 인식한다."""
    text = text or ""
    values: list[float] = []
    # HK$·S$ 같은 다른 달러는 제외(US$는 포함) — 홍콩달러 금액이 USD로 잡혀 오판하지 않게
    for m in re.finditer(rf"(?:\bUS\$|(?<![A-Za-z])\$|\bUSD)\s*({_NUM})\s*(trillion|billion|million|bn|m)?\b",
                         text, re.I):
        values.append(float(m.group(1).replace(",", "")) * _UNITS[(m.group(2) or "").lower()])
    for m in re.finditer(
            rf"({_NUM})\s*[- ]?(trillion|billion|million|bn|m)\s*[- ]?(?:USD|US dollars?)\b",
            text, re.I):
        values.append(float(m.group(1).replace(",", "")) * _UNITS[m.group(2).lower()])
    for m in re.finditer(rf"((?:{_KO_PART}\s*)+)달러", text):
        values.append(sum(float(n.replace(",", "")) * _KO_UNITS[u]
                          for n, u in re.findall(_KO_PART, m.group(1))))
    for m in re.finditer(rf"\$\s*({_NUM})\s*(조|억)", text):
        values.append(float(m.group(1).replace(",", "")) * _KO_UNITS[m.group(2)])
    return values


_INR_UNITS = {"": 1, "lakh": 1e5, "crore": 1e7, "cr": 1e7, "million": 1e6, "mn": 1e6, "m": 1e6,
              "billion": 1e9, "bn": 1e9, "trillion": 1e12, "lakh crore": 1e12}
_INR_UNIT_RE = r"(lakh crore|lakh|crore|cr|trillion|billion|million|bn|mn|m)"


def inr_values(text: str) -> list[float]:
    """텍스트에서 인도 루피 금액을 뽑아 절대값(INR) 리스트로 반환. 영어(Rs/₹/INR + crore·lakh 등)와
    한국어(루피, "2조 2,568억 루피" 같은 복합) 표기를 인식한다. 인도네시아 루피아(Rp·루피아)는 제외."""
    text = text or ""
    values: list[float] = []
    # Rs 10,000 crore / ₹2,000 / Rs 22,568-crore
    for m in re.finditer(rf"(?:\bRs\.?|₹|\bINR)\s*({_NUM})(?:\s*-?\s*{_INR_UNIT_RE}\b)?", text, re.I):
        values.append(float(m.group(1).replace(",", "")) * _INR_UNITS[(m.group(2) or "").lower()])
    # 통화기호 없이 쓰는 인도식 단위(10,000 crore rupees, 38.5 lakh) — 인도 기사에서 사실상 루피
    for m in re.finditer(rf"(?<![\w₹.,])({_NUM})\s*-?\s*(crore|lakh)\b", text, re.I):
        if not re.search(r"(?:Rs\.?|₹|INR)\s*$", text[:m.start()], re.I):
            values.append(float(m.group(1).replace(",", "")) * _INR_UNITS[m.group(2).lower()])
    for m in re.finditer(rf"((?:{_KO_PART}\s*)+)루피(?!아)", text):
        values.append(sum(float(n.replace(",", "")) * _KO_UNITS[u]
                          for n, u in re.findall(_KO_PART, m.group(1))))
    for m in re.finditer(rf"(?<![\d,.조억만])({_NUM})\s*루피(?!아)", text):
        values.append(float(m.group(1).replace(",", "")))
    return values


# 거점 통화(2026-10-07 HK 브리핑 'HK$500 billion → 500억 홍콩달러' 10배 오류로 확대).
# 코드: (영문 앞 표기, 영문 뒤 표기, 한국어 표기). 원화(원)는 환산값이라 대조하지 않는다.
_FX = {
    "HKD": (r"HK\$|\bHKD", r"Hong Kong dollars?", r"홍콩\s?달러"),
    "SGD": (r"(?<![A-Za-z])S\$|\bSGD", r"Singapore dollars?", r"싱가포르\s?달러"),
    "CNY": (r"\bRMB|\bCNY", r"yuan|renminbi", r"위안"),
    "JPY": (r"¥|\bJPY", r"yen", r"엔(?!화)"),
    "IDR": (r"\bRp\.?|\bIDR", r"rupiah", r"루피아"),
    "VND": (r"\bVND", r"dong", r"동(?![가-힣])"),
    "THB": (r"฿|\bTHB", r"baht", r"바트"),
    "GBP": (r"£|\bGBP", r"pounds?(?: sterling)?", r"파운드"),
    "EUR": (r"€|\bEUR", r"euros?", r"유로"),
    "PHP": (r"₱|\bPHP", r"pesos?", r"페소"),
    "MYR": (r"\bRM|\bMYR", r"ringgit", r"링깃"),
    "BDT": (r"৳|\bBDT|\bTk\.?", r"taka", r"타카"),
}
# 방글라데시 타카도 인도처럼 crore·lakh로 쓴다(Tk 20,000 crore = 2,000억 타카).
_EN_UNIT_RE = r"(lakh crore|trillion|billion|million|crore|lakh|bn|mn|tn|cr|b|m)"
_EN_UNITS = {**_UNITS, "mn": 1e6, "tn": 1e12, "b": 1e9,
             "crore": 1e7, "cr": 1e7, "lakh": 1e5, "lakh crore": 1e12}


def fx_values(text: str) -> dict[str, list[float]]:
    """USD·INR 외 거점 통화 금액을 {통화코드: [값]}으로 반환. 영어(HK$500 billion, 10 billion yuan)와
    한국어(5,000억 홍콩달러, 1.2조 엔) 표기를 인식한다. 금액이 없는 통화는 키가 없다."""
    text = text or ""
    out: dict[str, list[float]] = {}
    for code, (pre, post, ko) in _FX.items():
        vals: list[float] = []
        for m in re.finditer(rf"(?:{pre})\s*({_NUM})(?:\s*-?\s*{_EN_UNIT_RE}\b)?", text, re.I):
            vals.append(float(m.group(1).replace(",", "")) * _EN_UNITS[(m.group(2) or "").lower()])
        for m in re.finditer(rf"({_NUM})\s*-?\s*(?:{_EN_UNIT_RE}\s+)?(?:{post})\b", text, re.I):
            vals.append(float(m.group(1).replace(",", "")) * _EN_UNITS[(m.group(2) or "").lower()])
        for m in re.finditer(rf"((?:{_KO_PART}\s*)+)(?:{ko})", text):
            vals.append(sum(float(n.replace(",", "")) * _KO_UNITS[u]
                            for n, u in re.findall(_KO_PART, m.group(1))))
        for m in re.finditer(rf"(?<![\d,.조억만])({_NUM})\s*(?:{ko})", text):
            vals.append(float(m.group(1).replace(",", "")))
        if vals:
            out[code] = vals
    return out


def _mismatch(src: list[float], out: list[float]) -> bool:
    if not src or not out:
        return False
    return any(not any(abs(o - s) <= max(1, abs(s)) * 0.02 for s in src) for o in out)


def usd_mismatch(source_text: str, output_text: str) -> bool:
    """output_text의 금액 중 source_text 어느 금액과도 2% 이내로 맞지 않는 게 있으면 True.
    둘 중 하나라도 금액이 없으면(비교 불가) False."""
    return _mismatch(usd_values(source_text), usd_values(output_text))


def amount_mismatch(source_text: str, output_text: str) -> bool:
    """USD·INR·거점 통화 금액을 통화별로 각각 대조한다(usd_mismatch 확장). 한쪽이라도 어긋나면 True."""
    if (usd_mismatch(source_text, output_text)
            or _mismatch(inr_values(source_text), inr_values(output_text))):
        return True
    src, out = fx_values(source_text), fx_values(output_text)
    return any(_mismatch(src.get(code, []), vals) for code, vals in out.items())


def source_amount_conflicts(texts: list[str]) -> dict[str, list[float]]:
    """여러 출처가 같은 사건의 단일 금액을 서로 다르게 보도하면 통화별 값을 반환한다.

    한 출처 안에 여러 금액이 있으면 총액·부분액을 함께 설명하는 기사일 수 있어 자동
    충돌 판정에서 제외한다. 각 출처에서 단일 금액만 명확한 경우에만 2% 허용오차로
    비교해 오탐보다 누락을 택한다.
    """
    conflicts: dict[str, list[float]] = {}
    for currency, extract in (("USD", usd_values), ("INR", inr_values)):
        singles = []
        for text in texts:
            values = extract(text or "")
            unique = []
            for value in values:
                if not any(abs(value - old) <= max(1, abs(old)) * 0.02 for old in unique):
                    unique.append(value)
            if len(unique) == 1:
                singles.append(unique[0])
        distinct = []
        for value in singles:
            if not any(abs(value - old) <= max(1, abs(old)) * 0.02 for old in distinct):
                distinct.append(value)
        if len(distinct) >= 2:
            conflicts[currency] = sorted(distinct)
    return conflicts
