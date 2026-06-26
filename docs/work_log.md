# 작업 내역 (Work Log)

## 2026-06-26 현재 상태

### 레포 구조
| 레포 | 경로 | 역할 |
|---|---|---|
| glb-news-rss/prototype | 로컬 전용 | 풀 파이프라인 + KB 대시보드 (Streamlit) |
| glb-one-teams | GitHub | 수집 전용 (AI 없음), 새 UI 실험용 베이스 |

---

## 세션 이력

### 세션 1~2 (이전)
- `llm_prefilter.py` — ORDER BY filter_score DESC 변경 (고점수 기사 우선 처리)
- `keyword_filter.py` — ASEAN, UPI, CPF, gojek, VN-index 등 리전 키워드 추가
- `collector.py` — Google News URL 실제 링크 해소 기능 추가 (ThreadPoolExecutor 30 workers)
- `briefing.py` — 브리핑 재생성 스킵 로직 추가, key_stat 스키마 수정
- `score_engine.py` — 미사용 `_topic_matches()` 함수 제거
- `llm_ranker.py` — _NOISE_TITLES에서 "bi" 제거 (오탐 방지)
- `main.py` — cmd_brief 기본값 weekly → daily
- `dashboard_stocks.py` — `_latest_date()` `<= yesterday` 제약 제거 (당일 데이터 표시)
- `kb_network.py` — 뭄바이 → 구르구람 변경
- **glb-one-teams 레포 신규 생성** — GitHub: https://github.com/lSusial/glb-one-teams.git

### 세션 3 (2026-06-25)

#### Oracle Cloud 서버 세팅
- 서버: `ubuntu@168.107.56.139` (포트 22)
- SSH 키: `~/workspace/ssh-key-2026-06-25-4.key`
- 작업: git clone + venv + requirements.txt 설치 완료
- DB 초기화: 88 sources, 106 feeds

#### Google News 503 문제 발견
- Oracle Cloud IP → Google이 봇으로 감지 → 503 차단
- 서버 직접 수집: 106개 중 40 성공 / 66 실패
- **해결책:** 맥북에서 수집 → rsync로 서버 동기화 (개발 단계)
- 추후 프로덕션: Residential Proxy 도입 고려

#### 국가 구성 확정
- **HK(홍콩) 분리** — SCMP를 CN에서 HK로 이동, RTHK·HK Free Press·HKMA 추가
- **SG(싱가포르) 추가** — Straits Times·CNA를 GLOBAL→SG, Business Times·MAS 추가
- SCMP categories에서 CN 제거 (HK 전용으로 단일화)

#### 추가 파일
- `sync_to_server.sh` — 맥북→서버 rsync 동기화 스크립트

---

## 현재 관리 국가 (KB 거점 기준)

| 코드 | 국가 | 도시 | 형태 | 주요 매체 수 |
|---|---|---|---|---|
| GB | 영국 | 런던 | 지점 | 5개 |
| US | 미국 | 뉴욕 | 지점 | 7개 |
| HK | 홍콩 | 홍콩 | 지점 | 4개 |
| CN | 중국 | 베이징 | 법인 | 6개 |
| JP | 일본 | 도쿄 | 지점 | 7개 |
| SG | 싱가포르 | 싱가포르 | 지점 | 4개 |
| IN | 인도 | 구르구람 | 지점 | 7개 |
| VN | 베트남 | 하노이 | 법인 | 6개 |
| MM | 미얀마 | 양곤 | 사무소 | 9개 |
| ID | 인도네시아 | - | 자회사(KBI은행) | 7개 |
| KH | 캄보디아 | - | 자회사(프라삭은행) | 8개 |

---

## 서버 동기화 방법

```bash
# 맥북에서 수집만
python main.py run

# 수집 후 서버로 전송
./sync_to_server.sh --collect

# DB만 서버로 전송
./sync_to_server.sh
```

---

## 다음 과제
- [ ] 화면 구성 / 새 UI 설계 (glb-one-teams 기반)
- [ ] AI 프로바이더 실험 (Anthropic 외)
- [ ] 정기 수집 자동화 (맥북 cron 또는 스케줄러)
- [ ] RTHK 피드 XML 오류 수정 (SAXParseException)
