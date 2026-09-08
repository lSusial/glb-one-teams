# glb-one-teams 프로젝트 컨텍스트

## 프로젝트 목적
KB 글로벌 거점(13개국) 뉴스 데일리. 파이프라인: 수집 → 필터 → AI 분석 → export → **정적 4탭 UI**.
**★ 이 레포(glb-one-teams)가 메인이자 go-forward 레포다.** 신규 작업은 전부 여기서 한다.

## 코드 작성 AIs
claude_skill.md 내용을 참조 

## 관련 레포
- **glb-one-teams** (이 레포, ★메인): 수집 코어 + AI 레이어 + 정적 4탭 UI. GitHub: https://github.com/lSusial/glb-one-teams.git
- **glb-news-rss/prototype**: 구 Streamlit 대시보드 (로컬 전용, **참조·아카이브 레거시** — 여기서 신규 작업 안 함)
  - 경로: `/Users/sangminl/Documents/Claude/Projects/glb-news-rss/prototype/`

> **현재 상태·다음 작업은 [`STATUS.md`](STATUS.md) 참조.** 세션 상세는 [`docs/work_log.md`](docs/work_log.md).

## 서버 정보
- **Oracle Cloud:** `ubuntu@168.107.56.139` (포트 22)
- **SSH 키:** `~/workspace/ssh-key-2026-06-25-4.key`
- **접속:** `ssh -i ~/workspace/ssh-key-2026-06-25-4.key ubuntu@168.107.56.139`
- **서버 경로:** `/home/ubuntu/glb-one-teams/`
- **주의:** Oracle Cloud IP는 Google News RSS 503 차단됨 → 맥북에서 수집 후 rsync 동기화

## 수집 운영 방법 (개발 단계)
```bash
# 수집 후 서버 동기화 (맥북에서 실행)
./sync_to_server.sh --collect

# DB만 서버로 전송
./sync_to_server.sh

# 개별 실행
python main.py run     # fetch → filter → dedup
python main.py init    # DB 초기화 (sources.yaml 동기화)
python main.py list    # 최근 기사 확인
```

## 관리 국가 (KB 거점 기준, 진출 13개국)

| 코드 | 국가 | 도시 | 형태 |
|---|---|---|---|
| GB | 영국 | 런던 | 지점 |
| US | 미국 | 뉴욕 | 지점 |
| HK | 홍콩 | 홍콩 | 지점 |
| CN | 중국 | 베이징 | 법인 |
| JP | 일본 | 도쿄 | 지점 |
| SG | 싱가포르 | 싱가포르 | 지점 |
| IN | 인도 | 구르구람 | 지점 |
| VN | 베트남 | 하노이 | 법인 |
| MM | 미얀마 | 양곤 | 사무소 |
| ID | 인도네시아 | - | 자회사 (KBI은행, PT Bank KB Bukopin) |
| KH | 캄보디아 | - | 자회사 (프라삭은행, KB Prasac Bank) |
| TH | 태국 | 방콕 | 관심시장(2026-08-28 편입, 실제 지점 없음) |
| LA | 라오스 | 비엔티안 | 관심시장(2026-08-28 편입, 실제 지점 없음) |

미진출 13개국(통합 피드)은 `docs/design_미진출국.md` 참조.

## sources.yaml 규칙
- 국가 추가/변경 시 `sources.yaml` + `kb_network.py` (prototype 레포) 두 파일 동시 수정
- 매체 categories는 해당 국가 코드 하나만 (중복 금지)
- 글로벌 매체는 `GLOBAL`, 국가 전용 매체는 해당 국가 코드
- 소스·피드 수는 계속 늘고 있어 여기 하드코딩하지 않음 — `sources.yaml` 참조(수집 시 로그에 `feeds=N`으로 실측 출력)

## 현재 알려진 이슈
- Google News 피드: 서버에서 직접 수집 시 503 → 맥북 수집 후 rsync로 해결
- Google News 링크 해소: 구식 redirect-follow는 최신 consent/JS 리다이렉트에 실패 → `fulltext.py`가 `googlenewsdecoder`로 해소 후 본문 추출 (맥북 실행)

## 수집 심화 — 본문 추출 (fulltext.py, 무료)
- prefilter 통과분만 원문 본문 추출(`trafilatura`) → `articles_raw.full_text`, rank가 스니펫 대신 본문으로 분석. 파이프라인: prefilter → **fulltext** → rank. `main.py fulltext` / `ai` 5단계. 패키지: trafilatura·googlenewsdecoder. **뉴스 수집 강화 방향 = 무료 우선(유료 API 보류).**

## 기획·현황 문서 (2026-07-13 확정안 반영)
- `PLAN.md` — 기획 확정안·비전·11개 거점·로드맵 (기획 레이어). ⚠️ 문서 내 "6탭 UI" 구조는 구안 — 실제는 4탭(아래 참조)
- `STATUS.md` — 확정안 대비 구현 현황·두 레포 관계 (기술 브리지, ★최신 상태 기준)

## UI 디자인 시스템 · 설계 문서 (2026-09 리디자인 — ★현재 기준)
- **기준 문서:** `mockups/HANDOFF.md` — 화면별 사양·데이터 계약·구현 매핑(목업 4개: `pulse`/`country_detail`/`non_presence`/`topics`). 4탭(`web/{brief,countries,topics,weekly}.html`) 전면 리스킨 완료(2026-09-04).
- **디자인 시스템:** 무채색+골드 강조, 커스텀 SVG 국기(`web/shared-sprite.js`), 공용 토큰·컴포넌트 CSS(`web/shared-tokens.css`), 공용 기사 모달(`web/shared-modal.js`) — 전부 `main.py export`가 `data/export/`로 복사.
- **AI 레이어 현황:** `schema.sql`에 AI 컬럼(`llm_prefilter`, `ai_score`, `summary_ko`, `topics`, `kb_implication`) + `country_briefings` 완비, 매일 운영 중. 모델=Haiku, **Message Batches API(50% 할인) 기본**(`--sync`로 동기 전환). ai_score ACTIVE 임계=**55**. 표시 정렬은 `ranking.py`의 rank_score(다매체 커버리지+매체tier+최신성 등, 상세 `docs/rank_score_spec.md`) — ai_score 게이트 자체는 불변.
- **카테고리 3축:** 지역(`sources.yaml`) / 관련성게이트(`keyword_filter.py`) / 주제(`taxonomy.yaml`, AI `topics`로 분류).

## 다음 과제 (우선순위) — 2026-09-08 갱신, 상세는 `STATUS.md` 8장
1. 정기 수집 자동화(Oracle Cloud cron)
2. ESG 소스 보강 — `docs/esg_coverage_patch.md` 패치안 적용(분류는 정상, 수집 공백이 원인)
3. Telegram 채널 발송(영어판)
4. OFFICIAL/tier0 당국 피드 활성화, 태국·라오스 매체 확보
5. rank_score 재튜닝(현재 라벨 30건 기반, `docs/rank_score_spec.md` §8)

> ⛔ **진행 범위 원칙:** 뉴스 분석으로 도출 가능한 항목 포함 / 비-뉴스 소스 필요 항목 보류. 포함: 현지언론+AI(요약·시사점·분류·**국가 일일 브리핑**)·Global Pulse 지도·인사동향(뉴스)·**TopicWatch**·**국가별 거시지표(환율·주가지수·정책금리, `indicators.py`)**·**KB 미진출국 통합 피드**(`docs/design_미진출국.md`). 보류: 재미요소·참여형, 자회사 IR 링크, OFFICIAL 당국 원천 피드.
