# 현황 / 확정안 대비 정합 — glb-one-teams

> 최초 작성 2026-05-29(prototype) | glb-one-teams 재작성 2026-07-14 | 전면 갱신 2026-08-14 | 부분 갱신 2026-08-28(거점 13개·인사동향·지표확장·모달 긴요약 반영) | 전면 갱신 2026-09-04(mockups 기준 4화면 UI 리디자인 완료·rank_score 도입·GLOBAL MARKETS 티커) | **부분 갱신 2026-09-08**(인트로 화면 신설·하단내비 전용 아이콘·KFI/인사동향 모니터링 이전·모달 긴요약 확대·RANK_LIMIT 상향·근접중복 버그 2건 수정) | For Internal Use Only
>
> 본 문서는 **확정안 ↔ go-forward 레포(`glb-one-teams`) 현황 브리지**입니다. 화면 설계는 [`mockups/HANDOFF.md`](mockups/HANDOFF.md)(★현재 기준 — 구 `화면분석_개발가이드.md`/`데이터_AI_카테고리_설계.md`는 이 문서로 대체됨), 작업 이력은 [`docs/work_log.md`](docs/work_log.md), 랭킹 로직은 [`docs/rank_score_spec.md`](docs/rank_score_spec.md)를 참조하세요.

---

## 1. 두 레포 관계

| 레포 | 역할 | 상태 |
|---|---|---|
| **glb-one-teams** (이 레포) | 수집 코어 + AI 레이어 + 정적 4탭 UI | **go-forward 베이스, 매일 운영 중** |
| glb-news-rss/prototype | 풀 파이프라인 + KB 대시보드(Streamlit, 로컬) | 참조·아카이브 |

GitHub: `https://github.com/lSusial/glb-one-teams.git`
Cloudflare Pages (배포): `https://kb-global-daily.pages.dev`

---

## 2. 파이프라인 현황 (완전 가동 중 — 맥북)

```
fetch → keyword_filter → dedup → prefilter(LLM) → fulltext → rank(LLM) → translate(LLM) → briefing(LLM) → export → Cloudflare Pages
```

| 단계 | 모듈 | 상태 | 비고 |
|---|---|---|---|
| 수집 (fetch) | `collector.py` | ✅ 운영 중 | 106 피드, Google News 우회 |
| 키워드 필터 + 중복 | `keyword_filter.py` | ✅ 운영 중 | 점수제(≥3), dedup |
| LLM 1차 관문 | `llm_prefilter.py` | ✅ 운영 중 (개선 완료) | F1 0.542→0.708, 프롬프트 7카테고리 명시 |
| 본문 추출 | `fulltext.py` | ✅ 운영 중 | trafilatura, Google뉴스 URL 해소 |
| AI 분석 | `llm_ranker.py` | ✅ 운영 중 | ai_score·summary·topics·kb_implication |
| 번역 | `llm_translate.py` | ✅ 운영 중 | ACTIVE분 한국어, EN canonical 폴백 |
| 국가 브리핑 | `briefing.py` | ✅ 운영 중 | daily, 13~15개국 생성(태국·라오스 포함) |
| 모달 긴 요약 | `llm_expand.py` | ✅ 운영 중 (2026-09-08 분량 1.5~2배 확대: 10~20줄→15~35줄) | 노출(ACTIVE) 기사만, 다출처 종합, 증분. 소스가 얇으면(페이월 스니펫뿐) 별도 경량 프롬프트(`_SYS_THIN`)로 분기 — 거부·과장 늘리기 방지 |
| 인사동향 태깅 | `keyword_filter.py` | ✅ 운영 중 (신규 2026-08-28) | 역할어×교체신호어 AND매치, LLM 없음. export 시 근접중복 클러스터링 추가(2026-09-08 — 같은 인사이벤트가 국가 오분류로 며칠에 걸쳐 재탕되던 버그 수정) |
| 거시지표 | `indicators.py` | ✅ 운영 중 (확장 2026-08-28) | 환율·지수(+스파크라인)·정책금리·미국 10년물 국채 |
| 프로바이더 | `llm_provider.py` | ✅ | Batches API(50%↓), `--sync` 동기 옵션 |
| 복합 랭킹 | `ranking.py` | ✅ 운영 중 (신규 2026-09-03) | ai_score 양자화 보완용 rank_score(다매체 커버리지+tier+최신성 등) — 표시 정렬 전용, ACTIVE 게이트·mood는 ai_score 그대로. 상세 `docs/rank_score_spec.md` |
| export | `export_json.py` | ✅ 운영 중 | countries/pulse/weekly/topics + 아카이브. rank_score 정렬 반영 |
| UI (4탭+인트로, mockups 리디자인) | `web/*.html` | ✅ 실데이터 연동 (신디자인 2026-09-01~04, 인트로·내비 아이콘 2026-09-08) | `mockups/{pulse,country_detail,non_presence,topics}.html`+`HANDOFF.md` 기준 4화면 전면 리스킨, 공용 하단내비(전용 SVG 아이콘 4종+활성 골드 상단바), 첫 진입 인트로 연출(`web/intro.html` — KB 진출 11개 거점 지도 애니메이션 → brief 자동 이동), `shared-tokens.css`/`shared-sprite.js`. Cloudflare Pages 배포 완료 |

> **비용**: 모델=Haiku, Message Batches(50%↓), `--days 2` 물량 제한. ACTIVE 임계=55.

---

## 3. 확정안 요구 → 구현 상태

| 확정안 요구 | 상태 | 비고 |
|---|---|---|
| 자동 수집·필터·중복제거 | ✅ 운영 | 수동 1일 1회(자동화 미구현) |
| AI 중요도·요약·주제분류 | ✅ 실행 | ai_score, topics, summary_ko |
| KB 시사점 | ✅ 생성 | kb_implication(_ko/_en) |
| 대시보드/UI | ✅ Pages 배포 완료 | https://kb-global-daily.pages.dev |
| 한·영 토글 | ✅ | KR/EN 전환, 폴백 지원 |
| 국가 일일 브리핑 | ✅ | 11~12개국 daily |
| 본문 추출(fulltext) | ✅ 운영 중 | 약 50% 성공률(Reuters/Bloomberg 페이월) |
| TopicWatch(이슈 트래커) | ✅ 재설계 완료 | 카테고리 탭(경제/금융/디지털/리스크/지정학/ESG) |
| 평가 프레임워크 | ✅ | eval_set_v2(149건), prefilter/ranker eval |
| 메신저 배포 | 🔴 미구현 | Telegram/Zalo |
| 정기 자동화 | 🔴 미구현 | cron/Oracle Cloud |
| 거시지표(환율·지수·정책금리·국채) | ✅ 구현 완료(2026-08-28) | `indicators.py` — 정책금리는 소형 config 표(무료 API 없음), 국채는 미국만(무료 커버리지 한계) |
| 자회사 IR·OFFICIAL | ⛔ 보류 | 비-뉴스 소스 필요 |

---

## 4. 화면 구성 (4탭 정적 SPA — mockups/HANDOFF.md 기준 신디자인)

무채색+골드 강조 디자인 시스템(`web/shared-tokens.css`), 커스텀 SVG 국기 스프라이트(`web/shared-sprite.js`), 공용 기사 모달(`web/shared-modal.js`), 고정 하단 내비(홈·뉴스·모니터링·주간 브리핑) — main.py export가 `data/export/`로 복사.

| 탭 | 파일 | 데이터 창 | 내용 |
|---|---|---|---|
| ① 홈 | `brief.html` | 전일+당일 | GLOBAL PULSE 도트지도(거점 신호 말풍선, 최대 4개·심각도순, 국가코드+등급배지만) + **GLOBAL MARKETS 티커**(진출 13개국 fx/index/policy_rate 가로 마퀴, 2026-09-03) + TODAY'S TOP ISSUES(daily_highlights, top_news 10건). TOP ISSUES 모달은 top_news에 매칭이 없으면 countries.json 전체에서 제목 유사도로 보충(2026-09-08 수정) |
| ② 뉴스 | `countries.html` | 전일+당일 | 진출 13·미진출 13 토글(국기 선택기) + 거시지표 카드(rank_score순 기사) + 일일 브리핑 + 기사 피드. **한국계 금융기관·인사동향은 2026-09-08 모니터링 탭으로 이전** |
| ③ 모니터링 | `topics.html` | 주간(7일) | 이벤트 유형 탭(규제·거래투자·사건사고) + **한국계 금융기관·인사동향**(2026-09-08 편입, countries.json을 클라이언트에서 추가 fetch), 진출+미진출 병합·rank_score순. 인사동향은 export 시 근접중복 제거 |
| ④ 주간 브리핑 | `weekly.html` | 주간 | 국기 선택기 + 국가별 주간 요약·이슈·전망·키워드(목업 없이 디자인 시스템 톤으로 신규 제작, 2026-09-01) |

**인트로**: `intro.html` — 첫 진입(`/`) 시 KB 진출 11개 거점(서울 본점 기준) 지도 애니메이션 후 `brief.html`로 자동 이동, SKIP 가능. `?date`/`?lang` 쿼리 보존.

**주제 카테고리 6종(축 C, UI 필터)**: 경제 · 금융 · 디지털 · ESG · 리스크 · 지정학
**이벤트 유형 3종(축 E, 모니터링 전용)**: 규제 · 거래·투자 · 사건사고

> 상세 화면 사양·구현 매핑은 `mockups/HANDOFF.md`(★기준 문서) 참조. 구 6탭 기획(`화면분석_개발가이드.md`/`데이터_AI_카테고리_설계.md`)은 이 4탭 리디자인으로 대체됨.

---

## 5. 거점·소스 현황

- **13개 거점**: GB·US·HK·CN·JP·SG·IN·VN·MM·ID·KH·TH·LA (2026-08-28 태국·라오스 편입 — 실제 KB 지점 없는 "관심시장", `kb_network.py` 참조)
- **미진출국 13개**: PH·MY·BD·PL·DE·FR·KZ·UZ·AE·BR·MX·AU·CA (태국이 진출국으로 이동하며 14→13)
- **소스**: 태국·라오스 추가로 소스 수 증가 (정확한 총계는 `sources.yaml` 참조)
- **알려진 수집 실패**: Reuters/Bloomberg/WSJ(페이월 401/403), Google News(Oracle Cloud에서 503 → 맥북 수집)
- RTHK는 2026-08-28 소스 정리 때 제거(XML 파싱 계속 실패, 피드 자체 문제 — 홍콩은 SCMP·HK Free Press로 커버)

---

## 6. 평가 프레임워크 (eval/)

| 파일 | 내용 |
|---|---|
| `eval/eval_set_v2.jsonl` | 149건, grade 0~3, 11개 거점 균형 |
| `eval/run_eval.py` | `--mode prefilter` / `--mode ranker` 평가 |
| `eval/build_eval_v2.py` | eval_set v2 생성 스크립트 |
| `eval/results_prefilter_20260807.json` | prefilter 최종: P=0.739, R=0.680, F1=0.708 |

---

## 7. 운영 방법

```bash
# 일일 수집 + AI + 배포 (맥북)
python main.py run              # fetch → filter → dedup
python main.py ai --days 2      # prefilter → fulltext → rank → translate → brief (배치, ~10분)
python main.py export           # JSON 생성
wrangler pages deploy data/export --project-name kb-global-daily --commit-dirty=true  # CF Pages

# Oracle Cloud 동기화 (SSH 가끔 타임아웃)
rsync -avz -e "ssh -i ~/workspace/ssh-key-2026-06-25-4.key" data/export/ ubuntu@168.107.56.139:~/glb-one-teams/data/export/

# eval 실행
python main.py eval --mode prefilter
python main.py eval --mode ranker
```

---

## 8. 알려진 이슈 / 다음 과제

**이슈**:
- Oracle Cloud SSH 가끔 타임아웃 (서버 상태 확인 필요)
- ESG 카테고리 기사 수 적음 — 원인은 분류 오탐이 아니라 **수집 공백**(진단·패치안 `docs/esg_coverage_patch.md`, 미적용)
- DB integrity check 실패 이력 있음(인덱스 손상) → `REINDEX idx_articles_dedup`으로 복구
- RANK_LIMIT이 최근 30일 중 절반 가까운 날 물량을 초과해 초과분이 영구 미처리로 빠지던 버그 발견 → 400→700 상향(2026-09-08, PREFILTER_LIMIT과 동일 병목 패턴). 재발 여지 있어 물량 계속 모니터링 필요

**다음 과제 (우선순위)**:
1. **정기 자동화** — Oracle Cloud cron (수집→AI→export→CF Pages)
2. **ESG 소스 패치 적용** — `docs/esg_coverage_patch.md`의 sources.yaml 추가안 검토 후 반영
3. **Telegram 채널** — 영어판(현지 간부용)
4. **소스 보강** — OFFICIAL 피드 활성화, 태국·라오스 큐레이션 매체 추가 확보
5. **Oracle Cloud 서버 점검** — SSH 타임아웃 원인 파악
6. **라오스 정책금리** — 신뢰 가능한 무료 시드값 미확보, 재검토 필요
7. **rank_score 재튜닝** — 현재 표본 30건 기반(`docs/rank_score_spec.md` §8) — 라벨 늘려 재실행 여지
8. **근접중복 클러스터링 일반화 검토** — 인사동향·미진출국에 이미 적용된 국가 오분류 대응 로직을 다른 모아보기 섹션(korean_fi 등)에도 필요한지 점검

---

*최종 업데이트: 2026-09-08 (부분 갱신 — 인트로 화면·내비 아이콘·탭 재배치·모달 요약 확대·RANK_LIMIT 버그 수정 반영. 세션 상세는 `docs/work_log.md`)*
