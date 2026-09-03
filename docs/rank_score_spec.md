# 지시서 — ai_score 캘리브레이션(복합 rank_score) 배선

> 대상: Claude Code (glb-one-teams). 먼저 `ranking.py`(레포 루트, 이미 추가됨)와 이 문서를 읽어라.
> 배경 데이터는 §0 참조. UI(mockups/*)와 독립적인 백엔드 작업.

## 0. 왜 (실측 근거, news.db 최근 14일)
- `llm_ranker` 의 `ai_score`(0~100)는 LLM 절대채점이라 값이 양자화됨.
  **ACTIVE(≥55) 460건 중 459건이 60~64, ACTIVE 구간 고유 점수값 단 5개.**
- 즉 "ai_score 순" 정렬 = 사실상 전부 동점 → Top Issues·피드 순서가 무작위.
- 해결: `ai_score` 는 **게이트/온도 유지**, **표시용 정렬만** 결정적 복합 점수 `rank_score` 로.
  가장 강한 신호는 다매체 커버리지(`duplicate_of` 형제 수) — 지금 정렬에 안 쓰이던 것.
- 검증됨: 같은 456건에 `ranking.rank_score` 적용 시 고유값 5→21개, 범위 64~92.5 로 변별됨.

## 1. 추가된 모듈: `ranking.py` (루트)
- `cluster_sizes(conn) -> {rep_article_id: 형제수}` — export 실행당 1회 계산해 재사용.
- `rank_score(row, cluster=0, now=None) -> float` — 단일 기사 점수.
  읽는 키: `ai_score`(필수), `published_at`, `tier`, `cc`(또는 `primary_country_code`),
  `event_type`, `korean_fi`, `personnel_move`. 없으면 해당 보너스 0(안전).
- `order(conn, rows, cluster_map=...) -> rows(정렬)` — rank_score 내림차순 안정 정렬.
  **각 row 는 `article_id` 를 포함해야 다매체 신호가 반영됨.**
- 가중치는 모듈 상단 상수(W_CLUSTER 등) — 튜닝은 여기만.

## 2. 원칙 (반드시 지킬 것)
- **`ai_score` 자체는 건드리지 않는다.** ACTIVE 게이트(`WHERE a.ai_score >= AI_SCORE_ACTIVE_THRESHOLD`),
  국가 온도(mood)·stress·intensity 계산은 **그대로**. rank_score 는 **표시용 정렬 전용**.
- LLM 추가 리랭크는 이번 범위 아님(사용자 결정: 복합랭킹만).
- 수집/AI 파이프라인(collector/prefilter/ranker) 로직은 건드리지 말 것 — export/briefing 정렬만.

## 3. 적용 지점 (export_json.py) — `ORDER BY a.ai_score DESC` → rank_score 정렬
각 피드 빌더는 이미 "SQL로 뽑아 Python에서 조립"한다. 패턴:
1) 후보 SELECT에 **`a.article_id, m.tier, a.event_type, a.korean_fi, a.personnel_move`** 를 포함
   (대개 `m.primary_country_code`, `a.published_at`, `a.ai_score` 는 이미 있음).
2) 후보 LIMIT을 **넉넉히**(최종 N의 약 3배) 뽑아 재랭킹 재료 확보.
3) fetch 후 `rows = ranking.order(conn, rows, cluster_map=CM)[:N]` 로 정렬·절단.
   (`CM = ranking.cluster_sizes(conn)` 를 export 진입부에서 1회 만들어 각 빌더에 전달.)

바꿀 함수(현재 라인 기준, 상황 따라 이동 가능):
- `_compute_non_presence` (~L199~219): 후보 pool을 rank_score로 정렬 후 dedup/slice. 기존 `reps.sort(key=ai_score)` 도 교체.
- 국가 기사 피드 (~L350~374): `order = "a.ai_score DESC, a.published_at DESC"` 로 뽑은 뒤 rank_score 재정렬.
- 한국계 금융기관 피드 (~L275), 인사동향 피드 (~L320): 동일.
- `_compute_top_news` (~L568~583): 이미 60건 fetch → rank_score 정렬 후 limit.
- 펄스 카테고리 하이라이트 (~L520~526): 표시 목적이면 동일 적용(온도 계산 L478~은 ai_score 유지).

주의: export 가 각 기사에 `score=a["ai_score"]` 를 emit 한다(배지·표시용) — **유지**.
다만 배열 순서는 이제 rank_score 순이므로, **UI가 배열을 다시 score로 재정렬하지 않도록** 한다
(mockups/non_presence·topics 는 서버 정렬을 신뢰하고 순서 보존). 필요하면 각 기사에 `rank`(정수 순위) 필드도 emit.

## 4. 적용 지점 (briefing.py) — 홈 Top Issues 후보풀
- `generate_daily_highlights` (~L292~296): 후보를 `ORDER BY a.ai_score DESC` 로 뽑아 LLM(_system_highlights)에 넘긴다.
  이 **후보 SELECT를 rank_score 정렬로** 바꿔라(§3 패턴). 최종 순서는 기존 LLM 합성이 정하므로 LLM 단계는 그대로.
  → 압축된 ai_score로 뽑던 후보풀이 다매체·최신·진출국 신호가 반영된 풀로 개선됨.
- `_country_briefings`(국가별 상위, ~L169~175)도 같은 방식 적용 가능(선택).

## 5. 검증 (구현 후 필수)
1. `python main.py export` 가 에러 없이 통과.
2. 아래로 before/after 대조(고유값·상위10):
```python
import sqlite3, ranking
c=sqlite3.connect("data/news.db"); c.row_factory=sqlite3.Row
rows=c.execute("""SELECT a.article_id,a.ai_score,a.title_ko,a.title,a.published_at,
  a.event_type,a.korean_fi,a.personnel_move,m.tier,m.primary_country_code cc
  FROM articles_raw a JOIN media_sources m ON m.source_id=a.source_id
  WHERE a.ai_score>=55 AND a.published_at>=date('now','-14 day') AND a.duplicate_of IS NULL""").fetchall()
cm=ranking.cluster_sizes(c)
for r in ranking.order(c,rows,cluster_map=cm)[:10]:
    print(ranking.rank_score(r,cm.get(r["article_id"],0)), (r["title_ko"] or r["title"])[:30])
# 기대: 고유 rank_score 값이 ai_score(5개)보다 크게 늘고(≈20+), 상위가 다매체·최신·진출국 기사로 변별됨.
```
3. 생성된 `data/export/pulse.json`(daily_highlights)·`countries.json`(non_presence)·`topics.json` 의 배열 순서가 바뀌었는지 육안 확인.

## 6. 다음 단계(별도)
- 가중치(ranking.py 상수)를 eval/ 평가셋(또는 소량 인적 라벨)으로 튜닝 — 지금은 실측 기반 기본값.
- 이후 필요 시 상위 N건 LLM 리랭크(품질↑, 비용↑) 추가 검토.


## 8. 적용·튜닝 기록 (2026-09-03, 완료)
- **배선 완료**: export_json.py 7개 피드 + briefing.py 후보풀에 ranking.order 적용. ai_score 게이트·온도 불변. 같은 DB 구/신 대조 — 건수 동일, 정렬만 변경. countries.json·non_presence 에 `rank_score` 필드 추가.
- **가중치 튜닝(근거 기반)**
  - 정답 B: `daily_highlights` 이력(LLM이 고른 오늘의 핵심) 46건 → 기사 퍼지매칭 30건, 6일치. 후보=그날 ACTIVE(진출국).
    그리드(cluster×tier×recency×presence×event) 탐색: 기준선(ai_score순) **R@10 0.323 → 0.423**.
    일관 신호: tier↑(1.5~2×), 다매체 6→3, 이벤트 2, 최신성 1×. MRR은 기준선 우위(정답 생성 시 ai_score순 목록 1위 편향) — 표본 30건이라 방향 지표로만.
  - 정답 A: `eval/eval_set_v2.jsonl` 사람 등급 149건 — tier0 평균등급 2.0 > tier1 1.31 > tier2 0.97 → tier 보너스 뒷받침.
  - 채택: `W_CLUSTER 6→3`, `TIER_BONUS (6,4,2)→(9,6,3)`. 진출국(측정불가)·이벤트·최신성·한국계금융·인사는 유지.
  - 재현: `eval/rank_tune_labels.json`(정답), `eval/rank_tune_results.json`(상위50 구성). 라벨을 늘린 뒤 같은 절차로 재튜닝 권장.
