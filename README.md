# glb-one-teams

KB 글로벌 거점 뉴스 수집·분석 파이프라인.

수집·필터·중복제거(AI 없음)가 코어이며, 그 위에 **선택적 AI 레이어**(분류·요약·KB 시사점·국가 브리핑)와 UI용 JSON export를 둔다. 새 UI 및 AI 프로바이더 실험을 위한 베이스 레포.

## 구조

```
[수집·필터 — AI 없음]                 [AI 레이어 — 선택, API 키 필요]        [export]
fetch → keyword_filter → dedup  ──▶  prefilter → rank → briefing      ──▶  export(JSON)
(collector)  (keyword_filter)        (llm_prefilter)(llm_ranker)(briefing)  (export_json)
```

- 코어(`run`)는 AI 없이 동작한다 — 수집·필터·중복제거만.
- AI 단계(`ai`)는 `ANTHROPIC_API_KEY`가 있어야 실행되며, 키가 없으면 안내 후 중단된다(코어에는 영향 없음).
- 설계 문서: 수집/AI/카테고리 = `데이터_AI_카테고리_설계.md`, UI 화면 사양 = `mockups/HANDOFF.md`, 현재 구현 현황 = `STATUS.md`.

## 모듈 구성 (파일 역할)

**수집·필터 (AI 없음)**

| 파일 | 역할 |
|---|---|
| `main.py` | CLI 진입점 — 서브커맨드 디스패치(init/fetch/filter/dedup/run/ai/export 등) |
| `collector.py` | RSS 병렬 수집, Google News 우회·실제 URL 해소, `sources.yaml`↔DB 동기화, 가용성 리포트 |
| `keyword_filter.py` | 2단계 키워드 점수 필터(제목/본문 분리) + 제목 유사도 기반 중복 탐지 |
| `sources.yaml` | 매체·피드·카테고리 카탈로그 (106 소스 / 138 피드) |
| `schema.sql` | SQLite 스키마 (`articles_raw`, `media_*`, `country_briefings`) |

**공통 인프라**

| 파일 | 역할 |
|---|---|
| `config.py` | 경로·임계값·LLM 모델·수집 튜닝 상수의 단일 출처 |
| `db.py` | DB 연결(PRAGMA) + 멱등 컬럼 마이그레이션 헬퍼(`ensure_columns`) |

**AI 레이어 (별도 실행 · `ANTHROPIC_API_KEY` 필요, 매일 운영 중)**

| 파일 | 역할 |
|---|---|
| `llm_provider.py` | 프로바이더 추상화 — Anthropic(실제) / OpenAI(스캐폴드) / Stub(오프라인) + 팩토리, Batches API 배선 |
| `llm_prefilter.py` | LLM 1차 관문 — 키워드 통과분 중 무관·노이즈 keep/drop |
| `fulltext.py` | keep 기사 원문 본문 추출(trafilatura+googlenewsdecoder, 무료) — rank 품질↑ |
| `llm_ranker.py` | AI 분석 — `ai_score`·`summary_en`·`topics`·`kb_implication_en` 생성(영어 canonical) |
| `ranking.py` | 표시용 복합 정렬 `rank_score`(다매체 커버리지+매체tier+최신성 등) — ai_score 게이트는 불변, 상세 `docs/rank_score_spec.md` |
| `llm_expand.py` | 노출(ACTIVE) 기사만 모달용 긴 요약(`expanded_summary`, 다출처 종합) |
| `llm_translate.py` | 영어 canonical → 한국어 표시분 번역(`summary_ko`·`kb_implication`) |
| `briefing.py` | 국가별 일일/주간 브리핑 + 오늘의 글로벌 핵심(`daily_highlights`) |
| `indicators.py` | 국가별 거시지표(환율·주가지수·정책금리·미국 10년물 국채) 일별 스냅샷 |
| `taxonomy.py` / `taxonomy.yaml` | 주제 분류(MARKET/BANKING/DIGITAL/ESG/RISK) + 화면 라우팅 정의·로더 |
| `kb_network.py` | KB 거점(지점/법인/자회사) 정의 — 시사점 생성 맥락 주입 |
| `export_json.py` | DB → `data/export/*.json` (UI 데이터 계약, `web/*.html` 템플릿에 주입) |

**문서**

| 파일 | 역할 |
|---|---|
| `CLAUDE.md` | 프로젝트 컨텍스트(거점·규칙·로드맵) |
| `STATUS.md` | 확정안 대비 구현 현황(가장 최신) |
| `docs/work_log.md` | 작업 이력 |
| `mockups/HANDOFF.md` | 현재 UI(4탭) 화면 사양·데이터 계약 — 목업 기준 |
| `데이터_AI_카테고리_설계.md` | 수집 데이터 / AI 산출물 / 3축 카테고리 설계(taxonomy.yaml 근거) |

## 관리 국가

| 코드 | 국가 | KB 거점 |
|---|---|---|
| GB | 영국 | 런던 지점 |
| US | 미국 | 뉴욕 지점 |
| HK | 홍콩 | 홍콩 지점 |
| CN | 중국 | 베이징 법인 |
| JP | 일본 | 도쿄 지점 |
| SG | 싱가포르 | 싱가포르 지점 |
| IN | 인도 | 구르구람 지점 |
| VN | 베트남 | 하노이 법인 |
| MM | 미얀마 | 양곤 사무소 |
| ID | 인도네시아 | KBI은행 (자회사) |
| KH | 캄보디아 | 프라삭은행 (자회사) |
| TH | 태국 | 방콕 (관심시장, 실제 지점 없음) |
| LA | 라오스 | 비엔티안 (관심시장, 실제 지점 없음) |

## 사용법

```bash
# 환경 세팅
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# DB 초기화
python main.py init

# 수집 (AI 없음) — fetch → filter → dedup
python main.py run

# 개별 실행
python main.py fetch    # 피드 수집
python main.py filter   # 키워드 필터 (--refilter 로 전체 재처리)
python main.py dedup    # 중복 제거
python main.py list --limit 20   # 최근 기사 확인
python main.py report            # 매체 가용성 리포트
```

### AI 레이어 (선택 · `ANTHROPIC_API_KEY` 필요)

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # 키 없으면 안내 후 중단
python main.py ai        # prefilter → fulltext → rank → expand → translate → brief → highlights 일괄(7단계)
#  또는 개별:
python main.py prefilter # LLM 1차 관문 (keep/drop)
python main.py rank      # 점수·요약(en)·topics·KB시사점(en)
python main.py brief     # 국가별 브리핑
python main.py indicators  # 거시지표(환율·지수·정책금리) 수집

python main.py export    # DB → data/export/*.json (UI용, web/*.html에 주입)
```

> 모델은 `config.py`에서 작업별로 분리 — 현재 전 단계 Haiku + Message Batches API(토큰 50%↓, `--sync`로 동기 전환).
> 프로바이더 교체는 `LLM_PROVIDER`(anthropic|openai|stub) 환경변수.

## 서버 동기화

맥북에서 수집 후 Oracle Cloud 서버로 rsync:

```bash
./sync_to_server.sh            # DB만 전송
./sync_to_server.sh --collect  # 수집 후 전송
```

> **참고:** Oracle Cloud IP는 Google News RSS에서 503 차단됨.
> 개발 단계에서는 로컬(맥북) 수집 → 서버 동기화 방식으로 운영.

## 매체 현황

- 총 106개 소스, 138개 피드
- Google News 우회 피드 다수 포함 (직접 RSS가 막힌 매체)
- 중앙은행/공식기관 피드는 비활성(tier 0) 상태로 관리
