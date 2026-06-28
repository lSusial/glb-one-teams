# 데이터 수집 · AI 생성 · 카테고리 설계

> 초점: 화면 구성이 아니라 **무엇을 수집하고 · AI로 무엇을 만들고 · 어떻게 분류할지**
> 근거: 이 레포의 `schema.sql`, `keyword_filter.py`, `sources.yaml`, `docs/work_log.md` 실제 코드
> 작성: 2026-06-27
> 관련: 화면별 구성·연동은 **`화면분석_개발가이드.md`** 참조

---

## 0. 출발점 — 코드베이스가 이미 알려주는 것

가장 중요한 사실: **AI 파이프라인은 이미 설계돼 있다.** `schema.sql`의 `articles_raw` 테이블이 AI 산출 컬럼을 미리 예약해 뒀고, 별도 `country_briefings` 테이블까지 있다. 다만 이 "수집 전용" 레포에는 그 AI 모듈이 없다(`requirements.txt`에 anthropic/openai 같은 AI 라이브러리 없음 — feedparser·PyYAML·requests뿐). 실제 AI 모듈(`llm_prefilter.py`, `llm_ranker.py`, `briefing.py`)은 prototype 레포에 있다.

따라서 과제는 **"새 데이터/분류 체계를 발명"이 아니라, 스키마가 예약해 둔 AI 레이어를 이 베이스에 (재)구현하고, UI가 요구하는 필드까지 확장"** 하는 것이다.

3단계 현황:

| 단계 | 모듈 | 상태 |
|---|---|---|
| 수집 (fetch) | `collector.py` | ✅ 구현됨 (RSS + Google News 우회) |
| 키워드 필터 + 중복 | `keyword_filter.py` | ✅ 구현됨 (점수제 게이트) |
| LLM 1차 관문 | `llm_prefilter.py` | ⬜ 스키마만 예약 (컬럼 `llm_prefilter`, `llm_reject_reason`) |
| AI 분석 | `llm_ranker.py` | ⬜ 스키마만 예약 (`ai_score`, `summary_ko`, `topics`, `ai_model`) |
| 국가 브리핑 | `briefing.py` | ⬜ 스키마만 예약 (`country_briefings` 테이블) |

---

## 1. 어떤 데이터를 수집하나 (수집 레이어 — 현행)

### 1.1 원천(sources) — 88소스 / 106피드

소스 메타(`media_sources`): `media_name`, `primary_country_code`, `language`, `tier`, `categories`. RSS 직접 수집이 막히면 Google News 검색 RSS로 우회(`?q=site:DOMAIN+when:1d`).

소스에 붙는 `categories` 코드는 **주제가 아니라** 지역·성격 분류다:

- `GLOBAL_GENERAL` / `GLOBAL_ECONOMY` — 글로벌 매체 (BBC, Reuters, Bloomberg 등)
- 국가코드 `US/GB/HK/CN/JP/SG/IN/VN/MM/ID/KH` — 해당국 전용 매체
- `OFFICIAL` — 중앙은행·감독당국 (HKMA, MAS 등) — **현재 tier 0 비활성**

### 1.2 기사 원본 필드(`articles_raw`, AI 이전)

수집기가 AI 없이 채우는 컬럼: `title`, `link`, `summary`, `published_at`, `fetched_at`, `content_hash`(중복키), 그리고 키워드 필터 결과(`filter_decision`, `filter_score`, `filter_reason`), 중복(`duplicate_of`).

→ 이 필드들이 UI 기사 카드의 **매체·날짜·제목·원문URL**에 그대로 매핑된다.

### 1.3 UI 대비 빠진 수집원 (갭)

화면이 요구하지만 현재 수집 안 하는 데이터:

| 화면 | 필요한 수집원 | 현황 |
|---|---|---|
| 규제·금융기관 | `OFFICIAL` 당국 피드 (BCBS/BIS, EU, 각국 감독·중앙은행) | tier 0으로 **비활성** → 활성화 필요 |
| 국외연결자회사 | ID·KH 자회사 IR·경영공시 | **수집원 없음** → 신규 추가 |
| 글로벌 동향 빅넘버 | 금리·환율 등 거시지표 (Fed/BI rate, FX) | 뉴스가 아닌 **데이터 피드** → 별도 소스 |

---

## 2. AI로 어떤 데이터를 만들어내나 (AI 레이어)

스키마가 예약한 컬럼 = AI가 만들어야 할 산출물의 명세서다.

### 2.1 기사 단위 (per-article)

처리 순서는 "싼 필터 → 비싼 분석" (work_log: `filter_score` 높은 순으로 LLM 처리):

1. **`llm_prefilter`** (+ `llm_reject_reason`) — 키워드 통과분 중 LLM 2차 관문. 키워드는 통과했지만 실제론 무관·노이즈인 기사 제거. 입력 짧게(제목+요약), 저비용 모델.
2. **`ai_score`** — 중요도 점수. → UI의 **ACTIVE(노출) vs SOURCE WATCH** 판정 기준.
3. **`summary_ko`** — 한국어 요약. → UI 기사 **요약(q)**.
4. **`topics`** — 주제 태그(아래 3장 축 C). → UI **카테고리(c)·필터**.
5. **`ai_model`** — 생성에 쓴 프로바이더/모델 기록. → "AI 프로바이더 실험" 추적·A/B용.
6. **[갭] `kb_implication`** — UI의 **KB 시사점(k)**에 해당하는 컬럼이 **현재 없음**. 신규 컬럼 추가 필요. KB 거점 맥락(`kb_network.py`: 어느 국가가 지점/법인/자회사인지)을 프롬프트에 주입해 생성.

### 2.2 국가 단위 (`country_briefings`)

국가·날짜별 브리핑: `summary`, `issues`, `outlook`, `keywords`, `key_stat`, `article_count`, `source_articles`. → UI의 거점 브리핑·시장신호 근거, 빅넘버의 `key_stat`(예: BI-Rate 5.75%)으로 직접 연결.

### 2.3 코퍼스 합성 (상위 화면 — 신규 설계 필요)

스키마에 아직 없는, UI 상위 화면을 위한 합성물:

- **주제 클러스터링** → Topic Watch / 4대 시장신호 (여러 기사·국가를 묶어 1건의 리포트로)
- **주간 초점(hero) + KB 액션플랜** → 최상위 편집 합성. **사람 검수 필수**(책임 있는 대외/내부 메시지라 자동 발행 비권장).
- 합성물의 출처는 반드시 클러스터에 속한 **실제 수집 기사 URL**로 채운다 → 샘플의 죽은 링크(`url:'#'`) 해소.

### 2.4 AI 운영 원칙

- **역할별 모델 분리**: prefilter=저비용·고속, 요약/시사점/합성=고성능. 프로바이더를 작업별로 혼합하는 것이 "AI 프로바이더 실험"의 실체. `ai_model` 컬럼으로 추적.
- **근거 기반(grounding)**: 요약·시사점은 수집 원문 범위 내로만 생성, 환각 금지, 출처 URL 부착.
- **증분 처리**: `ai_score`/`summary_ko`가 이미 있으면 재처리 스킵 (비용 절감).
- **검수 게이트**: 시장신호·KB 시사점·브리핑은 발행 전 사람 확인 큐.
- **평가셋 기반 비교**: 프로바이더 우열은 분류 정확도·요약 품질 평가셋으로 판단(감으로 X).

---

## 3. 어떻게 카테고리화하나 (핵심: 3축 분리)

지금 "카테고리"라는 말에 **성격이 다른 3가지가 섞여** 있다. 분리해서 설계해야 깔끔하다.

### 축 A — 지역·소스 분류 (수집 시점, 규칙 기반)

`sources.yaml` → `media_category_map`. `GLOBAL_*` / 국가코드 / `OFFICIAL`. AI 불필요, 소스 등록 시 확정. UI의 국가 플래그·국가별 커버리지가 여기서 나온다.

### 축 B — 관련성 게이트 (키워드 필터, 규칙 기반)

`keyword_filter.py`의 점수제. **주제 분류가 아니라 "금융 관련 기사냐"를 거르는 관문**이다:

- `FINANCE_KEYWORDS`(거시·은행·통화·지수·중앙은행·에너지/EV·건전성) 제목 +5 / 본문 +3
- `COUNTRY_KEYWORDS`(국가별) 제목 +2 / 본문 +1
- `EXCLUSION_KEYWORDS`(스포츠·연예) −4
- 합산 ≥ 3 → `passed`

### 축 C — 주제 분류 (AI `topics` — 신규, **UI 필터의 정체**)

UI의 필터 버튼·신호 카테고리가 바로 이 축이다. 현재 코드엔 없고 AI `topics` 컬럼에 채워야 한다.

UI에서 쓰는 분류를 정리하면:

- 현지언론 필터: **금융 / 디지털 / ESG / 금융사고·리스크**
- 시장신호 태그: **시장·경제 / 금융산업 / 규제·리스크 / 전략·운영 / ESG**

이를 **표준 주제 코드(controlled vocabulary)** 로 통일 제안:

| 코드 | 의미 | UI 매핑 |
|---|---|---|
| `MARKET` | 금리·환율·채권·거시 | 시장·경제 |
| `BANKING` | 은행·여신·건전성·금융산업 | 금융 / 금융산업 |
| `DIGITAL` | 디지털뱅킹·핀테크·결제·AI | 디지털 / 전략·운영 |
| `ESG` | 환경규제·지속가능금융 | ESG |
| `RISK` | 규제·금융사고·신용/시장 리스크 | 리스크 / 규제·리스크 |

설계 규칙:

- **멀티라벨**: UI 필터가 `c:'esg risk'`처럼 부분일치(`includes`)로 동작 → AI도 한 기사에 복수 코드 허용.
- **시드 재활용**: `keyword_filter.py`의 `FINANCE_KEYWORDS`가 이미 거시/은행/통화/에너지·EV/건전성으로 그룹핑돼 있다 → 이 그룹을 주제 분류의 few-shot 예시·매핑 규칙으로 그대로 활용.
- **하이브리드**: 1차는 키워드 그룹으로 후보 태그 → 2차 LLM이 확정·정제. (전량 LLM보다 싸고 일관됨)

### 축 D — 화면 라우팅 (분류 → 어느 화면으로)

분류는 태깅에서 끝나지 않고 **어느 화면에 실릴지**까지 결정한다:

- `OFFICIAL`/규제 소스 → **규제·금융기관**
- ID·KH 자회사 IR → **국외연결자회사**
- 현지언론 + `ai_score` ≥ 임계 → **현지언론 피드** (+ 축 C 필터)
- 멀티기사 클러스터 → **Topic Watch**
- 주간 최상위 클러스터 → **글로벌 동향**(신호·초점)

### 3.1 단일 정의 파일 제안

`sources.yaml`이 소스의 단일 출처이듯, **`taxonomy.yaml`** 을 신설해 주제코드(축 C)·라우팅 규칙(축 D)을 한 곳에서 정의하고 수집기·AI·UI가 공유한다. 카테고리 추가 시 한 파일만 고치면 전 구간 반영.

---

## 4. 전체 데이터 흐름 (한 줄)

```
fetch(collector)
  → 관련성 게이트(keyword_filter, 축B) + dedup
  → llm_prefilter(노이즈 제거)
  → AI 분석: ai_score + summary_ko + topics(축C) + kb_implication[신규]
  → country_briefings(국가 합성)
  → 코퍼스 합성: topic/signal/brief (+ 사람 검수)
  → 라우팅(축D) → data/*.json export
  → UI(fetch)
```

축 A(지역)는 수집 시 확정, 축 B(관련성)는 규칙, 축 C(주제)는 AI, 축 D(라우팅)는 A·C 조합.

---

## 5. 즉시 할 일 (제안 순서)

1. **`taxonomy.yaml` 정의** — 주제코드 5종(축 C)을 UI 필터와 1:1로 고정. 가장 먼저, 나머지가 여기에 의존.
2. **`articles_raw`에 `kb_implication` 컬럼 추가** — UI의 KB 시사점(k)을 담을 자리(현재 갭).
3. **`llm_prefilter.py` / `llm_ranker.py` 이식** — prototype 참고하되 **프로바이더 추상화 계층**으로 감싸 교체 실험 가능하게.
4. **수집원 보강** — `OFFICIAL`/tier0 활성화(규제), 자회사 IR 소스 추가, 거시지표 피드(빅넘버).
5. **평가셋 구축** — 분류·요약 품질 측정용. 프로바이더 비교의 기준.

---

## 부록 A — `taxonomy.yaml` 초안 (구현 1순위)

> 축 C(주제) + 축 D(라우팅)의 단일 정의. 아래는 문서용 초안이며 구현 시 실제 파일로.

```yaml
# taxonomy.yaml — 주제 분류(축 C) + 화면 라우팅(축 D)
# 수집기·AI·UI 공유. 카테고리 추가 시 이 파일만 수정.

topics:
  - code: MARKET            # 시장·경제
    ui: finance
    label: { ko: 시장·경제, en: Market·Economy }
    seeds: [interest rate, exchange rate, bond, gdp, inflation, central bank, yield]
  - code: BANKING           # 금융산업
    ui: finance
    label: { ko: 금융산업, en: Banking }
    seeds: [bank, lending, loan, npl, capital ratio, deposit, basel]
  - code: DIGITAL           # 디지털·핀테크
    ui: digital
    label: { ko: 디지털, en: Digital }
    seeds: [fintech, digital banking, payment, upi, mobile banking, ai]
  - code: ESG
    ui: esg
    label: { ko: ESG, en: ESG }
    seeds: [esg, sustainable finance, carbon, green bond, supply chain due diligence]
  - code: RISK              # 규제·금융사고·리스크
    ui: risk
    label: { ko: 규제·리스크, en: Regulation·Risk }
    seeds: [regulation, sanction, fraud, default, fine, supervision, compliance]

# 멀티라벨 허용 (UI는 includes 부분일치). AI topics 출력 = code 배열.
# 예: topics = [ESG, RISK]  →  UI c:'esg risk'

routing:                    # 분류·소스 → 어느 화면으로
  regulations:  "source.category에 OFFICIAL 포함 OR topic에 규제성 RISK"
  subsidiaries: "country in [ID, KH] AND source.type == IR"
  countries:    "source.type == local_media AND ai_score >= THRESHOLD"
  topics:       "cluster.size >= 2"
  brief:        "주간 최상위 cluster"
```

`keyword_filter.py`의 `FINANCE_KEYWORDS` 그룹(거시/은행/통화/에너지·EV/건전성)을 위 `seeds`의 출발점으로 재사용한다.

## 부록 B — `kb_implication` 마이그레이션 (구현 2순위)

> UI 'KB 시사점(k)'을 담을 컬럼. 현재 `articles_raw`에 **없음**(코드 확인 완료).

```sql
-- articles_raw에 KB 시사점 컬럼 추가
ALTER TABLE articles_raw ADD COLUMN kb_implication TEXT;
```

- **생성 주체**: `llm_ranker.py`가 `summary_ko`와 함께 산출.
- **맥락 주입**: `kb_network.py`의 거점 구분(지점/법인/자회사)을 프롬프트에 넣어 `[뉴욕 지점] …`, `[본점 Action] …` 형태로 출력.
- **근거 기반**: 수집 원문 범위 내로 제한, 출처 URL 부착(샘플의 `url:'#'` 죽은 링크 해소).
