# glb-one-teams

KB 글로벌 거점 뉴스 수집 파이프라인 (수집 전용)

## 구조

```
fetch → keyword_filter → dedup
```

AI API 없음. 수집·필터·중복제거만 담당.

## 관리 국가

| 코드 | 국가 | KB 거점 |
|---|---|---|
| GB | 영국 | 런던 지점 |
| US | 미국 | 뉴욕 지점 |
| HK | 홍콩 | 홍콩 지점 |
| CN | 중국 | 베이징 법인 |
| JP | 일본 | 도쿄 지점 |
| SG | 싱가포르 | 싱가포르 지점 |
| IN | 인도 | 구르구람 지점 |
| VN | 베트남 | 하노이 법인 |
| MM | 미얀마 | 양곤 사무소 |
| ID | 인도네시아 | KBI은행 (자회사) |
| KH | 캄보디아 | 프라삭은행 (자회사) |

## 사용법

```bash
# 환경 세팅
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# DB 초기화
python main.py init

# 수집 실행 (fetch → filter → dedup)
python main.py run

# 개별 실행
python main.py fetch    # 피드 수집
python main.py filter   # 키워드 필터
python main.py dedup    # 중복 제거

# 최근 기사 확인
python main.py list --limit 20

# 매체 가용성 리포트
python main.py report
```

## 서버 동기화

맥북에서 수집 후 Oracle Cloud 서버로 rsync:

```bash
./sync_to_server.sh            # DB만 전송
./sync_to_server.sh --collect  # 수집 후 전송
```

> **참고:** Oracle Cloud IP는 Google News RSS에서 503 차단됨.
> 개발 단계에서는 로컬 수집 → 서버 동기화 방식으로 운영.

## 매체 현황

- 총 88개 소스, 106개 피드
- Google News 우회 피드 다수 포함 (직접 RSS가 막힌 매체)
- 중앙은행/공식기관 피드는 비활성(tier 0) 상태로 관리
