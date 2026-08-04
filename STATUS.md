# 현황 / 확정안 대비 정합 — glb-one-teams

> 최초 작성 2026-05-29(prototype) | glb-one-teams 재작성 2026-07-14 | **전면 갱신 2026-07-29** | For Internal Use Only
>
> 본 문서는 **확정안 ↔ go-forward 레포(`glb-one-teams`) 현황 브리지**입니다. 제품 비전·로드맵은 [`PLAN.md`](PLAN.md), 화면 설계는 [`화면분석_개발가이드.md`](화면분석_개발가이드.md), 수집·AI·카테고리 설계는 [`데이터_AI_카테고리_설계.md`](데이터_AI_카테고리_설계.md), 작업 이력은 [`docs/work_log.md`](docs/work_log.md)를 참조하세요.

---

## 1. 두 레포 관계

| 레포 | 역할 | 상태 |
|---|---|---|
| **glb-one-teams** (이 레포) | 수집 코어(AI 없음) + AI 레이어 + 정적 4탭 UI | **go-forward 베이스** |
| glb-news-rss/prototype | 풀 파이프라인 + KB 대시보드(Streamlit, 로컬) | 참조·아카이브 |

GitHub: `https://github.com/lSusial/glb-one-teams.git` · UI 레퍼런스(샘플): `https://uandix-kaneiko.github.io/global_One_Team/`

핵심 원칙: **수집기는 AI 없이**(코어 `run`은 API 키 없이 동작), **AI는 별도 단계**(`ai`는 `ANTHROPIC_API_KEY` 있을 때만).

---

## 2. 파이프라인 현황 (AI 레이어 실행 완료 — 맥북)

```
fetch → keyword_filter → dedup → prefilter → fulltext → rank → translate → briefing → export → 정적 4탭 UI
```

| 단계 | 모듈 | 상태 |
|---|---|---|
| 수집 (fetch) | `collector.py` | ✅ RSS 병렬 + Google News 우회·실URL 해소 |
| 키워드 필터 + 중복 | `keyword_filter.py` | ✅ 제목/본문 점수제(≥3 통과), F1 77 |
| 공통 인프라 | `config.py` / `db.py` | ✅ 경로·임계·모델 단일 출처, 멱등 마이그레이션 |
| 주제 분류 | `taxonomy.yaml` / `taxonomy.py` | ✅ 5종(MARKET/BANKING/DIGITAL/ESG/RISK) + 라우팅 |
| LLM 1차 관문 | `llm_prefilter.py` | ✅ 구현·실행 (keep/drop) |
| **본문 추출** | `fulltext.py` | 🟡 **구현 완료·미실행·미커밋** (keep 원문→`full_text`) |
| AI 분석 | `llm_ranker.py` | ✅ 구현·실행 (`ai_score`·`summary_en`·`topics`·`kb_implication_en`) |
| 번역 | `llm_translate.py` | ✅ 구현·실행 (표시분 한국어) |
| 국가 브리핑 | `briefing.py` | ✅ 구현·실행 (daily/weekly, 일일 9개국 생성됨) |
| 프로바이더 추상화 | `llm_provider.py` | ✅ Anthropic·OpenAI(스캐폴드)·Stub + **Message Batches(50%↓)** |
| UI 데이터 export | `export_json.py` | ✅ `countries/pulse/keyman/topics.json` + 아카이브 |
| UI (정적 4탭) | `web/*.html` | ✅ 실데이터 연동 완료 |

> **비용 최적화**: 모델=Haiku, Message Batches API(토큰 50%↓, `--sync`로 동기), `--days` 물량 축소, 번역은 표시분만. 영어 canonical로 분석 후 한국어 번역. `ai_score` 0~100, **ACTIVE 임계 55**.

---

## 3. 확정안 요구 → 구현 상태

| 확정안 요구 | 상태 | 남은 일 |
|---|---|---|
| 자동 수집·필터·중복제거 | ✅ 동작 | 정기 자동화(스케줄) |
| AI 중요도·요약·주제분류 | ✅ 실행 | 평가셋 정식 검증 |
| KB 시사점 | ✅ `kb_implication` 생성 | — |
| 대시보드/UI | ✅ 정적 4탭 실데이터 | 배포(Pages) |
| 한·영 토글 번역 | ✅ 영어 canonical + 번역, 폴백 | — |
| 국가 일일 브리핑 | ✅ 현지언론 상단(전일+당일) | — |
| 수집 심화(본문) | 🟡 fulltext 코드완료·미실행 | 맥북 1회 실행·성공률 확인 |
| 메신저 배포 | 🔴 미구현 | Pages vs 봇 방식 결정 |
| 자회사 IR·OFFICIAL·거시지표 | ⛔ 이번 범위 보류 | 비-뉴스 소스 필요 |
| 참여형 재미요소 | ⛔ 범위 제외 | 향후 재검토 |

> **이번 진행 범위(2026-07-23 갱신):** 뉴스 분석으로 도출 가능한 항목 포함(현지언론+AI·일일브리핑·온도계·Key-man·**TopicWatch**(규제 흡수)). 보류: 자회사 IR·OFFICIAL 원천 피드·거시지표·재미요소.

---

## 4. 화면 구성 (정적 4탭 SPA)

규제·정책 화면은 **TopicWatch로 흡수**되어 6탭 → 4탭(+관리자).

**화면 구성 확정 (2026-07-29):**

| 탭 | 파일 | 데이터 창 | 구성 |
|---|---|---|---|
| ① Global Pulse | `brief.html` | **전일+당일** | 카테고리 온도계(5) + **오늘의 핵심 뉴스**(전 거점 국가무관·중요도순, 핵심흐름+주요뉴스 통합) |
| ② 현지언론 | `countries.html` | **전일+당일** | 거점 커버리지 → **국가 선택**(국가별) → 일일 브리핑 박스 + 기사 피드(카테고리 필터) |
| ③ Key-man | `keyman.html` | **주간** | 인사·리더십 동향(뉴스 키워드 추출) |
| ④ TopicWatch | `topics.html` | **주간** | 주제별 횡단 이슈 클러스터(규제·감독 포함) |
| (관리자) | `admin.html` | — | 수집 데이터 조회 |

- **데이터 창 정책**: Global Pulse·현지언론 = 전일+당일(오늘의 …) / Key-man·TopicWatch = 주간 흐름.
- **역할 분리**: Global = 전 국가 통합 노출 / 현지언론 = 국가별 분리.
- **카테고리 5분할**(2026-07-29): 경제(MARKET)·금융(BANKING)·디지털·ESG·리스크(RISK). 온도계·배지·필터 라벨·색상 일치. (이전엔 MARKET+BANKING이 '금융' 하나로 합쳐졌던 catch-all 해소.)
- **공통**: KR/EN 토글(한쪽 비면 반대 언어 폴백), 날짜 선택 + `archive/{date}/` 아카이브, 데이터 앵커(최신 게시일) 기준 창.

---

## 5. 거점·소스 현황

- **11개 거점**: GB·US·HK·CN·JP·SG·IN·VN·MM·ID·KH. 홍콩(`HK`)은 중국(`CN`)과 분리.
- **소스**: 총 **88 소스 / 118 피드** (76개가 Google News RSS). 축 A 카테고리 = `GLOBAL_*` / 국가코드 / `OFFICIAL`(당국, tier0 비활성).
- **수집 심화 방향 = 무료 우선**(유료 API 보류). 본문추출 후 다음 무료 레버: GDELT·OFFICIAL 활성화·구글뉴스 쿼리 확대.

---

## 6. 운영·데이터 현황

- **최신 데이터: 2026-07-23** (수 일 미갱신). 일일 브리핑 9개국 생성됨. **본문추출 0건**(미실행).
- **수집 운영**: 맥북 `python main.py run` → `./sync_to_server.sh` rsync (Google News 503 회피).
- **미커밋(2026-07-29)**: `fulltext.py`(신규) + `config.py`·`main.py`·`llm_ranker.py`·`requirements.txt`·설계문서 2종·`docs/work_log.md`·`CLAUDE.md`.
- **배포**: 정적 화면은 Pages 준비 완료, 실제 배포 미수행.
- **정기 자동화 미구현**(수동).

---

## 7. 알려진 이슈

- **RTHK 피드**: XML SAXParseException으로 수집 실패.
- **Google News 링크 해소**: 구식 redirect-follow는 최신 consent/JS 리다이렉트 실패(keep 490건 잔존) → `fulltext.py`가 `googlenewsdecoder`로 해소 후 본문추출(맥북 실행 필요).
- **금융 catch-all**: taxonomy MARKET+BANKING이 UI 'finance'로 합쳐져 금융 배지가 과다. 5분할 검토 대기.

---

## 8. 다음 과제

**즉시(맥북)**: 미커밋분 커밋 → 최신 수집 + `fulltext --days 2`(성공률 확인) + `ai --days 2` + `export`. → 묵은 데이터 갱신 & fulltext 첫 검증.

- ✅ **화면 구성요소 확정**(2026-07-29): 데이터 창 정책·핵심뉴스 통합·카테고리 5분할 완료(§4).

**그다음 트랙(택1)**:
1. **배포** — GitHub Pages 실 URL, export→push 경로.
2. **자동화** — 수집→AI→export 정기 스케줄.
3. **수집 강화(무료)** — GDELT·OFFICIAL 피드·구글뉴스 쿼리 확대.

---

*최종 업데이트: 2026-07-29*
