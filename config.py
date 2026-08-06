"""
glb-one-teams 공용 설정 — 경로·임계값·LLM 파라미터 단일 출처.

기존 main.py에 흩어져 있던 경로 상수를 여기로 모은다(리팩토링).
AI 레이어(llm_*, briefing, export)도 이 값을 공유한다.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── 경로 ────────────────────────────────────────────────────────
ROOT        = Path(__file__).resolve().parent
DATA_DIR    = ROOT / "data"
DB_PATH     = DATA_DIR / "news.db"
SCHEMA      = ROOT / "schema.sql"
SOURCES     = ROOT / "sources.yaml"
TAXONOMY    = ROOT / "taxonomy.yaml"
EXPORT_DIR  = DATA_DIR / "export"            # data/export/*.json (UI 데이터 계약)
REPORT_PATH = DATA_DIR / "availability_report.md"

# ── LLM 프로바이더 ───────────────────────────────────────────────
# 환경변수로 오버라이드 가능: LLM_PROVIDER, ANTHROPIC_API_KEY
DEFAULT_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")

# 작업별 모델 분리 — 싼 모델로 거르고, 좋은 모델로 분석/합성 (비용 최적화)
ANTHROPIC_MODEL_FAST  = "claude-haiku-4-5-20251001"   # llm_prefilter (저비용·고속)
ANTHROPIC_MODEL_SMART = "claude-haiku-4-5-20251001"   # llm_ranker / briefing (비용 절감 위해 Haiku로 전환)
ANTHROPIC_MAX_TOKENS  = 1024

# OpenAI 등 추가 시 사용할 자리(어댑터는 llm_provider.py)
OPENAI_MODEL_FAST  = "gpt-4o-mini"
OPENAI_MODEL_SMART = "gpt-4o"

LLM_MAX_RETRIES = 2
LLM_RETRY_BASE_SEC = 2.0

# ── Message Batches API (비실시간 일괄 처리 → 토큰 50% 할인) ─────────
# 일일 파이프라인은 지연에 둔감하므로 배치가 기본. --sync 또는 LLM_USE_BATCH=0 로 끄면 동기 호출.
LLM_USE_BATCH        = os.environ.get("LLM_USE_BATCH", "1") not in ("0", "false", "False")
LLM_BATCH_MIN        = 2       # 요청 수가 이보다 적으면 배치 안 함(오버헤드 방지 → 동기)
LLM_BATCH_CHUNK      = 10000   # 배치당 최대 요청 수(API 한도)
LLM_BATCH_POLL_SEC   = 10      # 완료 폴링 간격(초)
LLM_BATCH_MAX_WAIT_SEC = 24 * 3600   # 배치 최대 대기(초, API SLA=24h)

# ── AI 파이프라인 파라미터 ───────────────────────────────────────
# 상한이 낮으면 filter_score 뒤쪽(소규모 거점: GB꼬리·MM·KH)이 미처리로 밀려 WATCH가 됨.
# Haiku+배치(50%↓)라 처리비용이 싸므로 하루 물량(전일+당일 ~700건)을 다 소화하게 상향.
PREFILTER_LIMIT = 800        # 한 번 실행에 처리할 최대 기사 수(prefilter)
RANK_LIMIT      = 400        # 한 번 실행에 처리할 최대 기사 수(ranker)

# ── 본문 추출(fulltext.py) ───────────────────────────────────────
# prefilter 통과분만 원문 본문 추출(무료). 스니펫(~141B) 대신 본문으로 rank 품질↑.
FULLTEXT_LIMIT   = 400       # 한 번 실행에 본문 추출할 최대 기사 수
FULLTEXT_WORKERS = 12        # 병렬 fetch 수
FULLTEXT_TIMEOUT = 15        # 원문 fetch 타임아웃(초)
FULLTEXT_MAXLEN  = 12000     # 저장 본문 최대 길이(자)
RANK_BODY_MAXLEN = 4000      # rank 프롬프트에 넣는 본문 최대 길이(자)

# ai_score(0~100) 가 이 값 이상이면 UI에서 ACTIVE(노출), 미만은 SOURCE WATCH
# 60→55: 임계 바로 아래(50~59)에 준수한 거시·금융 뉴스가 몰려 있어 노출 폭을 넓힘
AI_SCORE_ACTIVE_THRESHOLD = 55

# 국가 브리핑에 투입할 기사 상한(국가·기간당)
BRIEFING_MAX_ARTICLES = 25

# '오늘의 핵심 뉴스'(Global) 다양성 — 한 나라·같은 사건 도배 방지
TOP_NEWS_PER_COUNTRY = 2      # 핵심 뉴스 블록에서 한 국가 최대 노출 수
TOP_NEWS_SIM         = 0.5    # 제목 유사도 이 이상이면 근접 중복으로 제외(0~1)

# ── 수집(collector.py) 튜닝 ──────────────────────────────────────
# 한국 일부 매체는 봇 UA를 403으로 차단 → 일반 브라우저 UA 사용
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SEC   = 20
MAX_PARALLEL_FETCH    = 8
RETRY_DELAYS          = (1, 3)   # 실패 시 재시도 대기(초): 1회→1s, 2회→3s
GNEWS_RESOLVE_WORKERS = 30       # Google News 리다이렉트 URL 해소 병렬 수
GNEWS_RESOLVE_TIMEOUT = 5
