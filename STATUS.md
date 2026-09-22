# 현황 — glb-one-teams

> **최종 갱신 2026-09-21** (전면 갱신: 9/8 이후 6카테고리·제재·지표 탭·AI 중복판정·품질 감사 반영, 구 `feedback_status.md` 통합) | For Internal Use Only
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
| AI 분석 | `llm_ranker.py` | ✅ ai_score·요약(en)·주제 6종·이벤트유형 4종·`primary_country`·KB 시사점 |
| 근접중복 | `llm_dedup.py` | 🟡 9/16 신설, 9/21 범위 보존·40건 상한 제거 + **대표 재선정·겹침 가드·`dedup-repair`(소급)**. 코드 반영 완료, **실 DB 소급 적용(`main.py dedup-repair --apply`)·새 프롬프트 실측(`eval_dedup_guard.py --live`)은 맥북 실행 대기** |
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
- **품질 감사(9/18) 잔여 P2**: 다매체 가중치가 동일 매체 반복 보도에 과대 반영(발행사 기준 집계 필요) · 본문 추출 실패/미시도 구분 불가 및 나중에 확보한 본문이 재분석에 반영되지 않음 · F1 평가가 운영 랭커 프롬프트를 평가하지 않음. 상세 `docs/quality_audit_2026-09-18.md`. (P1 3건은 9/21 코드 반영, 재생성·배포는 미실행)
- **AI 중복판정 오묶음(9/21 발견)**: 저장분 표본 기준 오묶음 약 44%(라벨 70쌍, `eval/dedup_pairs_labeled.json`), 대표가 '결정 임박' 프리뷰로 남아 결정 기사가 숨는 결함. 코드 수정 완료·저장분 소급은 맥북에서 `python3 main.py dedup-repair --apply`. 소급 시뮬레이션(스크래치 복사본): 표시 감사 관련링크 무관 11→2, 프리뷰 노출 1→0, 더 새 기사 숨음 16→1.
- **표시 감사(9/21 신설 `eval/display_audit.py`) 상시 지표**: 요약 없는 카드(사회 예외경로 45~52점, IN 2·KH 1) · 3일 넘은 기사 노출(11/29) · 국가 탭에 그 나라 이야기가 아닌 기사(GB·US·SG 탭의 BOJ 등 3~5건, 진출국 탭이 매체국적 기준이라 발생 — **정책 결정 대기**) · 일본 탭 BOJ 동일 사건 4~5건.
- **SDK 호환**: `anthropic` 1.x는 파이프라인의 `temperature` 인자를 받지 않아 호출이 실패한다(2026-09-21 확인). `requirements.txt`를 `anthropic>=0.40.0,<1`로 고정. 새 가상환경 구성 시 주의.
- **TH·LA 국가 화면 빈 상태**: 채점분 최고점 45/35로 55점 게이트 미달. 관심시장용 적격 기준을 별도로 검토(표본 사람 검토 후 게이트·쿼리 조정, 점수 하향/억지 채우기는 지양).
- 라오스 정책금리 시드값 미확보 · 국채는 미국만 수집.
- 9/21 변경분(품질 감사 후속)은 로컬 코드에만 반영, 다음 실행부터 적용. 커밋 전 `git status`의 `.qbak` 임시 파일·`.claude/`·`resources/`·`web/intro_new.mp4` 등 미추적 항목 정리 필요.
- **관련 기사 링크 제목 불일치(9/22 발견·수정)**: 자기참조(같은 기사, url 동일)가 카드 헤드라인이 아닌 원문 스크래핑 제목으로 표시되어 다른 기사처럼 보임(인도 탭 '러시아 제재법' 등). `export_json.py` 수정 완료·`display_audit.py`에 SELF_TITLE_MISMATCH 점검 추가. **로컬 export 완료·Cloudflare 배포 미실행**.
- **홈 탑이슈 출처 직접 연결(9/22)**: `daily_highlights`가 국가 코드만 저장해 프론트가 같은 국가 상위 기사를 관련 링크로 추측하던 결함 수정. 생성 시 검증된 `source_article_ids`를 저장하고 export가 실제 기사 카드를 직접 포함한다. 무효 ID·출처 없음·근거와 다른 USD 금액은 저장 전 제외하며, 배포는 `display_audit.py --strict`의 출처 무결성 검사를 통과해야 한다. 현재 데이터 재생성·export 완료, 탑이슈 9건 모두 출처 검사 통과(베트남 190억 달러 오생성 1건은 금액 검증으로 제외). **Cloudflare 배포는 미실행**.

## 7. 다음 과제 (우선순위)

0. **(맥북에서 즉시)** `python3 main.py dedup-repair` → `--apply` → `python3 eval/eval_dedup_guard.py --live --days 8` → `./deploy_web.sh`(export 후 표시 감사 자동 출력). 상세 `docs/operations.md` §1. (9/22 관련링크 제목 불일치 수정도 이 export/배포에 함께 반영됨)
1. **정기 자동화** — 단기 맥북 launchd, 중기 native RSS 전환 + GitHub Actions (`docs/operations.md` §4)
2. **품질 감사 잔여 P2 + TH·LA 적격 기준** — 발행사 집계, 본문 추출 상태 추적, 랭커 평가 연결
3. **시연 준비** — #14 문구 반영, #12 결정
4. **Telegram 정기 발송** — 채널·독자 결정 후 파이프라인 연결
5. **rank_score 재튜닝** — 라벨 30건 기반(`docs/rank_score_spec.md` §8), 라벨 확대 후 재실행
6. 소스 보강 — 태국·라오스 매체, 국내 언론 글로벌 기사(#4 잔여). OFFICIAL 당국 피드는 보류 원칙 유지
