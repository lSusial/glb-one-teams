# Claude Code 구현 프롬프트 모음 — 2026-08-21

> glb-one-teams 신규 기능 4작업. 레포 루트에서 `claude` 실행 후 각 블록을 붙여넣기.
> 설계 근거: `docs/신규기능_설계_20260821.md`, `docs/design_미진출국.md`
> **선결**: `ANTHROPIC_API_KEY` 교체(AI 단계 블로커).
> 추천 순서: ① 지표 → ③ 핵심3줄 → C(이슈+무드) → B(미진출, 독립).

---

## ① 국가별 지표 스트립 (환율·지수)

```
[작업] 국가별 거시지표(환율·주가지수) 빅넘버 기능 추가

## 목표
현지언론 화면(countries.html) 상단에 국가별 "달러 기준 환율 + 주가지수" 빅넘버 스트립을 추가한다. 전일 대비 등락(값·%·▲▼)과 색상 표시.

## 원칙 (CLAUDE.md 준수)
- 무료 소스만. API 키 불필요. 클라우드/데이터센터 IP에서도 동작(뉴스 파이프라인과 달리 맥북 의존 없음).
- 최소 코드·표면적 변경. 기존 config/db/export/main 패턴을 따를 것.

## 데이터 소스
- 환율: https://open.er-api.com/v6/latest/USD (무료·키불필요·일1회). rates[통화코드] = "1 USD 당 현지통화".
- 주가지수: yfinance(야후). requirements.txt에 yfinance 추가. 실패 시 조용히 스킵(카드 생략), 파이프라인 계속.

## 국가별 매핑 (config.py 상수 테이블)
| 국가 | 통화 | 지수 티커 |
| GB | GBP | ^FTSE |
| US | (환율 없음) | ^GSPC, ^IXIC (S&P500+나스닥, 2개) |
| HK | HKD | ^HSI |
| CN | CNY | 000001.SS |
| JP | JPY | ^N225 |
| SG | SGD | ^STI |
| IN | INR | ^NSEI |
| VN | VND | VNM (미국상장 VanEck Vietnam ETF 대체, 라벨 "VN (VNM ETF)") |
| ID | IDR | ^JKSE |
| MM | MMK | (지수 없음 → 환율만) |
| KH | KHR | (지수 없음 → 환율만) |
특수: US=환율칸 없음·지수2개, MM/KH=환율만.

## 신규 모듈 indicators.py
- fetch_indicators(): 환율(open.er-api 1콜) + 지수(yfinance) 조회.
- SQLite indicators 테이블 일자별 스냅샷: (date, country, kind[fx|index], symbol, value, prev_value, change, change_pct). schema.sql 추가 + db.ensure_columns 멱등.
- 등락: 지수는 yfinance 전일종가, 환율은 DB 직전 스냅샷 비교(최초 실행 null).
- MMK 주의: open.er-api MMK는 공식환율(시장환율과 괴리) → "공식" 표기 필드 하나.

## export & UI
- export_json.py: countries.json에 국가별 indicators 블록.
- web/countries.html: 거점 선택 시 상단 빅넘버 스트립(환율·지수, 등락 색상). 카드 1~3개 가변(US=지수2, MM/KH=환율1). 기존 톤 유지.

## main.py
- 서브커맨드 indicators 추가: fetch → DB 저장. ai/export와 독립.

## 검증
1. python main.py indicators → 11개국 환율, 지수는 커버 국가만.
2. MM·KH 환율만, US 지수2개.
3. python main.py export 후 countries.json indicators 블록.
4. countries.html 스트립·등락 색상.
5. py_compile / 기존 커맨드 회귀 없음.

작업 전 가정·불명확한 점 먼저 말하고, 계획 3~5줄 요약 후 진행.
```

---

## ③ 오늘의 글로벌 핵심 3줄

```
[작업] "오늘의 글로벌 핵심 3줄" (daily highlights) 추가

## 참조
docs/신규기능_설계_20260821.md의 ③ 항목.

## 목표
Global Pulse(brief.html) 최상단에, 그날 11개국 ACTIVE 기사를 가로질러 AI가 종합한 핵심 3줄 표시.

## 생성 로직 (briefing.py에 generate_daily_highlights)
- 입력: 당일 ACTIVE 기사 중 ai_score 상위 20~30건(제목·요약·국가·topics·kb_implication).
- LLM 1콜로 정확히 3개 항목: {category(금리/FX/규제/시장/디지털/지정학), headline(건조한 헤드라인 1줄), impact(KB 거점/자회사 영향 1줄, kb_network.py 맥락), country_codes[]}.
- 3개 미만이면 있는 만큼, 0개면 블록 생략(파이프라인 계속).

## 저장 & export
- brief 산출물/DB 저장 → export_json.py에서 brief.json에 daily_highlights 필드.

## UI (web/brief.html)
- 최상단 "📌 오늘의 글로벌 핵심 + 날짜" 블록. 카테고리 태그+헤드라인+거점 영향. 없으면 숨김. 한·영 토글(EN 없으면 KO 폴백).

## main.py
- ai(또는 brief) 마지막에 실행. 개별 실행 python main.py highlights.

## 기본값
- 3개 고정 · 건조한 헤드라인 · 거점 영향 필수.

## 검증
1. python main.py highlights → 3개 항목.
2. 기사 0건 시 블록 생략.
3. export 후 brief.json daily_highlights.
4. brief.html 최상단 렌더.
5. py_compile / 회귀 없음.

작업 전 가정·불명확한 점 먼저 말하고, 계획 3~5줄 요약 후 진행.
```

---

## C. 국가별 오늘의 이슈 + 시장 무드 배지

```
[작업] Brief 국가별 섹션 — 국가별 오늘의 이슈 + 시장 무드 배지

## 참조
docs/신규기능_설계_20260821.md의 ④⑤ 항목.

## 목표
brief 화면, "오늘의 글로벌 핵심 3줄" 아래에 국가별 섹션:
(A) 국가별 오늘의 이슈  (B) 시장 무드 배지 — 둘은 같은 동적 top-5 국가 공유.

## top-5 선정 (동적)
- 배지 풀 = 진출국 11개 (+ 있으면 글로벌 블록 歐/EU: 유럽 관련 기사 집계, 없으면 스킵).
- 이슈 강도 = ACTIVE 기사 수 + ai_score 합 상위 5개.

## (A) 국가별 오늘의 이슈
- "국가명 – 이슈1 · 이슈2 · 이슈3" (짧은 키워드형 2~3개).
- 소스: country_briefings.issues의 title 재활용(새 생성 불필요).

## (B) 시장 무드 배지
- 배지 = 국기 + 대표 키워드 + 날씨 아이콘 + 추세 화살표 + 컬러 링.
- 5단계: ☀️맑음 → ⛅ → ☁️흐림 → 🌧️비 → 🌀태풍.
  계산 = 지표 방향(indicators 있으면 사용, 없으면 뉴스만) + RISK 토픽 밀도 + ai_score 분포 → 0~100 정규화 후 5단계.
- 추세 화살표 = 지수/지표 방향(없으면 무드 모멘텀). 대표 키워드 = briefing keywords[0] 또는 LLM 2~4자 라벨.
- 하단 "최근 N건 기준 · 갱신 시각".

## export & UI
- export_json.py: brief.json에 country_section = top-5 각 {cc, flag, mood_level, mood_icon, trend, keyword, issues[], basis_count}.
- web/brief.html: 핵심3줄 아래 (A)+(B)를 같은 5개국 한 섹션. 데이터 없으면 숨김. 한·영 토글.

## 검증
1. top-5 이슈 강도순(조용한 날 소거점도 진입).
2. (A)(B) 동일 5개국.
3. 무드 아이콘·화살표 근거대로, indicators 없어도 뉴스만으로 동작.
4. brief.json country_section / brief.html 렌더 / 0건 시 숨김.
5. py_compile / 회귀 없음.

작업 전 가정·불명확한 점 먼저 말하고, 계획 3~5줄 요약 후 진행.
```

---

## B. KB 미진출국 통합 피드 (실행됨 2026-08-21)

```
[작업] KB 미진출국 통합 피드 카테고리 추가

## 참조
docs/design_미진출국.md, docs/신규기능_설계_20260821.md 먼저 읽고 그 결정에 따를 것.

## 목표
현지언론(countries) 최상위에 [진출국] / [KB 미진출국] 구분 추가.
- 진출국(11개): 기존 국가별 구조 그대로. presence 딱지만 부여.
- KB 미진출국(14개): 국가 구분 없이 통합 피드. 중요도(ai_score)순. 카드에 국가 라벨·국기(국가 네비 없음). 주제 필터 유지.

## 대상 14개국 (⚠️ 잠정 — config 한 곳에서 수정 가능하게)
필리핀·태국·말레이시아·방글라데시·폴란드·독일·프랑스·카자흐스탄·우즈베키스탄·UAE·브라질·멕시코·호주·캐나다

## 구현
- 국가 메타에 presence: 진출|미진출. 미진출 14개국은 Google News 국가·언어 쿼리로 sources.yaml/config 등록(개별 매체 큐레이션 안 함).
- 수집: 미진출은 Google News 쿼리 경량 수집.
- AI: 미진출은 요약·topics까지. KB시사점·국가브리핑 스킵(kb_network.py 거점 없음 분기).
- export_json.py: countries.json presence 반영, 미진출은 국가 병합 단일 피드(ai_score desc), 기사에 country code/label 보존.
- web/countries.html: [진출국]/[KB 미진출국] 구분. 미진출은 통합 피드(국가 네비 없음)+카드 국기/국가 태그+주제 필터.

## 검증
1. run 후 미진출 14개국 presence=미진출 수집.
2. export 후 countries.json 진출/미진출 그룹, 미진출 통합 피드(ai_score순).
3. countries.html [KB 미진출국] → 국가 구분 없는 피드+국기 태그+주제 필터.
4. 진출국 화면 회귀 없음 / py_compile.

작업 전 가정·불명확한 점 먼저 말하고, 계획 3~5줄 요약 후 진행.
```
