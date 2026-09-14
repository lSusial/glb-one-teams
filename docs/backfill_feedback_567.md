# 피드백 5·6·7 백필·배포 런북 (맥북 전용)

> 작성 2026-09-11. LLM 단계는 **맥북에서만** 동작(샌드박스는 API키 차단). 코드는 이미 레포에 반영됨.
> 결정·설계: 프로젝트 메모리 `feedback_2026-09_decisions.md`, `country_tagging_deferred.md`.

## 무엇이 바뀌었나 (코드)
- **5 카테고리 재정의**: `taxonomy.yaml` 신 6종(ECONOMY/MARKETS/TECH/GEO/POLICY/SOCIETY, ESG→POLICY 흡수).
  `llm_ranker` 프롬프트에 카테고리 판단기준(①핵심변화 ②영향대상 ③세부) 추가.
  UI 필터: `web/countries.html`(필터 버튼), `web/shared-modal.js`(배지 라벨/색).
- **7 주제국가(primary_country)**: `articles_raw.primary_country` 신설. `llm_ranker`가 직접 출력.
  기존분은 `keyword_filter.backfill_primary_country`(무료 키워드 폴백). 표시(인사동향·한국계금융·
  모니터링·미진출)는 `export_json`이 주제국가 우선(폴백 매체국). 진출국 현지피드는 매체국 유지.
- **6 모달 요약 분량**: `llm_expand._SYS` 목표를 3~4문단으로 통일.

## 실행 순서 (레포 루트에서)
```bash
# 0) 변경 확인
git status

# 1) 주제국가 키워드 폴백 — 무료(LLM 없음). primary_country 컬럼 자동 생성 + 기존기사 채움
python main.py backfill-country
#   실측: passed ~35,800건 중 ~5,900건(16.6%) 채움. 국기 오분류(인니 총재 등) 이 단계만으로도 교정.

# 2) 노출(ACTIVE) 기사 재랭킹 — 신 6카테고리 + primary_country (LLM)
python main.py rank --redo-days 14
#   대상 ≈284건(14일 노출 ACTIVE), rank 단가 ~2원 → 약 570원. 미채점 백로그는 건드리지 않음.
#   창 조정: 7일 149건(~300원) / 14일 284건(~570원) / 21일 565건(~1,130원).
#   ※ redo는 재랭킹분의 summary_ko·kb_implication·expanded_summary를 NULL로 비워 아래서 재생성됨.

# 3) 한국어 번역 재생성 (LLM, 저비용)
python main.py translate --days 14

# 4) 모달 긴요약 3~4문단으로 통일 재생성 (LLM, Haiku — 노출 모달만)
python main.py expand --redo-days 14

# 5) (선택) 일일 브리핑/하이라이트 갱신 — 카테고리 라벨 반영 원하면
# python main.py brief --type daily
# python main.py highlights

# 6) export → Cloudflare Pages 배포
python main.py export
wrangler pages deploy data/export --project-name kb-global-daily --commit-dirty=true

# 7) (선택) Oracle Cloud 동기화
rsync -avz -e "ssh -i ~/workspace/ssh-key-2026-06-25-4.key" data/export/ ubuntu@168.107.56.139:~/glb-one-teams/data/export/

# 8) git 커밋
git add taxonomy.yaml llm_ranker.py llm_expand.py keyword_filter.py export_json.py main.py \
        web/countries.html web/shared-modal.js web/topics.html web/links.html \
        web/brief.html web/shared-glossary.js \
        docs/backfill_feedback_567.md
git commit -m "feat: 피드백 5·6·7 — 신 6카테고리, primary_country(주제국가), 모달요약 3~4문단 통일"
```

## 검증 포인트 (배포 후 육안)
- 뉴스 탭 카테고리 필터가 신 6종(경제/금융시장/기술/지정학/정책/사회)으로 뜨는지.
- 인사동향/모니터링에서 "인도네시아 중앙은행 총재" 류가 **ID 🇮🇩**로 뜨는지(과거 CN/GB/JP 오표기).
- 모달 요약 길이가 대체로 3~4문단으로 고른지.
- ⚠️ 미진출 피드에 드물게 진출국(예: US) 국기가 섞일 수 있음 — 결정된 "라벨 보정" 동작(주제국가 우선).

## 롤백
각 파일 `*.bak`(이 세션이 남김) 또는 `git checkout -- <file>`. taxonomy는 `taxonomy.yaml.bak`.
