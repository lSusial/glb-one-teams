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
ANTHROPIC_MODEL_SMART = "claude-sonnet-4-6"           # llm_ranker / briefing
ANTHROPIC_MAX_TOKENS  = 1024

# OpenAI 등 추가 시 사용할 자리(어댑터는 llm_provider.py)
OPENAI_MODEL_FAST  = "gpt-4o-mini"
OPENAI_MODEL_SMART = "gpt-4o"

LLM_MAX_RETRIES = 2
LLM_RETRY_BASE_SEC = 2.0

# ── AI 파이프라인 파라미터 ───────────────────────────────────────
PREFILTER_LIMIT = 300        # 한 번 실행에 처리할 최대 기사 수(prefilter)
RANK_LIMIT      = 150        # 한 번 실행에 처리할 최대 기사 수(ranker)

# ai_score(0~100) 가 이 값 이상이면 UI에서 ACTIVE(노출), 미만은 SOURCE WATCH
AI_SCORE_ACTIVE_THRESHOLD = 60

# 국가 브리핑에 투입할 기사 상한(국가·기간당)
BRIEFING_MAX_ARTICLES = 25

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
