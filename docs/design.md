# 설계 — 데이터 · AI · 카테고리 · 화면 구성

> 통합 문서(2026-09-21 현행화). 구 `데이터_AI_카테고리_설계`(6/27) · `데이터_구성요소_정의서`(8/26) · `design_미진출국`(8/21)을 하나로 합치고, 구현으로 대체된 초안 내용은 제거했다.
> 화면 픽셀·디자인 토큰은 `../mockups/HANDOFF.md`, 랭킹 수식은 `rank_score_spec.md`, 구현 현황은 `../STATUS.md`.

---

## 1. 전체 흐름

```
fetch(collector) → 관련성 게이트(keyword_filter, 축B) + dedup
  → llm_prefilter(노이즈 keep/drop) → fulltext(원문 추출)
  → llm_ranker(ai_score·요약·주제·이벤트유형·주제국가·KB시사점) → ai-dedup(근접중복)
  → expand(모달 긴 요약) → translate(한국어) → brief(국가 브리핑) → highlights(오늘의 글로벌 핵심)
  → export(data/export/*.json + web 템플릿 주입) → Cloudflare Pages
```

원칙: 싼 필터 → 비싼 분석, 증분 처리(이미 결과 있으면 스킵), 노출(ACTIVE) 기사에만 고비용 단계(긴 요약·번역), 무료 소스 우선.

## 2. 수집 레이어

- **원천**: `sources.yaml`(108 소스 / 154 피드, 2026-09-21 실측). RSS 직접 수집이 막히면 Google News 검색 RSS(`?q=site:DOMAIN+when:1d`)로 우회. 소스 메타: `media_name`, `primary_country_code`, `language`, `tier`(0 공식기관/1 주요언론/2 보조), `categories`.
- **특수 피드 묶음**: 미진출국 국가·언어 쿼리, **제재 모니터**(sanctions 글로벌/KB거점국/국내 3종), **거점국 주제확장**(진출 13개국 society·labor·tech·industry·policy·trade). 모두 Google News 쿼리.
- **`OFFICIAL`(중앙은행·감독당국) tier 0 피드는 비활성** — 당국 원천 피드는 "비-뉴스 소스 보류" 원칙에 따라 켜지 않는다.
- **기사 원본 필드**(`articles_raw`, AI 이전): `title`, `link`, `summary`, `published_at`, `content_hash`, 필터 결과(`filter_decision/score/reason`), `duplicate_of`, 본문 `full_text`.
- **지표(`indicators`)**: 환율(open.er-api, USD 기준) · 주가지수(yfinance) · 정책금리(`config.POLICY_RATES` 소형 표, 수동 갱신·as_of 명시) · 미국 10년물(무료·안정 소스가 미국뿐). SG·KH는 단일 정책금리가 없어 생략, 라오스 정책금리는 신뢰 가능한 무료 시드 미확보. 일별 스냅샷을 쌓아 스파크라인·1/3/6개월 추세(`indicator_history`, `indicators-history` 주 1회 백필)에 재사용.

## 3. AI 레이어 산출물

| 단위 | 산출물 | 모듈 | 용도 |
|---|---|---|---|
| 기사 | `llm_prefilter` | `llm_prefilter.py` | 키워드는 통과했지만 무관·노이즈인 기사 제거 |
| 기사 | `ai_score`(0~100), `summary_en`, `topics`(주제 카테고리), `event_type`, `primary_country`, `kb_implication_en` | `llm_ranker.py` | 노출 게이트(≥55)·카드 요약·필터·KB 시사점 |
| 기사 | `summary_ko`, `kb_implication` | `llm_translate.py` | 표시분 한국어(영어가 canonical, 미번역 시 EN 폴백) |
| 기사 | `expanded_summary` | `llm_expand.py` | 모달 3~4문단, 다출처 종합. 소스가 얇으면(스니펫 ≤300자) 경량 프롬프트로 분기 |
| 기사 | `duplicate_of`(`dup_by_ai`) | `llm_dedup.py` | 같은 사건의 다른 표현 묶기. 검사 성공 범위만 교체, 실패·미검사분은 기존 결과 보존. **대표 = 프리뷰 아님 → 그 나라 현지언론 → ai_score → 게시 최신**(국가 탭이 매체국적 기준이라 현지언론 대표가 아니면 탭에서 사라짐), **겹침 가드**(`config.DEDUP_MIN_OVERLAP`=0.25, 제목+요약 토큰 겹침 그래프의 연결요소만 한 그룹). 소급은 `main.py dedup-repair` |
| 국가 | `country_briefings`(summary/issues/outlook/keywords/key_stat/source_articles, 한·영) | `briefing.py` | 뉴스 탭 상단 일일 브리핑, 주간 브리핑. 적격 기사(주제국가·55점·요약 보유)가 없으면 "기준 미충족" 안내 저장 |
| 전체 | `daily_highlights`(Top 10) | `briefing.py` | 홈 TOP ISSUES |
| 전체 | `rank_score` | `ranking.py` | 화면 정렬 전용(게이트·온도는 ai_score) |

운영 원칙: 모델은 현재 전 단계 Haiku + Batches(50%↓); 요약·시사점은 수집 원문 범위 내로 제한(환각 금지, 출처 URL 부착); 평가는 `eval/`의 149건 평가셋(prefilter F1 0.542→0.708); 주간 합성 브리프처럼 대외 메시지 성격이 강한 산출물은 사람 검수 권장.

## 4. 카테고리 축

"카테고리"라는 말에 성격이 다른 축이 섞여 있어 분리해서 관리한다. 정의 파일은 `taxonomy.yaml`(주제·이벤트) + `sources.yaml`(지역).

| 축 | 정체 | 방식 |
|---|---|---|
| A 지역·소스 | `GLOBAL_*` / 국가코드 / `OFFICIAL` | 소스 등록 시 확정(규칙) |
| B 관련성 게이트 | "금융·국가 관련 기사냐" — 키워드 점수제 **통과 임계 2**, 제목 +5/본문 +3(금융어), 국가어 +2/+1, 제외어 −4. **SOCIETY 키워드는 독립 통과 경로** | `keyword_filter.py`(규칙) |
| C 주제 6종 | UI 카테고리 필터 | `llm_ranker`가 분류 — **첫 번째 = 주 카테고리(export `c`, 필터·칩 건수 기준), 나머지 최대 2개 = 보조(`c2`, 모달 표시 전용)** |
| D 화면 라우팅 | 어느 화면에 실을지 | export 단계 규칙 |
| E 이벤트 유형 4종 | 모니터링 전용(멀티라벨): REG 규제 · SANCTION 제재 · DEAL 거래·투자 · INCIDENT 사건사고 | `llm_ranker` |

**축 C — 6종과 판단 기준** (2026-09-11 확정, 구 MARKET·BANKING·DIGITAL·ESG·RISK 5종 대체)

| 코드 | 카테고리 | 핵심 변화 판단 |
|---|---|---|
| ECONOMY | 경제 | GDP·물가·고용·산업·무역·소비 등 실물경제 |
| MARKETS | 금융시장 | 은행·증권·보험·금리·환율·주식·채권 — 시장가격/금융시장 |
| TECH | 기술·디지털 | AI·핀테크·디지털금융·반도체·플랫폼 |
| GEO | 지정학·리스크 | 전쟁·선거·정치·부도·AML 등 국가/지역 리스크 |
| POLICY | 정책·규제 | 금융규제·법률·정부정책·통화정책·중앙은행 (**ESG는 여기로 흡수**) |
| SOCIETY | 사회·문화 | 인구·노동·사회·문화·소비 트렌드 |

주 카테고리 선택 우선순위: ① 기사 내 핵심 사건/변화 → ② 주요 영향 대상 → ③ 세부 정보. 금융시장↔정책·규제 경계는 **중앙은행·규제당국의 결정·발표·신호 = POLICY, 그 결과로 나타난 시장 가격 반응(금리·환율·주가 등) = MARKETS**. 주제 축의 "정책·규제"와 이벤트 축의 "규제"는 별개. (2026-09-21 전까지는 멀티라벨을 필터에 그대로 써서 칩 건수 합이 기사 수의 약 1.9배였다 — 주/보조 분리로 해소.)
구 MARKET·RISK는 2~3개로 갈라져 라벨 이관이 불가능해 노출(ACTIVE)분을 재랭킹으로 소급했다.

**축 D — 라우팅**: 홈 ← `daily_highlights` + `country_signals`(3단계 안정/주의/경계) + 진출국 지표 티커 / 뉴스 ← 국가 피드(ACTIVE, rank_score순) + 국가 브리핑 / 모니터링 ← 이벤트유형 탭 + 한국계 금융기관 + 인사동향 / 지표 ← `indicators` 추세 / 주간 ← 주간 브리핑.

**국가 태그 규칙**: 진출국 현지 피드는 매체국적 유지(정의상 현지언론). 인사동향·한국계 금융기관·모니터링·미진출 통합 피드는 AI 주제국가(`primary_country`, 없으면 매체국 폴백) 우선 — 매체국적이 주제와 어긋나는 오분류(예: 인니 중앙은행 총재 기사가 GB/JP로 표시)를 교정한다.

## 5. 진출국 / KB 미진출국

- **진출국 13**: 기존 11 + 태국·라오스(2026-08-28 편입, 관심시장). 국가별 선택 → 지표 + 일일 브리핑 + 기사 피드.
- **KB 미진출국 13**: 한국계 은행은 진출했지만 KB는 없는 시장 중 선별 — PH·MY·BD·PL·DE·FR·KZ·UZ·AE·BR·MX·AU·CA (태국이 진출국으로 이동하며 14→13). **국가 축을 접고 하나의 통합 피드**.

| 구분 | 진출국 | 미진출국 |
|---|---|---|
| 수집 | 큐레이션 로컬 매체(`sources.yaml`) | Google News 국가·언어 쿼리(매체 큐레이션 안 함) |
| AI | 전체(ai_score·요약·주제·KB 시사점) | 경량(ai_score·요약·주제, KB 시사점 생략) |
| 국가 브리핑 | daily/weekly | 없음 |
| UI | 국가별 브리핑+피드 | 통합 피드, rank_score순, 근접중복 1건+"관련 N건", 카드에 국가 태그(이모지 국기) |

미결: 확신도 '중' 6개국(말레이시아·프랑스·브라질·멕시코·호주·캐나다)의 사업그룹 검증. 증원 시 후보는 스리랑카·바레인(러시아는 제재로 비권장). 국내은행 해외점포 근거는 은행 공시·FSS 통계로 재확인 권장.

## 6. 화면 구성 요소 · 데이터 계약

하단내비 5탭 + 인트로. 모든 화면은 `web/*.html` 템플릿에 `export_json.py`가 `<script id="{name}-data">`로 데이터를 주입(자기완결 HTML), 없으면 `fetch('{name}.json')`, `?date=YYYY-MM-DD`면 `archive/<date>/`, 언어는 `?lang=en`.

| 화면 | 파일 / JSON | 구성요소 |
|---|---|---|
| 홈 | `brief.html` / `pulse.json` | GLOBAL PULSE 도트지도(거점 신호), GLOBAL MARKETS 티커(진출 13개국 fx·지수·정책금리), TODAY'S TOP ISSUES(`daily_highlights` 10건 → 클릭 시 모달: 요약 + 기사 링크) |
| 뉴스 | `countries.html` / `countries.json` | 진출/미진출 토글, 국기 선택기(★핀 고정), 지표 카드, 일일 브리핑, 기사 피드, 카테고리 칩, 원문 링크 진입 |
| 모니터링 | `topics.html` / `topics.json` | 이벤트유형 탭(규제·제재·거래투자·사건사고) + 한국계 금융기관·인사동향, 진출/미진출 병합, 국가 칩(탭별 건수) |
| 지표 | `markets.html` / `markets.json` | 환율·지수·정책금리·국채 추세, 1/3/6개월 토글 |
| 주간 | `weekly.html` / `weekly.json` | 국가별 주간 요약·이슈·전망·키워드 |
| 원문 링크 | `links.html` | 국가별·매체별 원문 링크(뉴스·모니터링에서 진입) |
| 인트로 | `intro.html` / `intro.mp4` | 시네마틱 영상 + SKIP, 종료/에러 시 자동 이동 |

확정 결정: 브리프는 **매일 05:00 KST 생성, 전일+당일 창**(→ 정기 자동화 필요) · 메인은 뉴스 Top 10 · 키맨(인사) 전용 화면은 제거하고 인사 뉴스는 일반 흐름에 노출 · 요약은 3개+ 언론사 종합, AI 티 배제 · 지표는 USD 기준 환율+지수(미국·MM·KH·LA 특수 표기) · 주간 리포트 유지 · 한국식 등락색(상승 빨강/하락 파랑).
미확정: 주간 합성 브리프(4대 시장신호·KB 액션·핵심리스크/전략기회)의 근거·합성 로직.

## 7. 결정 기록

- **ESG 수집 공백 진단(2026-09-03)**: 60일 채점 5,832건 중 ESG 178건(3%), 금융 ESG 용어 기사는 7건. ESG 태깅 정확도는 100%(분류·프리필터는 정상)이고 원인은 순수 수집 커버리지. 금융 ESG 전용 Google News 쿼리 3종(green bond/sustainable finance, 아시아 녹색금융, ESG 공시·기후리스크 규제) 패치를 준비했으나 **미적용** — 이후 9/11에 ESG를 POLICY로 흡수하면서 사실상 종결. 필요해지면 위 3개 쿼리를 `sources.yaml`에 추가하되 맥북에서 `fetch` 수율부터 확인할 것.
- **이용 활성화 재미요소·참여형 보류(2026-07-14)**: 캐릭터·날씨 아이콘, K-컬처 콘텐츠, 열람 이벤트, 퀴즈 등은 이번 범위 제외(정적 사이트라 접속자 카운트·당첨은 백엔드 필요).
- **거시지표·미진출국·한국계 금융기관·인사동향은 뉴스/무료 데이터로 도출 가능하므로 범위 편입**, 자회사 IR·OFFICIAL 원천 피드·제재 명단 스크리닝은 비-뉴스 소스라 보류.
