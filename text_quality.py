"""번역에서 사라지는 수치 기준과 한국어의 가나 혼입 검사. 외부 호출 없음."""
import re

KANA_RE = re.compile(r'[\u3040-\u30ff]')
_MONTHS = 'January February March April May June July August September October November December'.split()
_MOVE = re.compile(r'\b(?:down|up|fell|fall|drop|rose|rise|gain|lost|declin|slid|slump|plung|tumb|grew|growth|increas|decreas|jump|surg)', re.I)
_PCT = re.compile(r'\d+(?:\.\d+)?\s*%')


def _attached(pattern: str, text: str, pct):
    """비교 기준은 같은 문장의 가장 가까운 등락률에만 연결한다."""
    percentages = list(_PCT.finditer(text or ''))
    for marker in re.finditer(pattern, text or '', re.I):
        def distance(value):
            return max(marker.start()-value.end(), value.start()-marker.end(), 0)
        owner = min(percentages, key=distance, default=None)
        # 경제 캘린더의 '(YoY) above expectations (...) ... Actual (...)'는
        # 맨 앞 비교 기준을 예상치·실제치가 공유한다.
        shared_calendar = (percentages and marker.end() <= percentages[0].start()
                           and re.search(r'\b(?:above|below|matches)\s+(?:expectations?|forecasts?).*\bactual\b', text, re.I))
        if owner is None or (owner.start() != pct.start() and not shared_calendar) or distance(pct) > 90:
            continue
        gap = text[min(marker.end(), pct.end()):max(marker.start(), pct.start())]
        if not re.search(r'[.!?;]\s+|\n', gap):
            yield marker


def period_issues(source: str, output: str) -> list[str]:
    """출력에 남긴 등락률의 명시적 월간/전년/전월 기준이 사라졌는지 검사한다."""
    issues = []
    for m in _PCT.finditer(source or ''):
        matches = [p for p in _PCT.finditer(output or '') if p[0].replace(' ', '') == m[0].replace(' ', '')]
        if not matches:
            continue  # 제목에서 수치 자체를 생략하는 것은 허용한다.
        def retained(pattern):
            return any(any(_attached(pattern, output, p)) for p in matches)
        context = source[max(0, m.start()-90):m.end()+65]
        if not _MOVE.search(context):
            continue
        # 정책금리의 수준은 같은 문장에 다른 성장률이 있어도 등락률이 아니다.
        if re.search(r'\b(?:policy|interest|benchmark) rates?\s+(?:(?:is|was|were|held|set|kept)\s+)*(?:at|to|of)\s*$',
                     source[max(0,m.start()-70):m.start()], re.I):
            continue
        for pattern, target, label in (
            (r'year[- ]on[- ]year|\byoy\b', r'전년|year[- ]on[- ]year|\byoy\b', '전년 대비'),
            (r'month[- ]on[- ]month|\bmom\b', r'전월|month[- ]on[- ]month|\bmom\b', '전월 대비'),
            (r'quarter[- ]on[- ]quarter|\bqoq\b', r'전분기|quarter[- ]on[- ]quarter|\bqoq\b', '전분기 대비'),
        ):
            if any(_attached(pattern, source, m)) and not retained(target):
                issues.append(label)
        for marker in _attached(r'\bin\s+Q([1-4])\b', source, m):
            quarter = marker[1]
            if not retained(rf'{quarter}분기|\bQ{quarter}\b'):
                issues.append(f'{quarter}분기')
        months = '|'.join(_MONTHS)
        range_pattern = rf'\b(?:in|during|for|over)\s+(?:the\s+)?({months})\s*(?:[-–—]|to|through)\s*({months})\b'
        ranges = list(_attached(range_pattern, source, m))
        for marker in ranges:
            first, last = [_MONTHS.index(v.capitalize())+1 for v in marker.groups()]
            target = (rf'(?<!\d){first}(?:월)?\s*(?:[~\-–—]|월부터)\s*{last}월|'
                      rf'\b{marker[1]}\s*(?:[-–—]|to|through)\s*{marker[2]}\b')
            if not retained(target):
                issues.append(f'{first}~{last}월 누적')
        for n, month in enumerate(_MONTHS, 1):
            # 날짜(October 15)와 월간 실적(in October)을 구분한다.
            for marker in _attached(rf'\b(?:in|during|for|over)\s+{month}\b(?!\s+\d{{1,2}}\b)', source, m):
                if any(r.start() <= marker.start() < r.end() for r in ranges):
                    continue
                if not retained(rf'(?<!\d){n}월|\b{month}\b|월간|한\s*달'):
                    issues.append(f'{n}월 월간')
    return list(dict.fromkeys(issues))


def korean_issues(source: str, output: str, *, headline: bool = False) -> list[str]:
    errors = ['한국어에 가나 혼입'] if KANA_RE.search(output or '') else []
    if headline:
        errors += [f'수치 기간 누락: {p}' for p in period_issues(source, output)]
    return errors


def has_briefing_evidence(title: str, summary: str) -> bool:
    """제목 재탕·구체 정보 부재를 명시한 요약은 브리핑 근거로 쓰지 않는다."""
    if not (summary or '').strip():
        return False
    summary = re.sub(r'\s+', ' ', summary).strip()
    normalize = lambda value: re.sub(r'[\W_]+', ' ', value or '').strip().lower()
    head, text = normalize(title), normalize(summary)
    # 한 문장으로 결정을 전달하는 제목은 유효하다. 반복문·행사 표제만 걸러낸다.
    if head and not text.replace(head, '').strip() and (
            text != head or re.search(r'opening remarks|press conference|transcript|모두발언|연설문', head)):
        return False
    limited = re.search(
        r'(?:source text|provided (?:text|source|summary)|available (?:source text|text|information))'
        r'.{0,100}(?:limited to (?:the )?(?:title|headline)|'
        r'(?:does not|do not) (?:provide|include|contain) (?:any )?(?:specific|concrete) (?:figures|details|information)|'
        r'(?:specific|concrete) (?:figures|details|policy decisions).{0,40}(?:cannot|can not) be confirmed)|'
        r'\bsource(?: text(?: provided| supplied)?| article)? (?:provides?|contains?|includes?) only (?:a |the )?(?:headline|title)|'
        r'\bsource text(?: provided| supplied)? is (?:a |the )?(?:headline|title).{0,50}\bonly\b|'
        r'원문.{0,45}(?:제목.{0,25}(?:한정|반복)|구체.{0,25}확인할 수 없)',
        summary, re.I)
    return not bool(limited)


def informationless_index_recap(title: str, summary: str) -> bool:
    """지수 수치를 전혀 확보하지 못한 정형 마감 기사만 제외한다."""
    return bool(re.search(r'how major .*stock indexes fared', title or '', re.I)
                and re.search(r'not available|unavailable|no further (?:figures|numbers)|not .*provided', summary or '', re.I)
                and not re.search(r'\d[\d,.]*\s*(?:%|points?|포인트)', summary or '', re.I))
