# glb-one-teams 프로젝트 컨텍스트

## 프로젝트 목적
KB 글로벌 거점 뉴스 수집 파이프라인. AI 없이 수집·필터·중복제거만 담당.
새 UI 및 AI 프로바이더 실험을 위한 베이스 레포.

## 관련 레포
- **glb-one-teams** (이 레포): 수집 전용, GitHub: https://github.com/lSusial/glb-one-teams.git
- **glb-news-rss/prototype**: 풀 파이프라인 + KB 대시보드 (Streamlit, 로컬 전용)
  - 경로: `/Users/sangminl/Documents/Claude/Projects/glb-news-rss/prototype/`

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

## 관리 국가 (KB 거점 기준)

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

## sources.yaml 규칙
- 국가 추가/변경 시 `sources.yaml` + `kb_network.py` (prototype 레포) 두 파일 동시 수정
- 매체 categories는 해당 국가 코드 하나만 (중복 금지)
- 글로벌 매체는 `GLOBAL`, 국가 전용 매체는 해당 국가 코드
- 현재: 88 sources, 106 feeds

## 현재 알려진 이슈
- RTHK 피드: XML SAXParseException 오류로 수집 실패 (피드 자체 문제)
- Google News 피드: 서버에서 직접 수집 시 503 → 맥북 수집 후 rsync로 해결

## 새 UI 레퍼런스 · 설계 문서
- **UI 레퍼런스(샘플):** https://uandix-kaneiko.github.io/global_One_Team/ — 단일 HTML SPA, 콘텐츠 하드코딩, 6탭 구조
- **설계 문서:**
  - `화면분석_개발가이드.md` — 화면별 구성·콘텐츠 + 데이터 연동 로드맵
  - `데이터_AI_카테고리_설계.md` — 수집 데이터 / AI 산출물 / 3축 카테고리 설계
- **AI 레이어 현황:** `schema.sql`에 AI 컬럼(`llm_prefilter`, `ai_score`, `summary_ko`, `topics`) + `country_briefings` 예약돼 있으나 이 레포는 **미구현**(모듈은 prototype에). UI 'KB 시사점'용 `kb_implication` 컬럼은 신규 필요
- **카테고리 3축:** 지역(`sources.yaml`) / 관련성게이트(`keyword_filter.py`) / 주제(AI `topics`, 미구현). UI 필터 = 주제축 → `taxonomy.yaml`로 표준화 예정

## 다음 과제 (우선순위)
1. `taxonomy.yaml` 신설 — 주제코드 5종(MARKET/BANKING/DIGITAL/ESG/RISK) ↔ UI 필터 1:1
2. `articles_raw.kb_implication` 컬럼 추가 (KB 시사점)
3. `llm_prefilter.py` / `llm_ranker.py` 이식 + 프로바이더 추상화
4. 수집원 보강 — OFFICIAL/tier0 활성화(규제), 자회사 IR(ID·KH), 거시지표 피드
5. 새 UI 데이터 연동(현지언론 화면부터), 정기 수집 자동화

> 상세 계획은 위 설계 문서 및 `docs/work_log.md` 참조
