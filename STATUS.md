# 현황 / 확정안 대비 정합 — glb-one-teams

> 최초 작성 2026-05-29(prototype 기준) | glb-one-teams 재작성 2026-07-14 | For Internal Use Only
>
> 본 문서는 **확정안 ↔ go-forward 레포(`glb-one-teams`) 현황 브리지**입니다. 제품 비전·로드맵은 [`PLAN.md`](PLAN.md), 새 UI 화면 설계는 [`화면분석_개발가이드.md`](화면분석_개발가이드.md), 수집·AI·카테고리 설계는 [`데이터_AI_카테고리_설계.md`](데이터_AI_카테고리_설계.md), 모듈·사용법은 [`README.md`](README.md)·[`CLAUDE.md`](CLAUDE.md), 작업 이력은 [`docs/work_log.md`](docs/work_log.md)를 참조하세요.
>
> **📌 2026-07-14 재분석.** 사내 확정본 「글로벌 One Team 뉴스데일리 구축(안)」(2026-07-13) 반영본을 프로토타입에서 이관하고, 이 레포의 실제 구조에 맞춰 정합화했습니다. (프로토타입 전용 기술 상세 — Streamlit 대시보드·`score_engine` 온도계·WAL 복구 등 — 는 `glb-news-rss/prototype/STATUS.md`에 그대로 있습니다.)

---

## 1. 두 레포 관계

| 레포 | 역할 | 상태 |
|---|---|---|
| **glb-one-teams** (이 레포) | 수집 전용 코어(AI 없음) + 선택적 AI 레이어 + 새 6탭 UI 실험 | **go-forward 베이스** |
| glb-news-rss/prototype | 풀 파이프라인 + KB 대시보드(Streamlit, 로컬 전용) | 참조·아카이브 |

GitHub: `https://github.com/lSusial/glb-one-teams.git` · UI 레퍼런스(샘플): `https://uandix-kaneiko.github.io/global_One_Team/`

핵심 설계 원칙: **수집기는 AI 없이 두고, AI를 별도 모듈/단계로 추가**한다. 코어(`run`)는 API 키 없이 동작하고, AI 단계(`ai`)는 `ANTHROPIC_API_KEY`가 있을 때만 실행된다.

---

## 2. 파이프라인 현황

```
[수집·필터 — AI 없음, ✅ 동작]              [AI 레이어 — 코드 존재·미실행]        [export]
 fetch → keyword_filter → dedup    ──▶   prefilter → rank → briefing   ──▶   export(JSON) → UI
 (collector)   (keyword_filter)          (llm_prefilter)(llm_ranker)(briefing) (export_json)
```

| 단계 | 모듈 | 상태 |
|---|---|---|
| 수집 (fetch) | `collector.py` | ✅ 구현 (RSS 병렬 + Google News 우회·실URL 해소) |
| 키워드 필터 + 중복 | `keyword_filter.py` | ✅ 구현 (제목/본문 분리 점수제, 합산 ≥3 통과) |
| 공통 인프라 | `config.py` / `db.py` | ✅ 경로·임계값·모델 단일 출처, PRAGMA·멱등 마이그레이션 |
| 주제 분류 정의 | `taxonomy.yaml` / `taxonomy.py` | ✅ 정의 완료 (5종 + 라우팅), AI가 채우는 것은 대기 |
| LLM 1차 관문 | `llm_prefilter.py` | 🟡 코드 존재·**미실행** (키워드 통과분 keep/drop) |
| AI 분석 | `llm_ranker.py` | 🟡 코드 존재·**미실행** (`ai_score`·`summary_ko`·`topics`·`kb_implication`) |
| 국가 브리핑 | `briefing.py` | 🟡 코드 존재·**미실행** (`country_briefings`) |
| 프로바이더 추상화 | `llm_provider.py` | ✅ Anthropic(실제)·OpenAI(스캐폴드)·Stub(오프라인) 팩토리 |
| UI 데이터 export | `export_json.py` | 🟡 스캐폴드 (`data/export/*.json` 데이터 계약) |
| 새 6탭 UI | (샘플 SPA) | 🔴 하드코딩 목업만, 실데이터 미연동 |

> AI 모듈은 `py_compile` + 순수 헬퍼 단위검증만 통과 — **실제 네트워크 수집·API 호출은 미실행**(work_log 세션5·6). 모델: prefilter=haiku, rank/brief=sonnet (`config.py`), `ai_score` 0~100·ACTIVE 임계 60.

---

## 3. 확정안 요구 → 구현 상태 (기술 관점)

제품 관점 매핑은 [`PLAN.md` 확정안 요약](PLAN.md) 표 참조. 기술 관점 요약:

| 확정안 요구 | 이 레포 상태 | 남은 일 |
|---|---|---|
| 자동 수집·필터·중복제거 | ✅ 동작 | 정기 자동화(cron/스케줄러) |
| AI 중요도·요약·주제분류 | 🟡 코드 존재·미실행 | 실행 + 평가셋 검증, `topics` 채우기 |
| KB 시사점(Key-man 맥락) | 🟡 `kb_implication` 컬럼만 | `llm_ranker` 실행 시 생성(맥락=`kb_network.py`) |
| 대시보드/UI | 🔴 목업 | `countries.json`부터 실데이터 연동(Phase 1) |
| 한·영 토글 번역 | 🟡 UI 골격, EN stub | 백엔드 번역·EN 데이터 생성 |
| 메신저 배포 | 🔴 미구현 | Pages(정적 JSON) vs 메신저 봇 방식 결정 |
| Key-man/IR·규제·거시지표 | 🔴 수집원 없음 | OFFICIAL 활성화, 자회사 IR·지표 피드 추가 |
| 참여형 재미요소 | ⛔ 이번 범위 제외 | 향후 재검토 (2026-07-14) |

> **이번 진행 범위(2026-07-14):** 뉴스 분석으로 도출 가능한 항목은 포함(현지언론+AI·온도계·Key-man 인사동향·규제 화면), 비-뉴스 소스 필요 항목은 보류(자회사 IR 링크·OFFICIAL 당국 피드·거시지표 빅넘버·재미요소). 상세는 [`PLAN.md` '이번 진행 범위'](PLAN.md).

---

## 4. 거점·소스 현황

- **11개 거점**: GB·US·HK·CN·JP·SG·IN·VN·MM·ID·KH (상세·매체 수는 [`PLAN.md` 8장](PLAN.md)). 홍콩은 `HK`로 중국(`CN`)과 분리, 싱가포르·영국 포함 — 프로토타입의 GB/SG/HK 미결 이슈 해소됨.
- **소스**: 총 **88개 소스 / 106개 피드**. 카테고리(축 A) = `GLOBAL_GENERAL`/`GLOBAL_ECONOMY`(글로벌) · 국가코드(전용) · `OFFICIAL`(당국).
- **`OFFICIAL` 당국 피드는 tier 0 비활성** → 규제·금융기관 화면용으로 활성화 필요.
- 국가 추가/변경 시 `sources.yaml` 수정(매체 categories는 국가코드 1개만, 중복 금지).

---

## 5. 운영·배포 현황

- **수집 운영**: 맥북에서 `python main.py run` → `./sync_to_server.sh`로 Oracle Cloud 서버 rsync. (서버 접속 정보는 `CLAUDE.md` 참조)
- **Google News 503**: Oracle Cloud IP는 Google News RSS에서 503 차단 → **맥북 수집 → 서버 동기화** 패턴으로 운영. 프로덕션은 Residential Proxy 검토.
- **배포 방향**: 새 UI는 GitHub Pages(정적 `data/*.json` fetch) 기준 설계. 확정안의 메신저(Telegram/WhatsApp/Jalo/카카오톡) 링크 배포와의 연결 방식은 미결([`PLAN.md` 10장](PLAN.md)).
- **정기 수집 자동화 미구현** (수동 실행 단계).

---

## 6. 알려진 이슈

- **RTHK 피드**: XML SAXParseException으로 수집 실패(피드 자체 문제).
- **Google News 503**: 위 5장 — 맥북 수집으로 우회.
- **샘플 UI 죽은 링크**: 신호·자회사·토픽의 `url`이 전부 `'#'` → 실제 수집 URL로 교체 필요.

---

## 7. 다음 과제

즉시 과제는 [`docs/work_log.md`](docs/work_log.md) "다음 과제" 및 [`PLAN.md` 11장](PLAN.md)과 일치:

1. **Phase 1 — 현지언론 e2e**: `export_json.py` → `countries.json` → 새 UI `fetch` 전환 (AI 없이 데이터 흐름 증명).
2. **Phase 2 — AI 레이어 실행**: `llm_prefilter`/`llm_ranker` 실행 + `taxonomy` 주제 채움 + 평가셋.
3. **수집원 보강**: OFFICIAL/tier0 활성화, 자회사 IR, 거시지표 피드.
4. **AI 프로바이더 실험**(Anthropic 외) + 정기 수집 자동화 + RTHK 피드 수정.

---

*최종 업데이트: 2026-07-14 (glb-one-teams 기준 재작성)*
