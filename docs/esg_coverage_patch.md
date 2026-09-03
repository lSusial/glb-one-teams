# ESG 커버리지 보강 — 적용 준비된 패치 (미검증, 맥북에서 적용·검증 필요)

> 2026-09-03. **아직 적용하지 않았음.** 이유는 §3.
> 적용 대상: `sources.yaml` (추가만, 기존 항목 무수정)

## 1. 진단 결론 (실측 근거)

| 확인 | 결과 |
|---|---|
| ESG 태깅 정확도 | **100%** — 금융 ESG 용어(green bond·sustainable finance·transition finance·green credit·climate risk) 포함 채점기사 7건 전부 ESG 태깅 |
| DIGITAL 태깅 정확도 | **100%** (44/44) |
| RISK 태깅 | 53% (66/125) — 미태깅분은 대부분 이란·트럼프 **지정학 제재**로 GEO 태깅이 오히려 타당. 버그 아님 |
| ESG 절대량 | 60일간 채점 5,832건 중 ESG **178건(3%)**, 그중 *금융* ESG 용어 기사는 **7건** |
| prefilter가 버린 ESG | 사유가 전부 타당 — "홍보성 보도", "KB금융 경영 영향 미미", "소형 기업 파트너십", "일반 기후정책 입장 표명" |

**결론: AI 분류·필터는 정상. ESG 부족은 순수 "수집 커버리지" 문제.**
현재 수집되는 ESG 기사는 일반 기후·CSR 뉴스라 프리필터가 (정당하게) 걸러내고,
금융적으로 유의미한 ESG(녹색채권 발행·지속가능금융 규제·전환금융)는 **소스에 거의 안 잡힘**.

## 2. 제안 패치 — `sources.yaml` 에 ESG-금융 타깃 피드 추가

기존에 **동작이 검증된 도메인 + 레포의 기존 GNews 쿼리 패턴**만 사용(신규 매체 도입 아님).
`sources:` 리스트 끝에 아래 블록 추가:

```yaml
  # ============================================================
  # ESG-금융 타깃 (2026-09-03 추가) — 일반 기후/CSR 이 아니라
  # "금융적으로 유의미한 ESG"(녹색채권·지속가능금융·전환금융·ESG 규제)만 겨냥.
  # 근거: 60일간 금융 ESG 용어 기사 7건뿐 = 수집 커버리지 공백(분류는 100% 정상).
  # 되돌리기: 이 블록 삭제 후 `python main.py init`.
  # ============================================================
  - media_name: ESG Finance (Global)
    country: GLOBAL
    language: en
    tier: 1
    categories: [GLOBAL_ECONOMY]
    feeds:
      - section: esg_finance
        url: https://news.google.com/rss/search?q=(%22green+bond%22+OR+%22sustainable+finance%22+OR+%22transition+finance%22+OR+%22sustainability-linked+loan%22)+when:2d&hl=en-US&gl=US&ceid=US:en

  - media_name: ESG Finance (Asia)
    country: GLOBAL
    language: en
    tier: 1
    categories: [GLOBAL_ECONOMY]
    feeds:
      - section: esg_asia
        url: https://news.google.com/rss/search?q=(Indonesia+OR+Vietnam+OR+India+OR+Singapore+OR+%22Hong+Kong%22)+(%22green+bond%22+OR+%22green+financing%22+OR+%22sustainable+finance%22+OR+%22green+credit%22)+when:3d&hl=en-US&gl=US&ceid=US:en

  - media_name: ESG Regulation
    country: GLOBAL
    language: en
    tier: 1
    categories: [GLOBAL_ECONOMY]
    feeds:
      - section: esg_reg
        url: https://news.google.com/rss/search?q=(%22ESG+disclosure%22+OR+%22sustainability+reporting%22+OR+%22climate+risk%22)+(bank+OR+regulator+OR+central+bank)+when:3d&hl=en-US&gl=US&ceid=US:en
```

설계 의도: `when:2d~3d`로 물량 캡, 3개 피드만(비용 최소), tier1(랭킹 보너스 적정),
country=GLOBAL(진출국 국가피드를 오염시키지 않음 — 통합 화면·주제 탭에만 기여).

## 3. 왜 내가 적용하지 않았나
- `main.py run`(일일 파이프라인)이 **sources.yaml 을 자동 sync** → 내가 수정하면 검증 없이 다음 일일 수집에 즉시 반영됨.
- Google News 가 이 세션의 **양쪽 네트워크(디바이스 VM·클라우드 컨테이너) 모두 egress 차단** → 쿼리 수율·응답을 **실검증할 수 없음**.
- 검증 못 한 피드를 프로덕션 일일 수집에 넣는 건 부적절하다고 판단. 맥북에서는 네트워크가 되므로 아래 절차로 5분이면 검증됨.

## 4. 적용·검증 절차 (맥북에서)
```bash
cd ~/Documents/Claude/Projects/glb-one-teams
cp sources.yaml sources.yaml.bak          # 롤백용
# → 위 §2 블록을 sources.yaml 의 sources: 리스트 끝에 붙여넣기
python main.py init                        # 피드 등록 (feeds 수가 3 늘어나는지 확인)
python main.py fetch                       # 1회 수집 — new=N 이 늘면 쿼리가 살아있는 것

# 수율·품질 확인 (신규 3개 피드가 실제로 '금융 ESG'를 물어오는가)
python3 - <<'PY'
import sqlite3
c=sqlite3.connect("data/news.db"); c.row_factory=sqlite3.Row
for r in c.execute("""SELECT m.media_name, COUNT(*) n FROM articles_raw a
  JOIN media_sources m ON m.source_id=a.source_id
  WHERE m.media_name LIKE 'ESG %' GROUP BY m.media_name"""):
    print(r["media_name"], r["n"])
for r in c.execute("""SELECT a.title FROM articles_raw a JOIN media_sources m ON m.source_id=a.source_id
  WHERE m.media_name LIKE 'ESG %' ORDER BY a.article_id DESC LIMIT 15"""):
    print("  -", (r["title"] or "")[:80])
PY
```
**판정 기준**
- 3개 피드 합계가 하루 **0건** → 쿼리가 죽은 것. URL 인코딩/파라미터 점검 후 재시도, 안 되면 블록 삭제.
- 수집되지만 제목이 여전히 일반 기후·홍보성 → 쿼리를 더 좁히거나(예: `bond` 필수) **ESG 보강 포기하고 UI에서 ESG 필터 비중 축소**가 정직한 선택.
- 금융 ESG 제목이 잡히면 → 다음 `python main.py ai` 이후 `topics` 에 ESG 증가 확인.

**롤백**: `cp sources.yaml.bak sources.yaml && python main.py init`

## 5. 남는 판단 (제품 결정)
ESG 3%가 "수집을 더 해서 채울 문제"인지, "이 시장들에서 금융 ESG 이벤트가 실제로 드문 것"인지는
위 검증 1회면 갈립니다. 후자라면 소스를 늘려도 안 나오므로, **UI에서 ESG를 독립 필터로 두지 않고
RISK/GEO와 묶거나 비중을 낮추는 쪽**이 정직한 설계입니다.
