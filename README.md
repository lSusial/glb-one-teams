# glb-one-teams

KB 글로벌 거점 뉴스 수집·분석 파이프라인 + 정적 UI.

수집·필터·중복제거(AI 없음)가 코어이며, 그 위에 **AI 레이어**(분류·요약·KB 시사점·국가 브리핑)와 UI용 JSON export를 둔다.
현재 상태는 [`STATUS.md`](STATUS.md), 에이전트 작업 지침은 [`CLAUDE.md`](CLAUDE.md).

## 구조

```
[수집·필터 — AI 없음]              [AI 레이어 — ANTHROPIC_API_KEY 필요]                        [export]
fetch → keyword_filter → dedup ─▶ prefilter → fulltext → rank → ai-dedup ─▶ export(JSON)
(collector) (keyword_filter)      → expand → translate → brief → highlights   (export_json)
```

- 코어(`run`)는 AI 없이 동작한다 — 수집·필터·중복제거만.
- AI 단계(`ai`)는 `ANTHROPIC_API_KEY`가 있어야 실행되며, 키가 없으면 안내 후 중단된다(코어에는 영향 없음).
- 설계: `docs/design.md`, UI 사양: `mockups/HANDOFF.md`, 운영 절차: `docs/operations.md`.

## 모듈 구성

**수집·필터 (AI 없음)**

| 파일 | 역할 |
|---|---|
| `main.py` | CLI 진입점 — 서브커맨드 디스패치 |
| `collector.py` | RSS 병렬 수집, Google News 우회·URL 해소, `sources.yaml`↔DB 동기화, 가용성 리포트 |
| `keyword_filter.py` | 키워드 점수 필터(통과 임계 2, SOCIETY 독립 통과 경로), 제목 유사도 중복 탐지, 인사동향·한국계 금융기관 태깅, 주제국가 키워드 폴백 |
| `sources.yaml` | 매체·피드·카테고리 카탈로그 (수집 로그의 `feeds=N` 참조) |
| `schema.sql` | SQLite 스키마 (`articles_raw`, `media_*`, `country_briefings` 등) |

**공통 인프라**

| 파일 | 역할 |
|---|---|
| `config.py` | 경로·임계값·LLM 모델·수집 튜닝·지표 매핑 상수의 단일 출처 |
| `db.py` | DB 연결(PRAGMA) + 멱등 컬럼 마이그레이션 헬퍼(`ensure_columns`) |

**AI 레이어 (매일 운영 중)**

| 파일 | 역할 |
|---|---|
| `llm_provider.py` | 프로바이더 추상화 — Anthropic(실제)/OpenAI(스캐폴드)/Stub(오프라인) + Batches API 배선 |
| `llm_prefilter.py` | LLM 1차 관문 — 키워드 통과분 중 무관·노이즈 keep/drop |
| `fulltext.py` | keep 기사 원문 본문 추출(trafilatura+googlenewsdecoder, 무료) |
| `llm_ranker.py` | `ai_score`·`summary_en`·주제 카테고리·이벤트유형·`primary_country`·`kb_implication_en` 생성(영어 canonical) |
| `ranking.py` | 표시용 복합 정렬 `rank_score` — ai_score 게이트는 불변 (`docs/rank_score_spec.md`) |
| `llm_expand.py` | 노출(ACTIVE) 기사 모달용 긴 요약(3~4문단, 다출처 종합, 얇은 소스는 경량 프롬프트) |
| `llm_translate.py` | 영어 canonical → 한국어 표시분 번역 |
| `llm_dedup.py` | AI 근접중복 판정 — 같은 사건의 다른 표현을 묶어 `duplicate_of` 마킹 |
| `briefing.py` | 국가별 일일/주간 브리핑 + 오늘의 글로벌 핵심(`daily_highlights`) |
| `indicators.py` | 국가별 거시지표(환율·주가지수·정책금리·미국 10년물) 일별 스냅샷 + 6개월 추세 |
| `taxonomy.py` / `taxonomy.yaml` | 주제 6종·이벤트유형 4종 정의·로더 |
| `kb_network.py` | KB 거점(지점/법인/자회사/관심시장) 정의 — 시사점 생성 맥락 주입 |

**출력·배포·평가**

| 파일 | 역할 |
|---|---|
| `export_json.py` | DB → `data/export/*.json` + `web/*.html` 템플릿에 데이터 주입 |
| `admin_export.py` | 관리자 페이지(`data/export/admin.html`) 생성 |
| `broadcaster.py` | 브리핑 → Telegram 채널 발송 |
| `deploy_web.sh` / `sync_to_server.sh` | 화면 배포 / 맥북→서버 rsync |
| `web/` | 정적 UI 템플릿 — `brief`(홈)·`countries`(뉴스)·`topics`(모니터링)·`markets`(지표)·`weekly`(주간)·`links`(원문 링크)·`intro`·`admin` + 공용 `shared-*` |
| `mockups/` | 디자인 목업 4종 + `HANDOFF.md` |
| `eval/` | 프리필터·랭커 평가셋(v2 149건)과 평가 스크립트 |
| `tests/` | 품질 파이프라인 회귀 테스트 (`python -m unittest discover -s tests -v`) |

## 관리 국가

진출 13개국 GB·US·HK·CN·JP·SG·IN·VN·MM·ID·KH·TH·LA (TH·LA는 실제 지점 없는 관심시장) + KB 미진출 13개국 통합 피드.
거점 표·변경 시 손볼 파일 목록은 [`CLAUDE.md`](CLAUDE.md), 미진출국 구성은 `docs/design.md` §5.

## 사용법

```bash
# 환경 세팅
python -m venv .venv
.venv/bin/pip install -r requirements.txt

python main.py init      # DB 초기화 (sources.yaml 동기화)
python main.py run       # 수집(AI 없음) — fetch → filter → dedup
python main.py list --limit 20
python main.py report    # 매체 가용성 리포트
```

### AI 레이어 (`ANTHROPIC_API_KEY` 필요)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python main.py ai --days 2   # prefilter → fulltext → rank → ai-dedup → expand → translate → brief → highlights
python main.py ai-dedup --days 3   # AI 근접중복 판정만 단독 재실행
python main.py indicators    # 거시지표 수집
python main.py export        # DB → data/export/*.json (+ web 템플릿 주입)
```

개별 단계: `prefilter` · `fulltext` · `rank` · `ai-dedup` · `expand` · `translate` · `brief` · `highlights`.
부가 명령: `korean-fi` · `personnel` · `backfill-country` · `indicators-history` · `admin` · `broadcast` · `eval`.

> 모델은 `config.py`에서 작업별 분리 — 현재 전 단계 Haiku + Message Batches API(토큰 50%↓, `--sync`로 동기 전환).
> 프로바이더 교체는 `LLM_PROVIDER`(anthropic|openai|stub) 환경변수.

## 배포·서버 동기화

일일 절차(수집→AI→export→Cloudflare Pages 배포)는 [`docs/operations.md`](docs/operations.md).
맥북에서 수집 후 Oracle Cloud로 rsync: `./sync_to_server.sh [--collect]`

> Oracle Cloud IP는 Google News RSS에서 503 차단됨 — 수집은 맥북(가정용 IP)에서만 한다.
