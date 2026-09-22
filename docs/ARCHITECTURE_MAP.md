# glb-one-teams 아키텍처 기준 맵

> 이후 수정 때 "어디를 건드릴지" 바로 찾는 기준 문서. 최초 작성 2026-09-21.
> 화면 사양=`mockups/HANDOFF.md`, 현황=`STATUS.md`, 랭킹=`docs/rank_score_spec.md`, 피드백 현황=`docs/feedback_status.md`.

## 1. 파이프라인 흐름

```
수집        선별(무료)         AI 레이어(맥·토큰)                      출력            배포
fetch  →  keyword_filter  →  prefilter → fulltext → rank →       →  export_json  →  Cloudflare
         (+무료 dedup)        translate → expand → llm_dedup          (JSON+HTML)     Pages
              │                                    │                      │
         점수제 ≥임계                        "확정 후 제점검"          정적 주입/복사
                                            (근접중복 최종 정리)
```
- 무료 단계(수집·키워드·무료중복)는 어디서나. **AI 단계(prefilter~llm_dedup)는 맥에서만**(샌드박스는 Anthropic 차단).
- 모델=Haiku, Message Batches(50%↓), system 프롬프트 캐싱 ON.

## 2. 파일별 역할

### 엔트리·공통
| 파일 | 역할 |
|---|---|
| main.py | CLI 디스패처. 서브커맨드→cmd_* 함수. 맨 아래 dict가 이름→함수 매핑. cmd_export가 정적 자산 복사 목록 보유 |
| config.py | 모든 튜닝 상수(임계·상한·기간·모델). §4 참조 |
| db.py | SQLite 연결, ensure_columns, days_clause_now(날짜창) |
| taxonomy.py / taxonomy.yaml | 6 카테고리 + 이벤트유형(REG/DEAL/INCIDENT/SANCTION) 정의·프롬프트 문구 |
| ranking.py | 표시정렬용 rank_score(다매체·tier·최신성), order(), cluster_sizes() |
| llm_provider.py | LLM 추상화. get_provider("fast"/"smart"), complete_json_batch, 배치·캐싱 |

### 파이프라인 단계
| 파일 | 단계 | 핵심 출력 |
|---|---|---|
| collector.py | fetch | 원문 수집(106+ 피드) |
| keyword_filter.py | filter + 무료dedup | filter_decision, duplicate_of(제목 토큰겹침, (cc,date)그룹) |
| llm_prefilter.py | prefilter | llm_prefilter keep/drop (입력=제목+요약600자) |
| fulltext.py | fulltext | full_text(본문, ~40% 성공) |
| llm_ranker.py | rank | ai_score·title_ko/en·summary_en·topics·event_type·primary_country·kb_implication_en |
| llm_translate.py | translate | summary_ko·kb_implication (EN기준본→KO) |
| llm_expand.py | expand | expanded_summary(모달 긴요약, 다출처 종합) |
| llm_dedup.py | ai-dedup | dup_by_ai·duplicate_of (**primary_country 기준** 그룹핑, "같은 사건" 병합) |
| indicators.py | indicators / indicators-history | indicators(일 스냅샷) + indicator_history(주봉 추세) |
| briefing.py | brief / highlights | 국가 일일 브리핑, top 하이라이트 |

### 출력
| 파일 | 역할 |
|---|---|
| export_json.py | DB→JSON+HTML. 함수: export_countries(뉴스)·export_pulse(홈)·export_weekly·export_topics·export_markets(지표). 공통: _write_json(+아카이브), _inject_html(템플릿 주입), _cluster_dedup / _dedup_country_feed(표시 근접중복), _dedup_country_feed는 국가내 스토리 축소 |

## 3. 웹 (정적 5탭 SPA)

web/*.html = 템플릿(데이터 `<script id="X-data">null</script>`에 export가 주입).

| 탭 | 파일 | data-id | 데이터 |
|---|---|---|---|
| 홈 | brief.html | pulse-data | pulse.json |
| 뉴스 | countries.html | countries-data | countries.json |
| 모니터링 | topics.html (+links.html) | topics-data | topics.json |
| 지표 | markets.html | markets-data | markets.json |
| 주간 | weekly.html | weekly-data | weekly.json |

공용 자산(export가 복사): shared-tokens.css(디자인토큰)·shared-sprite.js(SVG 국기/아이콘)·shared-modal.js(기사모달)·shared-glossary.js(용어설명)·shared-datenav.js(날짜선택)·intro.html/intro.mp4(인트로)·index.html.
- 모든 페이지: 하단 5탭 내비 + 한/영 토글 + ?date=/?lang= 쿼리 계승. 과거는 archive/YYYY-MM-DD/*.json.

## 4. 튜닝 노브 맵 (config.py) — 수정 1순위

| 상수 | 의미 | 영향 |
|---|---|---|
| AI_SCORE_ACTIVE_THRESHOLD=55 | 노출 컷 | 물량(올리면 줄어듦) |
| PREFILTER_LIMIT=1600 / RANK_LIMIT=700 | 1회 처리 상한 | 비용·백로그 |
| RANK_BODY_MAXLEN | rank 본문 입력 길이 | 토큰(2000→1200 축소안) |
| SYNTH_MAX_SOURCES=5 / SYNTH_SNIPPET_MAXLEN=600 | expand 다출처 | expand 토큰 |
| COUNTRY_MAX_ARTICLES=8 | 국가당 노출 상한 | 물량(도배 방지) |
| COUNTRY_STORY_DEDUP_SIM=0.38 | 표시 근접중복 병합 민감도 | 중복↓(낮출수록 더 묶음) |
| COVERAGE_FLOOR=2 / COVERAGE_FILL_DAYS=7 | 얇은 거점 보충 | 빈 화면 방지 |
| NON_PRESENCE_DEDUP_SIM=0.5 / TOP_NEWS_SIM=0.5 | 미진출·홈 중복 임계 | 중복 |

프롬프트 노브: llm_ranker._system_prompt(점수 루브릭·시사점 규칙), llm_dedup._SYSTEM(중복 병합 기준), llm_prefilter._SYSTEM(1차 선별).

## 5. "이걸 고치려면 어디를" 맵

| 하고 싶은 것 | 건드릴 곳 |
|---|---|
| 노출 뉴스 수 조절 | AI_SCORE_ACTIVE_THRESHOLD, COUNTRY_MAX_ARTICLES |
| 점수 변별력(순위 품질) | llm_ranker._system_prompt 루브릭 → 맥 재랭킹 |
| 중복 더/덜 묶기 | COUNTRY_STORY_DEDUP_SIM(표시,즉시) / llm_dedup._SYSTEM(의미,맥) |
| 국가간 매크로 도배 | llm_dedup(primary_country 그룹핑) → 맥 ai-dedup |
| 카테고리 체계 | taxonomy.yaml |
| 수집원 추가 | sources.yaml |
| KB 시사점 문구 | llm_ranker kb_implication 규칙 → 맥 |
| 화면 레이아웃 | web/<탭>.html + shared-tokens.css |
| 새 탭 추가 | web/새페이지.html + 각 페이지 내비 + export_json.export_* + 복사목록(main.py) |
| 토큰 절감 | RANK_BODY_MAXLEN, 캐싱, 무료필터 강화 |

## 6. 데이터 모델 (핵심)

- articles_raw: article_id, title/title_ko/title_en, summary_en/summary_ko, kb_implication(_en), expanded_summary(_en), ai_score, topics, event_type, primary_country, duplicate_of, dup_by_ai, filter_decision, full_text, source_links, korean_fi, personnel_move, published_at, ai_model
- media_sources: source_id, media_name, primary_country_code, tier, language
- indicators: date, country, kind(fx/index/policy_rate/bond10y), symbol, value, change_pct  (일 스냅샷)
- indicator_history: country, kind, symbol, d, close  (주봉 추세, PK=country+kind+symbol+d)

표시 국가 규칙: 진출국 뉴스 피드는 **매체국(media primary_country_code)** 기준. dedup·주제는 **primary_country(주제국가)** 우선.

## 7. 실행 명령 (맥)
```bash
python main.py run              # fetch→filter→dedup
python main.py ai --days 2      # prefilter→…→llm_dedup (배치)
python main.py ai-dedup         # 근접중복만 재판정(멱등)
python main.py export           # JSON/HTML 생성 (토큰 0)
python main.py indicators        # 일 지표 스냅샷
python main.py indicators-history # 6개월 주봉 추세
wrangler pages deploy data/export --project-name kb-global-daily --commit-dirty=true
```
