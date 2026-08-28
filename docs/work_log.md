# 작업 내역 (Work Log)

## 2026-06-26 현재 상태

### 레포 구조
| 레포 | 경로 | 역할 |
|---|---|---|
| glb-news-rss/prototype | 로컬 전용 | 풀 파이프라인 + KB 대시보드 (Streamlit) |
| glb-one-teams | GitHub | 수집 전용 (AI 없음), 새 UI 실험용 베이스 |

---

## 세션 이력

### 세션 1~2 (이전)
- `llm_prefilter.py` — ORDER BY filter_score DESC 변경 (고점수 기사 우선 처리)
- `keyword_filter.py` — ASEAN, UPI, CPF, gojek, VN-index 등 리전 키워드 추가
- `collector.py` — Google News URL 실제 링크 해소 기능 추가 (ThreadPoolExecutor 30 workers)
- `briefing.py` — 브리핑 재생성 스킵 로직 추가, key_stat 스키마 수정
- `score_engine.py` — 미사용 `_topic_matches()` 함수 제거
- `llm_ranker.py` — _NOISE_TITLES에서 "bi" 제거 (오탐 방지)
- `main.py` — cmd_brief 기본값 weekly → daily
- `dashboard_stocks.py` — `_latest_date()` `<= yesterday` 제약 제거 (당일 데이터 표시)
- `kb_network.py` — 뭄바이 → 구르구람 변경
- **glb-one-teams 레포 신규 생성** — GitHub: https://github.com/lSusial/glb-one-teams.git

### 세션 3 (2026-06-25)

#### Oracle Cloud 서버 세팅
- 서버: `ubuntu@168.107.56.139` (포트 22)
- SSH 키: `~/workspace/ssh-key-2026-06-25-4.key`
- 작업: git clone + venv + requirements.txt 설치 완료
- DB 초기화: 88 sources, 106 feeds

#### Google News 503 문제 발견
- Oracle Cloud IP → Google이 봇으로 감지 → 503 차단
- 서버 직접 수집: 106개 중 40 성공 / 66 실패
- **해결책:** 맥북에서 수집 → rsync로 서버 동기화 (개발 단계)
- 추후 프로덕션: Residential Proxy 도입 고려

#### 국가 구성 확정
- **HK(홍콩) 분리** — SCMP를 CN에서 HK로 이동, RTHK·HK Free Press·HKMA 추가
- **SG(싱가포르) 추가** — Straits Times·CNA를 GLOBAL→SG, Business Times·MAS 추가
- SCMP categories에서 CN 제거 (HK 전용으로 단일화)

#### 추가 파일
- `sync_to_server.sh` — 맥북→서버 rsync 동기화 스크립트

### 세션 4 (2026-06-27)

#### 새 UI 레퍼런스 분석
- 샘플 화면: https://uandix-kaneiko.github.io/global_One_Team/
- 단일 HTML SPA(바닐라 JS), 콘텐츠 전량 하드코딩. 6개 탭(글로벌동향/현지언론/자회사/TopicWatch/규제/참여)
- 화면이 요구하는 기사 필드: 매체·날짜·제목·요약(q)·KB시사점(k)·원문URL + 카테고리(c)
- 메모: 샘플엔 오클랜드(NZ)가 추가됨(관리국 11개엔 없음) → 포함 여부 결정 필요

#### 설계 문서 2종 작성
- `화면분석_개발가이드.md` — 화면별 항목·콘텐츠 + 데이터 연동 로드맵
- `데이터_AI_카테고리_설계.md` — 수집 데이터 / AI 산출물 / 3축 카테고리 설계

#### 핵심 발견
- `schema.sql`이 이미 AI 파이프라인 컬럼 예약: `llm_prefilter`, `ai_score`, `summary_ko`, `topics`, `ai_model` + `country_briefings` 테이블
- 단, 이 레포엔 AI 모듈 없음(`requirements.txt`에 AI 라이브러리 없음). 실제 모듈은 prototype 레포에
- UI 'KB 시사점(k)' 담을 컬럼 `kb_implication` **부재** → 신규 필요
- "카테고리"가 3축 혼재: 지역(sources.yaml) / 관련성게이트(keyword_filter) / 주제(AI topics, 미구현)

### 세션 5 (2026-06-27) — AI 레이어 구현 (코드 생성, 미실행)

#### 신규 모듈
- `taxonomy.yaml` + `taxonomy.py` — 주제코드 5종(MARKET/BANKING/DIGITAL/ESG/RISK), 시드매칭·검증·UI매핑
- `config.py` / `db.py` — 공통 경로·임계값·모델, 연결·마이그레이션 헬퍼 (리팩토링)
- `llm_provider.py` — 프로바이더 추상화 + Anthropic(실제)·OpenAI(스캐폴드)·Stub(오프라인) + 팩토리
- `kb_network.py` — KB 거점 정의(시사점 맥락 주입용)
- `llm_prefilter.py` — LLM 1차 관문(keep/drop)
- `llm_ranker.py` — ai_score / summary_ko / topics / kb_implication 생성
- `briefing.py` — 국가별 country_briefings 생성
- `export_json.py` — DB → data/export/countries.json (UI 데이터 계약)

#### 변경
- `schema.sql` — articles_raw에 `kb_implication` 컬럼 추가
- `requirements.txt` — anthropic 추가
- `main.py` — config/db 사용, 서브커맨드 prefilter/rank/brief/ai/export 추가 (run은 수집 전용 유지)

#### 상태
- py_compile + 순수 헬퍼 검증 통과. **실제 실행·API 호출은 안 함** (ANTHROPIC_API_KEY 필요)
- 모델 분리: prefilter=haiku, rank/brief=sonnet (config.py에서 조정)

### 세션 6 (2026-06-27) — 수집·필터 점검 + 리팩토링

#### 리팩토링 (동작 보존)
- `config.py` — 수집 튜닝 상수(USER_AGENT·타임아웃·병렬수·재시도·GNews 해소) 일원화
- `collector.py` — 위 상수 config 참조, `init_db`를 `db.open_conn` 경유(PRAGMA 일관)
- `keyword_filter.py` — `ensure_filter_columns`/`ensure_dedup_column`을 `db.ensure_columns`로 위임(중복 제거)

#### 점검 발견·수정 (필터 커버리지 갭)
- 관리국 11개 중 **GB·HK·SG 누락** — `COUNTRY_KEYWORDS`/`KOREAN_COUNTRY_KEYWORDS`에 추가
- GB/HK/SG **금융 신호(hang seng·ftse·hkma·MAS·gbp/hkd/sgd 등)가 `FINANCE_KEYWORDS`에도 누락** → 추가
- 효과: 영국/홍콩/싱가포르 현지 금융기사 정상 통과(오프라인 테스트 확인). 스포츠·무관 거부 동작 보존

#### 검증
- py_compile + 필터 오프라인 단위테스트(합성 입력). 실제 네트워크 수집·AI 호출 미실행

### 세션 7 (2026-07-16) — 기획 확정안 반영 · 필터 품질 개선 · 관리자 페이지

#### 기획·문서
- 사내 확정본 「글로벌 One Team 뉴스데일리 구축(안)」(2026-07-13) 반영 → `PLAN.md`·`STATUS.md` 신규(이 레포 기준: 11거점·6탭 UI·3축 카테고리). `CLAUDE.md`에 참조·현재 진행 범위 추가.
- **이번 진행 범위 확정**: 뉴스 분석으로 도출 가능한 항목 포함(현지언론+AI·온도계·Key-man 인사동향·규제 화면), 비-뉴스 소스(IR·OFFICIAL 원천·거시지표)·재미요소는 보류.

#### 수집·필터 품질 (진단→개선→측정)
- 평가셋 `eval/eval_set.jsonl`(70건 라벨) 구축.
- `keyword_filter.py` 개선: **복수형 매칭**(`_kw_pattern` s? 허용), 금융어 보강(opec·fed chair·insurance·stock·net loss 등), 내비게이션 페이지 제외.
- 결과: 정밀도 54→66% · 재현율 60→92% · **F1 57→77%**. 리포트 `docs/수집필터_품질진단.md`.

#### 코드/CLI
- `main.py` **복구**(HEAD에 있었으나 워킹트리 삭제 상태) + `export --passed`(Phase1 AI-free) · `admin` 서브커맨드 추가.
- `export_json.py`: `active_only` 파라미터 추가(AI 없이 passed 기사 export). Phase 1 `countries.json` 생성 검증(9개국 140건).
- **관리자 페이지** 신규: `web/admin.html`(템플릿) + `admin_export.py` → `data/export/admin.html`(개요·기사·소스 3탭, 오프라인 열람).

#### 미해결 / 다음
- **최신 재수집 필요**(맥북): 현재 DB는 2026-06-22 스냅샷 — 구 필터 판정·HK/SG 0건. 개선 필터+HK/SG 반영은 재수집 후 `filter --refilter`·`admin`·`export --passed` 재실행.
- `ai_score` 스케일: 구 DB는 1~5, 코드는 0~100(임계 60) → `export`(ACTIVE) 0건. Phase 2에서 재랭킹·표기 통일.
- Phase 2 AI(taxonomy 태깅·요약·kb_implication) + LLM 프리필터(정치성 노이즈).
- **[검토] Phase 2 비용 최적화**: rank가 Sonnet이라 레거시(Haiku) 대비 토큰 비용↑ (+ kb_implication 출력 추가, 1호출=1기사). prompt caching은 시스템 프롬프트 177토큰(<1024 최소치)이라 무효. 레버 — ① `rank --fast`(Haiku) 토글 추가해 품질 A/B, ② N건/호출 배치(시스템 프롬프트 분할, 출력비용은 그대로). 물량은 `--days`로 이미 축소. → **세션 8에서 모델 Haiku 전환 + Message Batches API(50% 할인) 적용 완료.**

### 세션 8 (2026-07-23) — 비용 최적화 · UI 개편 · 현지언론 일일 브리핑

#### AI 비용 최적화
- **모델 Haiku 전환**: `config.ANTHROPIC_MODEL_SMART` = `claude-haiku-4-5`(rank/brief도 Haiku). 토큰 절약.
- **Message Batches API 적용(토큰 50% 할인)** — 비실시간 일괄 처리라 일일 파이프라인에 최적(완료 최대 24h·보통 수분).
  - `llm_provider.py`: `complete_batch`/`complete_json_batch` 추가. `AnthropicProvider`가 배치 제출→폴링→`custom_id` 수거. Stub/OpenAI는 동기 폴백.
  - `llm_prefilter.py`·`llm_ranker.py`·`llm_translate.py`·`briefing.py`: 기사별 순차 호출 → "요청 일괄 구성→배치→결과 반영"으로 전환.
  - `config.py`: `LLM_USE_BATCH`(기본 on)·`LLM_BATCH_MIN/CHUNK/POLL_SEC/MAX_WAIT_SEC`.
  - `main.py`: prefilter/rank/translate/brief/ai에 **`--sync`**(배치 끄고 동기, 디버깅용). SDK 표면(create/retrieve/results, request_counts, result.type) 확인.

#### UI/데이터 개편
- **규제·정책 → TopicWatch 흡수** (5탭→**4탭**): `web/regulations.html` nav 제거·생성 중단, TopicWatch `_ISSUES`에 **규제·감독 클러스터** 추가.
- **ai_score ACTIVE 임계 60→55**: 임계 바로 아래(50~59)에 준수한 거시·금융 뉴스가 몰려 노출 폭 확대. export만 재실행하면 반영(AI 재호출 X).
- **Global Pulse '오늘의 핵심 흐름' 배지 인라인화**: 왼쪽 고정폭 배지 컬럼 제거 → `[국기][배지] 제목` 한 줄, 본문 시작점 정렬.
- **KO/EN 폴백**: 표시 언어가 비면 반대 언어로 폴백(`tq/tk/tf`) — 미번역분도 빈칸 없이 노출. 4개 화면 적용.

#### 현지언론 화면 재구성
- **국가 선택 게이팅**: 국가 미선택 시 목록 숨김·안내만. 커버리지에서 거점 클릭 시 브리핑+기사 노출.
- **일일 브리핑 박스**(목록 상단): 선택 거점의 **전일+당일** 뉴스를 AI가 4~5문장 한/영 동시 종합.
  - `briefing.py`: `--type daily`(전일+당일, `summary`+`summary_en` 한/영, 배치) 추가. `country_briefings.summary_en` 컬럼 보강.
  - `export_json.py`: `_daily_briefs()` → 국가별 최신 daily 브리핑을 `countries.json`의 국가별 `brief`{ko,en,date} 필드로 주입.
  - `main.py ai` 4단계가 이제 **일일 브리핑** 생성.

#### 수집 심화 — 본문 추출 (무료, rank 품질↑)
- **진단**: 76/118 피드가 Google News RSS → summary 평균 141B(제목 수준 스니펫)로만 분석. keep 802건 중 **490건이 아직 `news.google.com` 리다이렉트 링크**(구식 redirect-follow 해소 실패).
- **`fulltext.py` 신규**: prefilter 통과분만 원문 URL을 열어 본문 전체 추출(`trafilatura`) → `articles_raw.full_text`. Google News 링크는 `googlenewsdecoder`(현행 batchexecute 방식)로 실제 URL 해소 후 추출. 실패 시 스니펫 폴백. 병렬 fetch.
- **파이프라인 편입**: prefilter → **fulltext** → rank. `llm_ranker` 는 `full_text` 있으면 본문으로, 없으면 스니펫으로 자동 분석. `main.py fulltext` + `ai` 5단계로 확장. `config`에 FULLTEXT_*·RANK_BODY_MAXLEN. `requirements.txt`에 trafilatura·googlenewsdecoder.
- **실행 환경**: 수집처럼 **맥북(개방망)**. 방향 결정 = 무료 우선(유료 API는 보류).
- **다음 후보(무료)**: GDELT breadth, OFFICIAL 12피드 활성화, 구글뉴스 쿼리 확대.

#### 운영 메모
- 실제 AI 실행은 맥북(ANTHROPIC_API_KEY): `pip install -r requirements.txt` 후 `python main.py ai --days 2`(2단계=본문추출, 5단계=일일 브리핑 포함) → `python main.py export`.
- 임계 55 하향 후 55~59 신규 노출분 KO 미번역 소수 → `translate --days 2`로 채움(당장은 EN 폴백으로 노출).

---

### 세션 9 (2026-08-28) — 인사동향·지표확장·모달 긴요약·태국라오스 편입
> 세션 8(2026-07-23) 이후 4탭 리브랜딩(글로벌 원팀 뉴스/국가별 뉴스/모니터링/주간 리포트)·
> taxonomy.yaml·이슈 트래커 카테고리 탭 등 여러 세션이 있었으나 이 로그에는 미기록 —
> 최신 확정 상태는 `docs/_INDEX_현황.md`·`STATUS.md` 참조.

#### 인사동향(리더십 교체) 신설
- `keyword_filter.py`: 중앙은행·은행·감독당국 "역할어"(bank ceo 등) 단독 매치는 재직 중
  발언 인용까지 다 잡아 오탐 심함(1차 21건 중 다수 단순 언급) → 역할어 + 교체/이동
  신호어(appointed/resigned/ex-/outgoing 등) **AND 매치**로 전환(재검증 6건, 전부 실제
  인사 이벤트). `personnel_move` 컬럼, `run_personnel_tag()`.
- `export_json.py` `_compute_personnel()`: 진출 13 + 미진출 13 전체 대상, ACTIVE 게이트
  없음(신호 희소). `countries.html` 4번째 탭 — 은행명/주제 세부 필터 칩은 제거하고
  전체 목록 + 건수만(신호가 너무 희소해 필터링이 무의미).

#### 국가별 금융지표 확장
- `config.POLICY_RATES`: 중앙은행마다 API 형식이 달라 표준 무료 API가 없어 소형 표로
  직접 관리(as_of 명시, 수동 갱신). SG(MAS는 S$NEER 밴드 운용)·KH(달러화 경제)는
  단일 정책금리가 없어 표에서 제외 — export가 자동 생략.
- `config.BOND10Y_MAP`: yfinance·stooq 실측 결과 무료·무키로 안정적인 건 미국(`^TNX`)뿐
  (stooq는 최근 JS 봇 차단 걸림). 미국만 수집, 나머지 생략.
- 스파크라인: 새 수집원 없이 기존 일별 `indicators` 스냅샷 재사용, 최근 7영업일 미니 SVG.
- **버그 수정**: index 등락 배지가 yfinance 자체 5일 히스토리의 전일종가를 쓰고
  스파크라인은 DB 스냅샷을 써서 하루 중 재수집 시 배지↑ 스파크↓ 같은 모순 발생 —
  index도 fx와 동일하게 DB 전일 스냅샷을 prev_value로 쓰도록 통일.

#### 모달 긴 요약(expanded_summary)
- `llm_expand.py` 신규: 카드용 짧은 요약(q)과 별개로 모달 전용 10~20줄(4~6문단) 요약.
  **노출(ACTIVE) 기사에만** 생성(전량 생성 금지, 비용 관리), 증분(`expanded_summary IS NULL`),
  Haiku + Batches. `llm_ranker._cluster_sources()`의 duplicate_of 클러스터를 재사용해
  다출처 종합(단일 기사 패러프레이즈 방지, 저작권 완화).
- `shared-modal.js`는 이미 이 필드의 폴백 구조를 갖고 있었음(선행 세션에서 준비만 해둠) —
  이번에 실제 데이터를 채움. `.sh-sum`에 `white-space:pre-line` 추가해 문단 줄바꿈 렌더.

#### 태국·라오스 진출국 편입 (진출 11→13, 미진출 14→13)
- 실측 후 착수: THB·LAK 환율(open.er-api 확인) / SET지수(`^SET.BK`, 1600.70 실측 = 실제
  종가 일치, 다만 yfinance 히스토리가 1일치만 반환돼 등락은 DB 스냅샷 누적으로 채워짐) /
  BOT 정책금리 1.00%(2026-08-26). 라오스는 정책금리 시드값 미확보로 생략.
- `sources.yaml`(태국 재분류+Bangkok Post·Nikkei Asia 보강, 라오스 신규 GNews),
  `config.py`(INDICATOR_MAP/POLICY_RATES/NON_PRESENCE_COUNTRIES), `kb_network.py`
  (실제 지점 없음 → "관심시장"), `export_json.py`(`_FLAGS_ALL`/`_PRESENCE_NAMES`),
  3개 웹페이지 NAMES 딕셔너리 전부 반영.

#### 운영 메모
- 오늘 실행: `main.py run`(수집, 태국·라오스 신규 피드 포함) → `main.py ai`(7단계,
  expand 신규 편입) → `main.py indicators` → `main.py export` → `deploy_web.sh`.
- 디자이너 공유용으로 `data/export/for_designer/`에 4개 화면 + `shared-modal.js` 사본 보관.

---

## 현재 관리 국가 (KB 거점 기준)

| 코드 | 국가 | 도시 | 형태 | 주요 매체 수 |
|---|---|---|---|---|
| GB | 영국 | 런던 | 지점 | 5개 |
| US | 미국 | 뉴욕 | 지점 | 7개 |
| HK | 홍콩 | 홍콩 | 지점 | 4개 |
| CN | 중국 | 베이징 | 법인 | 6개 |
| JP | 일본 | 도쿄 | 지점 | 7개 |
| SG | 싱가포르 | 싱가포르 | 지점 | 4개 |
| IN | 인도 | 구르구람 | 지점 | 7개 |
| VN | 베트남 | 하노이 | 법인 | 6개 |
| MM | 미얀마 | 양곤 | 사무소 | 9개 |
| ID | 인도네시아 | - | 자회사(KBI은행) | 7개 |
| KH | 캄보디아 | - | 자회사(프라삭은행) | 8개 |
| TH | 태국 | 방콕 | 관심시장(지점 없음) | 3개 |
| LA | 라오스 | 비엔티안 | 관심시장(지점 없음) | 1개 |

> TH·LA는 2026-08-28 제품 기준으로 진출국 편입(실제 KB 지점·법인 없음) — 상세는 위 세션 9.

---

## 서버 동기화 방법

```bash
# 맥북에서 수집만
python main.py run

# 수집 후 서버로 전송
./sync_to_server.sh --collect

# DB만 서버로 전송
./sync_to_server.sh
```

---

## 다음 과제

> 아래 2026-07 목록 중 taxonomy.yaml·kb_implication 컬럼·llm_prefilter/ranker 이식·
> 새 UI 데이터 연동은 이후 세션들에서 완료됨(이 로그엔 미기록 — `STATUS.md` 참조).
> 최신 우선순위는 `STATUS.md` 8장·`docs/_INDEX_현황.md` "다음 할 일" 기준.

### 수집원 보강
- [ ] `OFFICIAL`/tier0 당국 피드 활성화 (규제 화면)
- [ ] ID·KH 자회사 IR·공시 수집원 추가 (자회사 화면)
- [ ] 태국·라오스 큐레이션 매체 추가 확보(현재 최소 소스만)

### 기타
- [ ] AI 프로바이더 실험 (Anthropic 외)
- [ ] 정기 수집 자동화 (맥북 cron 또는 스케줄러)
- [ ] 라오스 정책금리 시드값 확보(신뢰 가능한 무료 소스 미발견)

> RTHK 피드는 2026-08-28 소스 정리 때 제거(XML 파싱 계속 실패, 피드 자체 문제).
> `web/regulations.html`(TopicWatch에 흡수돼 죽어있던 페이지)·`web/index_v2.html`
> (커밋된 적 없는 미완성 프로토타입)도 같이 정리.
