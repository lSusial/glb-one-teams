# 현황 — glb-one-teams

> **최종 갱신 2026-09-28** (9/21 전면 갱신 이후 항목: 카테고리 주/보조 분리·모달 음영·KB 시사점 제거·홈 탑이슈 출처검증·국가피드 전일+당일 제한·국가탭 primary_country 라우팅·AI 중복판정 쌍단위 재설계·금액검증(번역+브리핑) — 전부 Cloudflare 배포 완료, §6·§7 갱신) | For Internal Use Only
>
> 기획은 [`PLAN.md`](PLAN.md), 설계는 [`docs/design.md`](docs/design.md), 운영은 [`docs/operations.md`](docs/operations.md), 이력은 [`docs/work_log.md`](docs/work_log.md), UI 사양은 [`mockups/HANDOFF.md`](mockups/HANDOFF.md).

## 1. 한 줄 요약

**수집 → 필터 → AI 분석 → export → 정적 UI(하단내비 5탭 + 인트로)** 파이프라인이 맥북에서 매일 수동 1회 가동 중이다. 배포 `https://kb-global-daily.pages.dev`(Cloudflare Pages), GitHub `lSusial/glb-one-teams`. 다음 마일스톤은 **회장님 시연(피드백 #20)** 이며 실서비스 배포는 시연 이후.

## 2. 파이프라인

```
fetch → keyword_filter(통과≥2, SOCIETY 독립경로) → dedup → prefilter(LLM) → fulltext → rank(LLM)
      → ai-dedup(LLM) → expand → translate → brief → highlights → export → Cloudflare Pages
```

| 단계 | 모듈 | 상태 · 비고 |
|---|---|---|
| 수집 | `collector.py` | ✅ 108소스/154피드(GNews 우회 다수). 거점국 주제확장·제재 모니터 피드 포함 |
| 키워드 필터·중복 | `keyword_filter.py` | ✅ 인사동향(역할어×교체신호어 AND)·한국계 금융기관·주제국가 폴백 태깅 포함 |
| LLM 1차 관문 | `llm_prefilter.py` | ✅ F1 0.542→0.708 |
| 본문 추출 | `fulltext.py` | ✅ 노출 기사 기준 약 48% 성공(Reuters/Bloomberg/WSJ 페이월). 실패 사유 미기록(§6) |
| AI 분석 | `llm_ranker.py` | ✅ 요약(en)·주제 6종·이벤트유형 4종·`primary_country`. 9/28부터 중요도를 4차원(직접성·규모·긴급성·신규성 0~4)으로 받고 코드가 ai_score를 합산·근거 JSON 저장(다음 rank부터 적용) |
| 근접중복 | `llm_dedup.py` | ✅ 기사쌍 단위 소청크(recall 0.87/precision 0.94), 전이 병합 차단. 9/28 소급 보정도 제목 전용으로 변경하고 `--days`/`--rep-id` 범위 지원; 오늘 오병합 해제·숨은 고점 대표 4→0 적용 |
| 긴 요약·번역 | `llm_expand.py` `llm_translate.py` | ✅ 모달 3~4문단(얇은 소스는 경량 분기), 표시분 한국어 |
| 브리핑 | `briefing.py` | ✅ 국가 일일/주간 + Top10. 9/21 적격 기준(주제국가·55점·요약 보유) 강화, 적격 0건은 안내 저장 |
| 랭킹 | `ranking.py` | ✅ 표시 정렬 rank_score(`docs/rank_score_spec.md`), 게이트는 ai_score≥55 |
| 지표 | `indicators.py` | ✅ 환율·지수·정책금리·미국채, 1/3/6개월 추세 |
| 배포 | `deploy_web.sh` | ✅ export + wrangler. 서버 rsync는 SSH 타임아웃으로 비활성 |
| 메신저 | `broadcaster.py` | 🟡 Telegram 구현, 정기 발송 미가동·채널 확정 대기 |
| 자동화 | — | 🔴 정기 실행 미구현(수동 1일 1회) |

비용: 모델 Haiku, Message Batches(50%↓), `--days 2`, rank 본문 입력 1,200자(9/17 축소). 물량 한도 `PREFILTER_LIMIT=1600`·`RANK_LIMIT=700`·`FULLTEXT_LIMIT=400`.

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

- Oracle Cloud SSH 22번 간헐적 타임아웃 → 서버 동기화 비활성. 원인 미파악.
- DB integrity check 실패 이력(인덱스 손상) → `REINDEX idx_articles_dedup`로 복구했음. 재발 시 동일 조치.
- **품질 감사(9/18) 잔여 P2**: 다매체 가중치가 동일 매체 반복 보도에 과대 반영(발행사 기준 집계 필요) · 본문 추출 실패/미시도 구분 불가 및 나중에 확보한 본문이 재분석에 반영되지 않음 · F1 평가가 운영 랭커 프롬프트를 평가하지 않음. 상세 `docs/quality_audit_2026-09-18.md`. (P1 3건은 9/21 코드 반영 후 재생성·배포까지 완료)
- ~~AI 중복판정 오묶음~~ **(9/28 재설계로 해소)**: 국가 전체를 한 번에 LLM에 넣던 방식이 응답 무효 시 국가 전체를 실패보존시켜 recall이 무너지는 구조였다(라이브 평가 recall 0.28). 기사쌍 단위 소청크 요청으로 재설계(`llm_dedup.py`) — 실 DB 적용 결과 실패보존 0건, 라벨 회귀평가 recall 0.87/precision 0.94. `eval/eval_dedup_guard.py --labeled-live`로 저비용 회귀평가 가능.
- **ai_score 양자화 개선(9/28 코드 반영)**: 기존 최종 숫자 직접 생성은 오늘 ACTIVE 20건이 62/72 두 값에 집중됐다. `directness·magnitude·urgency·novelty` 각 0~4를 받아 Python 가중합(8~96)으로 산출하고 `ai_score_factors`에 저장하도록 변경. 기존 응답은 `ai_score` 폴백. 다음 정규 rank부터 실데이터 분포를 재측정한다.
- **SDK 호환**: `anthropic` 1.x는 파이프라인의 `temperature` 인자를 받지 않아 호출이 실패한다(2026-09-21 확인). `requirements.txt`를 `anthropic>=0.40.0,<1`로 고정. 새 가상환경 구성 시 주의.
- **TH·LA 국가 화면 빈 상태**: 채점분 최고점 45/35로 55점 게이트 미달. 관심시장용 적격 기준을 별도로 검토(표본 사람 검토 후 게이트·쿼리 조정, 점수 하향/억지 채우기는 지양).
- 라오스 정책금리 시드값 미확보 · 국채는 미국만 수집.
- **국가 피드 노출 기간(9/22 확정)**: 얇은 거점을 최대 7일까지 소급 보충하던 COVERAGE_FLOOR·SOCIETY 14일 예외를 제거하고 전일+당일로 통일. 수집이 매일 안 돌면(예: 9/28처럼 6일 공백) 그만큼 국가탭이 정직하게 비는 게 정상 — 표시 감사 THIN_TABS는 이 트레이드오프를 반영한 지표.
- **국가 탭 라우팅(9/22 수정)**: 매체국적 대신 AI 주제국가(`primary_country`, 없으면 매체국적)로 통일 — 미국 매체가 쓴 한국 기사가 US 탭에 뜨는 등 오분류 해소(COUNTRY_MISMATCH 0건).
- **금액(USD) 표기 검증**: 기사 번역(`llm_translate.py`)·홈 탑이슈(`briefing.generate_daily_highlights`)·국가 브리핑(`briefing.run_briefing`) 3곳 모두 `numeric_guard.py`로 원문 대비 금액 불일치 시 저장 안 함. 9/28 실전에서 "$30억"(정답 $300억) 등 달러기호+한국어 단위 혼합 표기 누락을 발견해 패턴 추가.
- Google News 링크 해소(collector.py의 구식 redirect-follow)가 9/28 한때 0/4,462건 실패(Google 쪽 API 변경 추정) — `fulltext.py`의 `googlenewsdecoder` 경로는 정상 동작(140/257, 역사적 기준치 수준)해 실질 영향은 적음. 재발 시 `googlenewsdecoder` 최신 버전(0.2.1)도 동일 실패 확인됨 — Google 쪽 문제로 추정, 재현 시 재확인 필요.
- 커밋 전 `git status`의 `.qbak` 임시 파일·`.claude/`·`resources/` 등 미추적 항목 정리 필요.

## 7. 다음 과제 (우선순위)

1. **정기 자동화** — 단기 맥북 launchd, 중기 native RSS 전환 + GitHub Actions (`docs/operations.md` §4). 9/22→9/28처럼 실행을 건너뛰면 그 기간 데이터가 영구 공백이 되므로 우선순위 높음.
2. **품질 감사 잔여 P2 + TH·LA 적격 기준** — 발행사 집계, 본문 추출 상태 추적, 랭커 평가 연결
3. **시연 준비** — #14 문구 반영, #12 결정
4. **Telegram 정기 발송** — 채널·독자 결정 후 파이프라인 연결
5. **4차원 ai_score·rank_score 재튜닝** — 다음 정규 실행에서 factor 분포와 국가별 55점 통과율 측정 후, 라벨 확대해 가중치 재조정
6. 소스 보강 — 태국·라오스 매체, 국내 언론 글로벌 기사(#4 잔여). OFFICIAL 당국 피드는 보류 원칙 유지
