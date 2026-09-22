# glb-one-teams 프로젝트 컨텍스트

> Claude·Codex 등 코딩 에이전트 공통 지침. `AGENTS.md`는 이 파일을 가리키는 포인터다.
> **현재 상태·다음 작업은 [`STATUS.md`](STATUS.md)** 가 기준이다.

## 프로젝트 목적
KB 글로벌 거점(13개국) 뉴스 데일리. 파이프라인: 수집 → 필터 → AI 분석 → export → **정적 UI(하단내비 5탭 + 인트로)**.
신규 작업은 전부 이 레포(glb-one-teams)에서 한다.
배포: Cloudflare Pages `https://kb-global-daily.pages.dev` (GitHub: `https://github.com/lSusial/glb-one-teams.git`)

## 코드 작성 원칙
1. **Think Before Coding** — 가정을 명시하고, 불확실하면 묻는다. 해석이 여럿이면 조용히 고르지 말고 제시한다. 더 단순한 방법이 있으면 말한다.
2. **Simplicity First** — 요청받은 것만, 최소 코드로. 일회성 코드에 추상화 금지, 요청 안 된 "유연성"·불가능한 시나리오의 에러 처리 금지. 200줄이 50줄로 되면 다시 쓴다.
3. **Surgical Changes** — 필요한 곳만 건드린다. 인접 코드·주석·포맷 "개선" 금지, 기존 스타일 유지. 무관한 dead code는 지우지 말고 언급만. 내 변경으로 생긴 미사용 import/변수만 정리. 바뀐 모든 줄이 요청에 직접 추적돼야 한다.
4. **Goal-Driven Execution** — 성공 기준을 정하고 검증될 때까지 반복. "버그 수정" = 재현 테스트 작성 후 통과, "리팩터" = 전후 테스트 통과. 다단계 작업은 `[단계] → verify: [확인]` 형태로 짧게 계획을 적는다.

## 문서 지도
| 문서 | 내용 |
|---|---|
| `STATUS.md` | ★현재 구현 현황·피드백 이행·알려진 이슈·다음 과제 |
| `README.md` | 모듈 구성·CLI 사용법 |
| `PLAN.md` | 기획 확정안·비전·로드맵(제품 레이어) |
| `docs/design.md` | 데이터·AI·카테고리(축 A~E)·화면 구성 요소·미진출국 설계 |
| `docs/operations.md` | 배포 대상·채널·자동화 결정, 브로드캐스트 설계, 일일 운영 절차 |
| `docs/rank_score_spec.md` | 표시 정렬 rank_score 스펙·가중치 튜닝 기록 |
| `docs/quality_audit_2026-09-18.md` | 품질 감사(P1~P2)·잔여 개선 과제 |
| `docs/work_log.md` | 작업 이력(요약본) |
| `mockups/HANDOFF.md` | UI 디자인 시스템·화면별 데이터 계약 |

## 서버 정보
- **Oracle Cloud:** `ubuntu@168.107.56.139` (포트 22), 서버 경로 `/home/ubuntu/glb-one-teams/`
- **SSH 키:** `~/workspace/ssh-key-2026-06-25-4.key` → `ssh -i ~/workspace/ssh-key-2026-06-25-4.key ubuntu@168.107.56.139`
- **주의:** Oracle Cloud IP는 Google News RSS 503 차단 → **맥북에서 수집** 후 rsync 동기화. SSH 22번 포트 간헐적 타임아웃(2026-08-24부터 `deploy_web.sh`의 서버 동기화는 비활성).

## 수집 운영 방법 (개발 단계)
```bash
./sync_to_server.sh --collect   # 수집 후 서버 동기화 (맥북)
./sync_to_server.sh             # DB만 서버로 전송
python main.py run              # fetch → filter → dedup
python main.py init             # DB 초기화 (sources.yaml 동기화)
python main.py list             # 최근 기사 확인
```
일일 전체 절차(수집→AI→export→배포)는 `docs/operations.md` 참조.

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

미진출 13개국(통합 피드)은 `docs/design.md` §5 참조. (뉴질랜드 오클랜드 지점은 2026-09 폐쇄 — 관리국 아님)

## 국가·소스 변경 규칙
- 매체 categories는 해당 국가 코드 **하나만**(중복 금지). 글로벌 매체는 `GLOBAL`, 국가 전용 매체는 해당 국가 코드.
- 국가 추가/변경 시 함께 손볼 곳: `sources.yaml`, `kb_network.py`, `config.py`(`INDICATOR_MAP`·`POLICY_RATES`·`NON_PRESENCE_COUNTRIES`), `export_json.py`(`_FLAGS_ALL`·`_PRESENCE_NAMES`), `web/*.html`의 NAMES 딕셔너리, `web/shared-sprite.js`(국기).
- 소스·피드 수는 하드코딩하지 않는다 — `sources.yaml` 참조(수집 로그에 `feeds=N` 출력).
- `main.py run`이 sources.yaml을 자동 sync하므로, **검증 안 된 피드는 넣기 전에 맥북에서 `fetch`로 수율부터 확인**한다.

## 핵심 설계 규칙
- **AI 레이어**: 모델 Haiku, **Message Batches API(50% 할인) 기본**(`--sync`로 동기 전환). 물량은 `--days`로 제한. 영어가 canonical(`summary_en`) → `llm_translate`가 한국어 채움.
- **ai_score ACTIVE 임계 = 55** — 노출 게이트·국가 온도는 ai_score 그대로. 화면 정렬만 `ranking.py`의 rank_score(다매체+tier+최신성 등, `docs/rank_score_spec.md`).
- **카테고리**: 축 A 지역(`sources.yaml`) / 축 B 관련성 게이트(`keyword_filter.py`) / 축 C 주제 6종 ECONOMY·MARKETS·TECH·GEO·POLICY·SOCIETY + 축 E 이벤트유형 REG·SANCTION·DEAL·INCIDENT(`taxonomy.yaml`, AI가 분류). 상세 `docs/design.md`.
- **국가 태그**: 전 화면 AI 주제국가(`primary_country`) 우선, 없으면 매체국적(`media_sources.primary_country_code`). 2026-09-22 이전엔 진출국 현지피드만 매체국적이었으나, 미국 매체가 쓴 한국 기사가 US 탭에 뜨는 등 오분류가 있어 통일.
- **본문 추출**: prefilter 통과분만 `fulltext.py`(trafilatura+googlenewsdecoder, 무료)로 원문 추출 후 rank. Google News 링크는 맥북에서만 해소 가능. **수집 강화 방향 = 무료 우선(유료 API 보류).**

## 진행 범위 원칙
뉴스 분석으로 도출 가능한 항목은 포함, 비-뉴스 원천이 필요한 항목은 보류.
- 포함: 현지언론+AI(요약·시사점·분류·국가 브리핑), Global Pulse 지도, 인사동향, 모니터링(이벤트 유형·제재 뉴스), 국가별 거시지표(환율·지수·정책금리·미국채, `indicators.py`), 미진출국 통합 피드.
- 보류: 재미요소·참여형(퀴즈 등), 자회사 IR 링크, OFFICIAL 당국 원천 피드, 제재 명단 스크리닝.
