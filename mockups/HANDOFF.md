# 구현 지시서 — 모바일 UI 리디자인 (mockups → 실제 화면)

> 대상: Claude Code (glb-one-teams 레포 로컬 작업)
> 최종 갱신: 2026-09-01
> 이 문서 하나로 작업 지시 완결. 먼저 `CLAUDE.md`, `STATUS.md`, `docs/design_미진출국.md`를 읽고 시작할 것.

## 0. 한 줄 요약
`mockups/`에 있는 4개 정적 HTML 목업을 **디자인 기준(source of truth)** 으로 삼아, 실제 정적 UI로 구현/이식한다. 목업은 손으로 다듬은 최종 시안이며, 데이터만 `data/export/*.json`에서 주입하면 그대로 동작하도록 설계돼 있다.

## 1. 디자인 기준 파일 (반드시 열어서 마크업·CSS 그대로 참조)
- `mockups/pulse.html` — **홈 (Global Pulse)**
- `mockups/country_detail.html` — **진출국** (국기 선택 → 단일 국가 상세)
- `mockups/non_presence.html` — **KB 미진출국** 통합 피드
- `mockups/topics.html` — **모니터링 (TopicWatch)**
- `mockups/_archive/countries.html` — (폐기) 진출국 전체 리스트. `country_detail`로 대체됨. 구현하지 말 것.

> 4개 목업은 공통 `<style>`(디자인 토큰)과 SVG 스프라이트(국기·아이콘)를 공유한다. 실제 구현에서는 이 공통 CSS/스프라이트를 **1개 공용 파일로 추출**해 4개 화면이 재사용하도록 리팩터링할 것(목업은 각자 인라인 복제 상태).

## 2. 화면별 데이터 계약 (data/export/*.json)
| 화면 | 파일 | 경로 | 카드 필드 |
|---|---|---|---|
| 홈 Top Issues | `pulse.json` | `daily_highlights[]` | `headline_ko`, `country_codes[0]`(국기), `category`(대표 카테고리) |
| 홈 지도/브리핑 | `pulse.json` | `country_signals`, `categories`, `daily_highlights` | — |
| 진출국 상세 | `countries.json` | `countries[]` | `brief.ko`, `articles[]`(t,q,src,d,u), `indicators[]`(kind=fx/index/policy_rate: label,value,change_pct,note) |
| 미진출국 피드 | `countries.json` | `non_presence.articles[]` | cc, flag, cc_label, src, d, t, q, score, u, related_count |
| 모니터링 | `topics.json` | `categories[]` | code(REG/DEAL/INCIDENT), presence(진출/미진출), ccs, articles[](t,q,k,src,d,score,u) |

- 진출국 상세: 진출국(=presence "진출") 카드는 `articles`가 국가별 큐레이션. **국가 브리핑 있음.**
- 미진출국: `non_presence`는 이미 **국가 병합된 단일 피드**. 국가별 네비게이션/브리핑 **없음**. 카드에 국가 태그(국기+국가명)만.
- 모니터링: 주제(3종) 안에서 진출+미진출 기사를 **병합, score 내림차순**. 카드에 국가 태그 + 진출/미진출 칩.

## 3. 화면별 사양 (목업과 1:1)
### 3.1 홈 (pulse)
- 상단 상태바·알림벨·LIVE펄·탭·하단 배너 **없음**(목업에서 이미 제거됨).
- GLOBAL PULSE: 도트 세계지도 + 거점 말풍선. 지도는 `CROP={c0:16,c1:73,r0:9,r1:35}` 상수로 크롭(빈 태평양/아메리카 서부 컷). 하단 집계(N Markets·Signals·Articles)·건수 표시 **없음**. 범례는 RISK/WATCH/POSITIVE만.
- TODAY'S GLOBAL BRIEF: 제목 밑 설명 문단 **없음**. 지표 칩(금리/FX/에너지/규제)은 **한 줄 가로 스크롤**. 칩 상하 패딩 타이트.
- TODAY'S TOP ISSUES: `daily_highlights` 10건을 **한 줄 행**으로. rank 번호·국가명·View All·헤더 카운트 **없음**. Top1만 골드 강조(연골드 배경). 국기는 **좌측 컬러바에 flush로 붙인 둥근 직사각형**(왼쪽 각짐, 오른쪽 라운드; clipPath `#rr`). 나머지 무채색.
- COUNTRIES: 하단 국기 스크롤(원형 국기). 각 국기 → 해당 국가 상세로 이동(3.2).

### 3.2 진출국 상세 (country_detail)
- 상단 **국기 선택기**(원형 국기 가로 스크롤, 홈 COUNTRIES 방식 재사용). 선택 국가 골드 링.
- 한 번에 **한 국가만** 표시: 국기(좌측 컬러바 부착) + 국가명 + KB 진출/상태 배지.
- **지표 빅넘버** 카드 행: fx(값+변동%), index(값+변동%), policy_rate(값%+기준일). 변동 색상은 **한국식(상승 빨강 #E0303F / 하락 파랑 #2563EB / 보합 회색)**.
- **국가 일일 브리핑**(전문).
- **현지언론 피드**: 기사 제목 + 요약(q) 2줄 + 출처·날짜 + chevron. 기사 없으면 "최근 수집된 현지 언론 기사가 없습니다 · 모니터링 중".

### 3.3 KB 미진출국 (non_presence) — 별도 페이지
- 국가 구분 없는 **단일 통합 피드**(국가 네비게이션 없음). 상단 안내 바 + 건수·기준일.
- 카드: 국가 태그(이모지 국기 + 국가명) + 제목 + 요약 2줄 + 출처·날짜 + (관련 N건). **중요도(score)순.**
- 국가 브리핑 없음. 대상 국가가 많아 국기는 **이모지** 사용(커스텀 SVG 아님).

### 3.4 모니터링 (topics)
- 상단 **주제 탭**: 규제(REG) / 거래·투자(DEAL) / 사건사고(INCIDENT). 선택 시 건수 표시.
- 주제 헤더에 진출/미진출 집계. 선택 주제의 진출+미진출 기사 병합·score순.
- 카드: 국가 태그(이모지 국기+국가명) + **진출/미진출 칩** + 제목 + 요약 2줄 + 출처·날짜.

## 4. 디자인 시스템 규칙 (합의됨 — 반드시 준수)
- **색은 무채색 통일 + 강조는 골드 한 색만.** 카테고리별 색 코딩 금지(어지럽다는 피드백 반영). 유일 예외: 지표 변동의 한국식 빨강/파랑.
- 모바일 **공간 절약 최우선**(여백·패딩·줄간격 타이트).
- 국기: 커스텀 인라인 SVG는 **진출 11개국 중심**. 원형 `fl-*` + 우측라운드 `flr-*` 2버전. 신규 작도된 것: JP·GB·MM·TH·LA. 대상국 많은 화면(미진출/모니터링)은 이모지 국기.
- 외부 이미지 0 (전부 인라인 SVG). 폰트만 CDN(Pretendard/Poppins).
- 반응형: 좁은 폭 우선, 넓은 폭에서 확장.

## 5. 화면 연결(내비게이션) — 이번 구현에 포함
- 4개 화면을 오갈 **공통 하단 내비게이션**(예: 홈 · 국가 · 미진출 · 모니터링) 신설.
- 홈 Top Issues·COUNTRIES 국기 → country_detail(해당 국가 선택 상태로) 진입.
- 공통 헤더(KB GLOBAL ONE TEAM + 언어토글)는 유지, 서브타이틀만 화면별.

## 6. 산출 위치 / 빌드 — ★ web/ 에 실제 반영 (확정)
신규 디자인은 **`web/*.html` 템플릿을 교체/갱신**하는 방식으로 실제 반영한다. (share/ 아님)

### 6.1 반드시 지킬 프로덕션 계약
- `web/*.html`은 **템플릿**이다. `export_json.py`가 각 템플릿의 `<script id="{name}-data">null</script>` 자리에 payload를 **주입**해 자기완결 HTML(`data/export/{name}.html`)로 빌드하고 Cloudflare Pages로 배포한다.
- 따라서 새 템플릿도 **동일한 주입 지점(`<script id="...-data">null</script>`) + 동일 JSON 계약(2장)** 을 유지해야 한다. 런타임 폴백도 유지: 주입 없으면 `fetch('{name}.json')`, `?date=YYYY-MM-DD`면 `fetch('archive/<date>/{name}.json')`.
- 템플릿 구조(주입 태그 id, 파일명)를 바꾸면 **`export_json.py`의 주입 대상도 같이 수정**하고 `python main.py export`로 빌드가 깨지지 않는지 확인할 것. (수집/AI는 건드리지 말고, export 템플릿 연동만.)
- **탭 내비게이션**: 기존 `.nav a` 링크가 `?date`·`?lang` 쿼리스트링을 보존하며 탭 간 이동한다. 이 방식 유지(하단 공용 내비도 같은 규칙).
- **언어 토글**: `?lang=en` URL 파라미터 방식(기존). 목업의 KR/EN 토글을 이 방식에 연결.
- **기사 상세**: 클릭 시 공용 모달 `web/shared-modal.js`를 재사용(목업의 chevron/카드 → 모달 오픈).

### 6.2 목업 → web 탭 매핑
| 목업 | web 파일(탭) | 데이터 | 비고 |
|---|---|---|---|
| `pulse.html` | 홈(현 `brief.html` ①) | `pulse.json` | 온도계 중심의 기존 brief를 지도+브리핑+Top Issues 신디자인으로 대체 |
| `country_detail.html` + `non_presence.html` | `countries.html` ② | `countries.json` | ②는 이미 **진출/미진출 토글 + 한국계 금융기관(KFI)·인사동향(personnel) 탭** 보유 → 진출=country_detail, 미진출=non_presence 로 넣되 **KFI·인사동향 목록/건수 탭은 유지**(신디자인으로 감싸기) |
| `topics.html` | `topics.html` ③ | `topics.json` | 이벤트유형 탭(규제·거래투자·사건사고)+진출/미진출 토글. 목업과 동일 컨셉 |
| — | `weekly.html` ④ | `weekly.json` | **보류**(기능 미완성). 이번 범위 아님 |
| — | `admin.html` | — | 손대지 말 것 |

- ②(countries)는 목업이 커버 못 한 부분(KFI·인사동향 탭, 진출/미진출 상단 토글, `?date` 아카이브, 모달)이 있으니 **기존 countries.html 동작을 먼저 파악한 뒤** 신디자인으로 감쌀 것. 진출국 국가 선택 UX는 country_detail 목업의 국기 선택기 채택.

## 7. 완료 기준 (검증)
- 4개 화면이 `data/export/*.json` 실제 데이터로 렌더되고, 하단 내비로 상호 이동 가능.
- 홈 국기/이슈 → 국가 상세 이동 동작.
- 모바일(≤420px)에서 가로 스크롤 없음(피드 카드 제외), 목업과 시각적으로 일치.
- 라이트/다크 및 폰트 로드 실패 시에도 레이아웃 깨지지 않음.
- 스크린샷으로 목업 대비 대조 확인.
