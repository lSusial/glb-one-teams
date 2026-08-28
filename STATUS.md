# 현황 / 확정안 대비 정합 — glb-one-teams

> 최초 작성 2026-05-29(prototype) | glb-one-teams 재작성 2026-07-14 | 전면 갱신 2026-08-14 | **부분 갱신 2026-08-28**(거점 13개·인사동향·지표확장·모달 긴요약 반영) | For Internal Use Only
>
> 본 문서는 **확정안 ↔ go-forward 레포(`glb-one-teams`) 현황 브리지**입니다. 제품 비전·로드맵은 [`PLAN.md`](PLAN.md), 화면 설계는 [`화면분석_개발가이드.md`](화면분석_개발가이드.md), 수집·AI·카테고리 설계는 [`데이터_AI_카테고리_설계.md`](데이터_AI_카테고리_설계.md), 작업 이력은 [`docs/work_log.md`](docs/work_log.md)를 참조하세요.

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
| 모달 긴 요약 | `llm_expand.py` | ✅ 운영 중 (신규 2026-08-28) | 노출(ACTIVE) 기사만 10~20줄 다출처 종합, 증분 |
| 인사동향 태깅 | `keyword_filter.py` | ✅ 운영 중 (신규 2026-08-28) | 역할어×교체신호어 AND매치, LLM 없음 |
| 거시지표 | `indicators.py` | ✅ 운영 중 (확장 2026-08-28) | 환율·지수(+스파크라인)·정책금리·미국 10년물 국채 |
| 프로바이더 | `llm_provider.py` | ✅ | Batches API(50%↓), `--sync` 동기 옵션 |
| export | `export_json.py` | ✅ 운영 중 | countries/pulse/weekly/topics + 아카이브 |
| UI (4탭) | `web/*.html` | ✅ 실데이터 연동 | Cloudflare Pages 배포 완료 |

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

## 4. 화면 구성 (4탭 정적 SPA)

| 탭 | 파일 | 데이터 창 | 내용 |
|---|---|---|---|
| ① 글로벌 원팀 뉴스 | `brief.html` | 전일+당일 | 카테고리 온도계(5) + 오늘의 핵심뉴스(중요도순) + 오늘의 글로벌 핵심(합성 10건) |
| ② 국가별 뉴스 | `countries.html` | 전일+당일 | 진출 13·미진출 13 토글 + 거시지표 카드 + 일일 브리핑 + 기사 피드. 한국계 금융기관·인사동향 탭(세부 필터 없음, 목록+건수) |
| ③ 모니터링 | `topics.html` | 주간(7일) | **이벤트 유형 탭**(규제·거래투자·사건사고) — 진출/미진출 토글 |
| ④ 주간 리포트 | `weekly.html` | 주간 | 국가별 주간 상위 기사 — **보류(기능 미완성)** |

**주제 카테고리 6종(축 C, UI 필터)**: 경제 · 금융 · 디지털 · ESG · 리스크 · 지정학
**이벤트 유형 3종(축 E, 모니터링 전용)**: 규제 · 거래·투자 · 사건사고

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
- ESG 카테고리 기사 수 적음(3건/주) — 소스 보강 필요
- DB integrity check 실패 이력 있음(인덱스 손상) → `REINDEX idx_articles_dedup`으로 복구

**다음 과제 (우선순위)**:
1. **정기 자동화** — Oracle Cloud cron (수집→AI→export→CF Pages)
2. **Telegram 채널** — 영어판(현지 간부용)
3. **소스 보강** — ESG·OFFICIAL 피드 활성화, 태국·라오스 큐레이션 매체 추가 확보
4. **Oracle Cloud 서버 점검** — SSH 타임아웃 원인 파악
5. **라오스 정책금리** — 신뢰 가능한 무료 시드값 미확보, 재검토 필요

---

*최종 업데이트: 2026-08-14 (부분 갱신 2026-08-28 — 세션 상세는 `docs/work_log.md`)*
