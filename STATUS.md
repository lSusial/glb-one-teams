# 현황 — glb-one-teams

> **최종 갱신 2026-09-30** — 9/28~30 품질 개선(금액 검증 USD·INR·다출처 충돌, 번역 대상 확대, 대체된 예고 기사 제거, Google News 링크 해소 재설계, 발행사 기준 집계, 본문 추출 상태 기록, 국가 라우팅 공용화) 전부 `main` 반영. **9/29 06:12 UTC 배포 이후 머지분(#10, 7a8180f, 3628cc8, 429 재시도 수정)은 다음 정기 실행 때 배포** | For Internal Use Only
>
> 기획은 [`PLAN.md`](PLAN.md), 설계는 [`docs/design.md`](docs/design.md), 운영은 [`docs/operations.md`](docs/operations.md), 이력은 [`docs/work_log.md`](docs/work_log.md), UI 사양은 [`mockups/HANDOFF.md`](mockups/HANDOFF.md).

## 1. 한 줄 요약

**수집 → 필터 → AI 분석 → export → 정적 UI(하단내비 5탭 + 인트로)** 파이프라인이 맥북에서 매일 수동 1회 가동 중이다. 배포 `https://kb-global-daily.pages.dev`(Cloudflare Pages), GitHub `lSusial/glb-one-teams`. 다음 마일스톤은 **회장님 시연(피드백 #20)** 이며 실서비스 배포는 시연 이후.

## 2. 파이프라인

```
fetch → keyword_filter(통과≥2, SOCIETY 독립경로) → dedup → prefilter(LLM) → fulltext → rank(LLM)
      → ai-dedup(LLM) → resolve-links → expand → translate → brief → highlights → export → Cloudflare Pages
```

| 단계 | 모듈 | 상태 · 비고 |
|---|---|---|
| 수집 | `collector.py` | ✅ 108소스/154피드(GNews 우회 다수). 거점국 주제확장·제재 모니터 피드 포함. 9/30 기사별 원발행사명(`publisher_name`, GN `<source>`) 저장. 9/29 성공률 0%이던 GN redirect 해소 제거(Google 429 원인) |
| 키워드 필터·중복 | `keyword_filter.py` | ✅ 인사동향(역할어×교체신호어 AND)·한국계 금융기관·주제국가 폴백 태깅 포함 |
| LLM 1차 관문 | `llm_prefilter.py` | ✅ F1 0.542→0.708 |
| 본문 추출 | `fulltext.py` | ✅ GN 링크 순차 디코딩(429 연속 3회 시 중단), 상태 `ok/unresolved_url/extract_failed/pending`·시각·사유 기록, 확정 실패만 3일 재시도 제한(429·미시도는 다음 실행 재시도). 한도 `FULLTEXT_LIMIT=400` |
| AI 분석 | `llm_ranker.py` | ✅ 요약(en)·주제 6종·이벤트유형 4종·`primary_country`. 중요도 4차원(직접성·규모·긴급성·신규성 0~4) 가중합, 근거 `ai_score_factors` 저장. 빈/무효 응답은 미채점 유지(임의 50점 제거). 한·영 제목 금액 검증, 다출처 금액 충돌 시 종합 보류(`source_conflict`) |
| 근접중복 | `llm_dedup.py` | ✅ 기사쌍 단위 소청크(recall 0.87/precision 0.94), 전이 병합 차단(소급 보정도 동일 규칙), 한국 공통어 가드, 예고 판정은 AI 제목 기준. 국가 판정 `db.effective_country_expr()` 공용 |
| 링크 해소 | `fulltext.resolve_display_links` | ✅ rank·중복판정 후 채점 기사 → 그 형제(관련 링크) 순으로 GN 링크 해소, 최대 150건(`main.py resolve-links` 단독 실행 가능). 9/29 GN 링크 카드 46→7/107 |
| 긴 요약·번역 | `llm_expand.py` `llm_translate.py` | ✅ 모달 3~4문단(얇은 소스는 경량 분기), 표시분 한국어. 금액 검증(USD·INR), 번역 대상에 55점 미만 노출 경로(사회 예외·인사동향·한국계 금융기관) 포함 |
| 브리핑 | `briefing.py` | ✅ 국가 일일/주간 + Top10. 적격 기준(주제국가·55점·요약 보유), 금액 검증, 탑이슈 국가 태그를 근거 기사 주제국가로 교정 |
| 랭킹 | `ranking.py` | ✅ 표시 정렬 rank_score(`docs/rank_score_spec.md`), 다출처 보너스는 독립 발행사 수 기준. 게이트는 ai_score≥55 |
| export | `export_json.py` | ✅ 대체된 예고 기사 제거(`superseded_ids`), 토픽·태그 페이지 링크 제외, 관련 링크 최대 4건(원문+같은 사건 3)·사건 유사도 검증, 홈 지도 무데이터 분리 |
| 지표 | `indicators.py` | ✅ 환율·지수·정책금리·미국채, 1/3/6개월 추세(주 1회 `indicators-history`) |
| 표시 감사 | `eval/display_audit.py` | ✅ 19개 항목. `deploy_web.sh --strict`는 탑이슈 출처 무결성만 배포 차단, 나머지는 보고 전용(§6 결정 대기) |
| 배포 | `deploy_web.sh` | ✅ export + 감사 + wrangler. 서버 rsync는 SSH 타임아웃으로 비활성 |
| 메신저 | `broadcaster.py` | 🟡 Telegram 구현, 정기 발송 미가동·채널 확정 대기 |
| 자동화 | — | 🔴 정기 실행 미구현(수동 1일 1회) |

비용: 모델 Haiku, Message Batches(50%↓), `--days 2`, rank 본문 입력 1,200자(9/17 축소). 물량 한도 `PREFILTER_LIMIT=1600`·`RANK_LIMIT=700`·`FULLTEXT_LIMIT=400`·`DISPLAY_LINK_RESOLVE_LIMIT=150`.

**작업 방식(9/29~)**: 코드는 클라우드 세션(Claude Code on the web)에서 수정·PR 머지, 실행·DB는 맥북. 클라우드에서 코드를 바꾸면 맥북 실행 프롬프트를 함께 준다(`CLAUDE.md` "클라우드 세션 작업 규칙").

## 3. 화면

| 탭 | 파일 | 내용 |
|---|---|---|
| 홈 | `brief.html` | GLOBAL PULSE 도트지도 · GLOBAL MARKETS 티커(JS scroll) · TODAY'S TOP ISSUES 10건(모달) |
| 뉴스 | `countries.html` | 진출/미진출 토글 · 국기 선택기(★핀) · 지표 카드 · 일일 브리핑 · 기사 피드 · 6카테고리 칩(빈 칩 자동 숨김) · 원문 링크 |
| 모니터링 | `topics.html` | 이벤트유형 탭(규제·제재·거래투자·사건사고) · 한국계 금융기관 · 인사동향 · 국가 칩 |
| 지표 | `markets.html` | 환율·지수·정책금리·국채 추세, 1/3/6개월 토글 (9/17 신설) |
| 주간 | `weekly.html` | 국가별 주간 요약·이슈·전망·키워드 |
| 부가 | `links.html` `intro.html` | 원문 링크 페이지 · 시네마틱 인트로 영상 + SKIP |

공통: 무채색+골드 디자인 시스템(`shared-tokens.css`), 커스텀 국기 스프라이트, 공용 기사 모달·용어 툴팁(`shared-glossary.js`)·과거 날짜 선택(`shared-datenav.js`), 한·영 토글(`?lang=en`, 폴백 지원). 6종 카테고리·이벤트유형 정의는 `docs/design.md` §4.

## 4. 거점·소스

- **진출 13**: GB·US·HK·CN·JP·SG·IN·VN·MM·ID·KH·TH·LA (TH·LA는 관심시장). **미진출 13**: PH·MY·BD·PL·DE·FR·KZ·UZ·AE·BR·MX·AU·CA.
- 알려진 수집 실패: Reuters/Bloomberg/WSJ(페이월 401/403), Google News(클라우드 IP 503 → 맥북 수집). RTHK는 XML 파싱 실패로 제거(홍콩은 SCMP·HK Free Press).

## 5. 중간발표(9/10) 피드백 20건 이행 현황

기준: 글로벌 원팀 뉴스 피드백 PDF. ✅ 완료 · 🟡 부분 · ❌ 미구현 · 📌 비개발/결정.

| # | 항목 | 상태 | 비고·잔여 |
|---|---|---|---|
| 1 | 경제지표 페이지 | ✅ | 지표 탭. 6개월 주봉은 `indicators-history` 주 1회 백필 |
| 2 | 뉴스 링크 페이지 | ✅ | `links.html` |
| 3 | 제재 페이지(FI유닛) | 🟡 | 제재 **뉴스**(A안)만 반영 — 이벤트유형 SANCTION + 전용 피드 3종. OFAC/UN/FATF/EU 공식 명단 연동은 비-뉴스라 **스코프 결정 대기**(예외 개설/링크 안내/보류) |
| 4 | 수집 범위 확대 | 🟡 | SOCIETY·거점국 주제확장 반영. **국내 언론의 글로벌 기사** 미반영 |
| 5 | 카테고리 재정비 | ✅ | 신 6종, ESG→POLICY 흡수 |
| 6 | 기사 분량 통일 | ✅ | 모달 3~4문단 |
| 7 | 품질(오분류·중복) | ✅ | 주제국가, 키워드+AI+스토리 dedup(국가당 상한 8). 의미만 같은 중복은 ai-dedup 재실행 필요 |
| 8 | 국가 선택 우선순위 | 🟡 | ★핀 완료. IP 기반 자동 우선노출은 정적 배포 한계로 보류 |
| 9 | 모니터링 국가 선택 | ✅ | 국가 칩(탭별 건수) — 구 현황 문서의 ❌는 오기, 코드로 확인 |
| 10 | 용어 설명 | ✅ | `shared-glossary.js` |
| 11 | 검색 | 🚫 | **하지 않기로 결정(9/21)** — 포털이 아니므로 자체 검색 미제공. 카테고리·국가·날짜 탐색으로 대체 |
| 12 | 퀴즈·참여형 | ❌ | **결정 대기**: 형식·문항, "100번째 접속자" 이벤트는 백엔드 필요(재미요소 보류 원칙과 충돌) |
| 13 | 인트로 문구·Skip | ✅ | 영상 인트로 + SKIP. 사운드 정책 미결 |
| 14 | 원팀 메시지 | ❌ | 문구·수치 타부서 확정 대기(레포 기준 진출 13개국, 원안의 "14개국 19,047명" 검증 필요) |
| 15 | 법률지원부 검토 | 📌 | 비개발 |
| 16 | 다국어 | ✅ | **한·영으로 확정(9/21)**. 현지어는 필요가 생길 때 추가 검토 |
| 17 | 업데이트 주기 | 📌 | 현재 1일 1회 — 단축은 자동화·비용 검토 선행 |
| 18 | 전달 채널 | 🟡 | Telegram 구현, 채널 확정 대기 |
| 19 | 해외법인 인증 | 📌 | KBFG 이메일 미사용 법인 접근 방안, 시연 후 |
| 20 | 회장님 시연 | 📌 | 마일스톤 — 개선 버전으로 시연 |

시연 전 착수 후보(경량): #14 → #12. 결정 대기 요약: #3 원천 범위 · #12 · #14 문구 · #17 주기 · #18 채널 · 배포 대상(`docs/operations.md`). (#11 검색 제외, #16 한·영 확정 — 9/21 결정)

## 6. 알려진 이슈

**품질 검증 체계 (9/28~30 구축)**
- **금액 검증(`numeric_guard.py`)**: USD(달러기호·USD 선행·billion/억·만 복합 표기)와 INR(Rs/₹·crore/lakh ↔ 루피, 루피아 제외)을 통화별 대조. 번역·모달 긴 요약·한/영 제목·국가 브리핑·탑이슈 저장 전 검증, 불일치 시 저장하지 않고 재생성. 다출처 금액 충돌 시 종합 보류. 실전에서 잡은 사례: 인도 crore→억 10배 오류, RBI "$18.65억", 인니 Rp 9.1조→"$9.1 trillion", 주간 CN·JP·VN 10배 축소.
- **표시 감사 19항목**: 9/29 배포본 기준 핵심 항목 0건(THIN_TABS 7은 정책상 허용). UNRELATED_LINKS 1(IN 국채↔루피 느슨한 스토리 묶음)·HIDDEN_NEWER_REP 1(실제 예고 기사) 확인, 오류 아님.
- **결정 대기**: AMOUNT_MISMATCH 등을 `--strict` 배포 차단 조건에 넣을지(현재 보고 전용, 9/29 보류).

**데이터·수집**
- **Google News 링크(9/29 재설계)**: collector redirect 요청(성공률 0%)과 병렬 디코딩이 요청 한도를 소진해 맥북 IP가 429 차단됐던 문제. 재설계 후 GN 링크 카드 46→7/107, 국가탭 0/33. **다음 정기 실행에서 확인할 것**: 429가 본문추출 단계에서 먼저 나는지(그렇다면 본문추출 쪽 디코딩 상한 추가), 순차 디코딩으로 늘어난 소요시간.
- **품질 감사(9/18) 잔여 P2**: 발행사 기준 다출처 집계·본문 추출 상태 기록·운영 랭커 F1 연결은 9/30 반영. 남은 항목은 **나중에 확보한 본문을 기존 결과 유실 없이 재분석하는 원자적 큐**. 상세 `docs/quality_audit_2026-09-18.md`, `docs/tech_debt_audit_2026-09-29.md`.
- **ai_score 4차원(9/28 반영)**: 이전엔 ACTIVE가 62/72 두 값에 집중. 실데이터 분포·국가별 55점 통과율은 아직 미측정(다음 정규 실행 쿼리로 측정).
- **중복판정 임계 `DEDUP_MIN_OVERLAP=0.25`**: 제목+요약 기준으로 보정한 값인데 9/28부터 제목 전용 → `eval/eval_dedup_guard.py` 재측정 필요.
- **얇은 거점**: HK·VN·MM 국가탭 0건, TH·LA는 채점 최고점 45/35로 55점 미달. 관심시장 적격 기준 별도 검토(점수 하향·억지 채우기는 지양). 수집 공백일(9/22~27)엔 주간도 비는 게 정상.
- **국가 피드 노출 기간(9/22 확정)**: 전일+당일. 수집을 건너뛰면 그만큼 국가탭이 정직하게 빈다.
- **국가 탭 라우팅**: AI 주제국가(`primary_country`) 우선, `db.effective_country_expr()`로 국가 화면·브리핑·중복판정·홈 집계 공용.

**환경**
- Oracle Cloud SSH 22번 간헐적 타임아웃 → 서버 동기화 비활성. 원인 미파악.
- DB integrity check 실패 이력(인덱스 손상) → `REINDEX idx_articles_dedup`로 복구. 재발 시 동일 조치.
- **SDK 호환**: `anthropic` 1.x는 `temperature` 인자 비호환 → `requirements.txt` `anthropic>=0.40.0,<1` 고정.
- 라오스 정책금리 시드값 미확보 · 국채는 미국만 수집.
- 커밋 전 `git status`의 `.qbak` 임시 파일·`.claude/`·`resources/` 등 미추적 항목 정리 필요.

## 7. 다음 과제 (우선순위)

1. **다음 정기 실행 검증** — 9/29 배포 이후 머지분 첫 실행. 링크 해소(429 발생 위치·소요시간), 4차원 점수 분포·국가별 통과율, 표시 감사 19항목, `eval_dedup_guard.py` 임계 재측정.
2. **정기 자동화·실패 알림** — 맥북 launchd로 수집→분석→감사→배포 전 산출 자동화 + Telegram 실패 알림(`docs/operations.md` §4, 기술부채 점검 1순위). 실행을 건너뛰면 그 기간이 영구 공백.
3. **본문 확보 후 안전한 재분석** — 새 본문을 얻은 기존 채점 기사를 원자적으로 재랭킹(실패 시 기존 결과 유지).
4. **4차원 ai_score·rank_score 재튜닝** — 1번 측정 후 라벨 확대해 가중치 조정.
5. **시연 준비** — #14 문구 반영, #12 결정.
6. **Telegram 정기 발송** — 채널·독자 결정 후 파이프라인 연결.
7. **소스 보강** — HK·VN·MM·TH·LA 현지 매체(맥북 `fetch`로 수율 확인 후 편입), 국내 언론 글로벌 기사(#4 잔여). OFFICIAL 당국 피드는 보류 원칙 유지.
8. **기술부채** — DB 연결 수명 관리, 국가 메타데이터 단일화, `export_json.py` 분리(`docs/tech_debt_audit_2026-09-29.md`).

결정 대기: `--strict` 배포 차단 범위 확대 · 관심시장(TH·LA) 적격 기준 · §5 결정 항목.
