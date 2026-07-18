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
- **[검토] Phase 2 비용 최적화**: rank가 Sonnet이라 레거시(Haiku) 대비 토큰 비용↑ (+ kb_implication 출력 추가, 1호출=1기사). prompt caching은 시스템 프롬프트 177토큰(<1024 최소치)이라 무효. 레버 — ① `rank --fast`(Haiku) 토글 추가해 품질 A/B, ② N건/호출 배치(시스템 프롬프트 분할, 출력비용은 그대로). 물량은 `--days`로 이미 축소. **미구현 — 필요 시 진행.**

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

### 설계 (문서화 완료, 구현 대기) — 상세: `데이터_AI_카테고리_설계.md`
- [ ] `taxonomy.yaml` 신설 — 주제코드 5종(MARKET/BANKING/DIGITAL/ESG/RISK) ↔ UI 필터 1:1 **(1순위)**
- [ ] `articles_raw.kb_implication` 컬럼 추가 (UI KB 시사점) **(2순위)**
- [ ] `llm_prefilter.py` / `llm_ranker.py` 이식 + 프로바이더 추상화 계층
- [ ] AI 분류·요약 평가셋 구축 (프로바이더 비교 기준)

### 수집원 보강
- [ ] `OFFICIAL`/tier0 당국 피드 활성화 (규제 화면)
- [ ] ID·KH 자회사 IR·공시 수집원 추가 (자회사 화면)
- [ ] 거시지표(금리·환율) 피드 — 빅넘버용

### 기타
- [ ] 새 UI 데이터 연동 (현지언론 화면부터 엔드투엔드) — 상세: `화면분석_개발가이드.md`
- [ ] AI 프로바이더 실험 (Anthropic 외)
- [ ] 정기 수집 자동화 (맥북 cron 또는 스케줄러)
- [ ] RTHK 피드 XML 오류 수정 (SAXParseException)
