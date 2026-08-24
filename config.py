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

# '오늘의 글로벌 핵심 3줄'(briefing.generate_daily_highlights)에 투입할 기사 상한
HIGHLIGHTS_MAX_ARTICLES = 25

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
RETRY_DELAYS          = (1, 3)   # 일반 5xx/네트워크 오류 재시도 대기(초): 1회→1s, 2회→3s
GNEWS_RESOLVE_WORKERS = 30       # Google News 리다이렉트 URL 해소 병렬 수
GNEWS_RESOLVE_TIMEOUT = 5

# 429/503("얌전한 클라이언트" 대응) 전용 — 일반 5xx보다 훨씬 길게 대기 + 지터.
# Google이 결과 무시(bozo)가 아니라 명시적으로 차단 신호를 보내는 상태코드이므로 별도 취급.
RATE_LIMIT_STATUSES     = (429, 503)
RATE_LIMIT_RETRY_DELAYS = (5, 15, 45)   # 1회→5s, 2회→15s, 3회→45s (기본값, Retry-After 있으면 그 값 우선)
RATE_LIMIT_JITTER_FRAC  = 0.3           # 대기시간에 0~30% 지터 추가(동시 재시도 몰림 방지)

# Google News(news.google.com) 피드는 direct RSS와 분리된 저병렬 풀 + 요청 간 딜레이로 처리.
GNEWS_FETCH_DELAY  = 1.5   # Google News 요청 사이 최소 간격(초, 지터 포함하면 실제로는 더 김)
GNEWS_MAX_PARALLEL = 3     # Google News 전용 풀 병렬 수 (direct RSS는 MAX_PARALLEL_FETCH=8 유지)

# ── 국가별 거시지표(indicators.py) ────────────────────────────────
# fx: open.er-api.com 통화코드(1 USD = X {통화}). None이면 환율 카드 없음(예: US).
# indices: yfinance 티커 목록(화면 표시 라벨 포함). 국가에 지수가 없으면 빈 리스트.
# short: 국가별 시장 신호 보드(compact) 등 좁은 공간용 축약 라벨.
INDICATOR_MAP: dict[str, dict] = {
    "GB": {"fx": "GBP", "indices": [{"symbol": "^FTSE",     "label": "FTSE100",   "short": "FTSE"}]},
    "US": {"fx": None,  "indices": [{"symbol": "^GSPC",     "label": "S&P500",    "short": "S&P"},
                                     {"symbol": "^IXIC",     "label": "나스닥종합", "short": "나스닥"}]},
    "HK": {"fx": "HKD", "indices": [{"symbol": "^HSI",      "label": "항셍지수",   "short": "항셍"}]},
    "CN": {"fx": "CNY", "indices": [{"symbol": "000001.SS", "label": "상하이종합", "short": "상해"}]},
    "JP": {"fx": "JPY", "indices": [{"symbol": "^N225",     "label": "닛케이225",  "short": "닛케이"}]},
    "SG": {"fx": "SGD", "indices": [{"symbol": "^STI",      "label": "STI",       "short": "STI"}]},
    "IN": {"fx": "INR", "indices": [{"symbol": "^NSEI",     "label": "NIFTY50",   "short": "Nifty"}]},
    # VN: 베트남 지수를 직접 커버하는 무료 티커가 없어 미국 상장 VanEck Vietnam ETF로 대체
    "VN": {"fx": "VND", "indices": [{"symbol": "VNM",       "label": "VN (VNM ETF)", "short": "VNM"}]},
    "ID": {"fx": "IDR", "indices": [{"symbol": "^JKSE",     "label": "자카르타종합", "short": "JCI"}]},
    "MM": {"fx": "MMK", "indices": []},   # 지수 없음, 환율만(공식환율 — indicators.py 참조)
    "KH": {"fx": "KHR", "indices": []},   # 지수 없음, 환율만
}

# 국가별 시장 신호 보드 3단계(go/warn/stop) 임계 — mood_level(0~100, 높을수록 안정) 기준.
SIGNAL_GO_THRESHOLD   = 70   # 이상 = 안정
SIGNAL_WARN_THRESHOLD = 55   # 이상(70 미만) = 주의, 그 미만 = 경계

# ── KB 진출국 / 미진출국 (docs/design_미진출국.md) ─────────────────
# 진출국 11개(위 INDICATOR_MAP·kb_network.KB_NETWORK와 동일 거점)는 국가별 화면
# 그대로 유지. 아래 14개국은 "한국계 은행은 있지만 KB는 없는" 시장 — 국가 구분
# 없이 통합 피드로만 노출한다(⚠️ 잠정 리스트, 이 한 곳에서만 수정하면 됨).
NON_PRESENCE_COUNTRIES: dict[str, dict] = {
    "PH": {"name_ko": "필리핀",     "name_en": "Philippines", "flag": "🇵🇭"},
    "TH": {"name_ko": "태국",       "name_en": "Thailand",    "flag": "🇹🇭"},
    "MY": {"name_ko": "말레이시아", "name_en": "Malaysia",    "flag": "🇲🇾"},
    "BD": {"name_ko": "방글라데시", "name_en": "Bangladesh",  "flag": "🇧🇩"},
    "PL": {"name_ko": "폴란드",     "name_en": "Poland",      "flag": "🇵🇱"},
    "DE": {"name_ko": "독일",       "name_en": "Germany",     "flag": "🇩🇪"},
    "FR": {"name_ko": "프랑스",     "name_en": "France",      "flag": "🇫🇷"},
    "KZ": {"name_ko": "카자흐스탄", "name_en": "Kazakhstan",  "flag": "🇰🇿"},
    "UZ": {"name_ko": "우즈베키스탄", "name_en": "Uzbekistan", "flag": "🇺🇿"},
    "AE": {"name_ko": "UAE",        "name_en": "UAE",         "flag": "🇦🇪"},
    "BR": {"name_ko": "브라질",     "name_en": "Brazil",      "flag": "🇧🇷"},
    "MX": {"name_ko": "멕시코",     "name_en": "Mexico",      "flag": "🇲🇽"},
    "AU": {"name_ko": "호주",       "name_en": "Australia",   "flag": "🇦🇺"},
    "CA": {"name_ko": "캐나다",     "name_en": "Canada",      "flag": "🇨🇦"},
}
NON_PRESENCE_CODES = tuple(NON_PRESENCE_COUNTRIES.keys())


def is_presence(cc: str) -> bool:
    """KB 진출국이면 True. 14개 미진출국 화이트리스트에 없는 코드는 전부 True
    (GLOBAL 등 기존 코드의 동작을 바꾸지 않기 위한 안전한 기본값)."""
    return cc not in NON_PRESENCE_COUNTRIES
