"""
glb-news-rss 2단계 키워드 필터 (v3 — 제목/본문 분리 점수 + 중복 제거)

점수 체계 (v3):
  제목 금융 키워드 히트: +5   (본문보다 1.6배 가중)
  본문 금융 키워드 히트: +3
  제목 국가 키워드 히트: +2   (국가당 1회)
  본문 국가 키워드 히트: +1   (국가당 1회)
  제외 키워드 히트:      -4   (전문 대상)
  ──────────────────────────────────────────
  ≥ 3  passed   (금융 키워드 1개 단독으로 통과)
  < 3  rejected  (단순 국가 언급만으로는 통과 불가)

v2→v3 변경사항:
  - 제목/본문 분리 채점 → 제목 히트에 가중치 부여
  - PASS_THRESHOLD 2 → 3 (국가 키워드 1개만으로는 통과 불가)
  - run_dedup() 추가: 동일 이슈 중복 기사 탐지 및 표시
  - filter_score 컬럼 저장 (디버깅·분석용)

filter_stage  : 0=미처리 | 2=키워드필터완료
filter_decision: 'pending' | 'passed' | 'rejected'
filter_reason  : 통과·거부 주요 근거 문자열
filter_score   : 키워드 합산 점수
duplicate_of   : 중복인 경우 원본 article_id, NULL이면 독립 기사
"""
from __future__ import annotations

import html as _html
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher

import db

log = logging.getLogger("keyword_filter")

# ---------------------------------------------------------------------------
# 점수 상수 (v3)
# ---------------------------------------------------------------------------
FINANCE_SCORE_TITLE  =  5   # 금융·ESG 키워드가 제목에서 히트
FINANCE_SCORE_BODY   =  3   # 금융·ESG 키워드가 본문에서 히트
COUNTRY_SCORE_TITLE  =  2   # 국가 키워드가 제목에서 히트 (국가당 1회)
COUNTRY_SCORE_BODY   =  1   # 국가 키워드가 본문에서 히트 (국가당 1회)
EXCLUSION_SCORE      = -4   # 스포츠·연예 제외 키워드 히트당
PASS_THRESHOLD       =  3   # ≥3 이면 passed (제목 금융 히트 단독으로 통과)
BODY_ONLY_THRESHOLD  =  5   # 제목 금융 키워드 없이 본문만 히트 시 더 높은 기준

# 중복 탐지
DEDUP_THRESHOLD      = 0.75  # 제목 유사도 임계값 (0.60은 "rate hike" vs "rate hold" 같은 다른 기사도 합침)

# ---------------------------------------------------------------------------
# 글로벌 카테고리 코드 (리포트용)
# ---------------------------------------------------------------------------
GLOBAL_CATEGORIES = ("GLOBAL_GENERAL", "GLOBAL_ECONOMY")

# ---------------------------------------------------------------------------
# ① 금융·경제·ESG 키워드 (영문, 소문자 부분 매칭)
# ---------------------------------------------------------------------------
FINANCE_KEYWORDS: list[str] = [
    # 거시경제
    "gdp", "inflation", "deflation", "recession", "stagflation",
    "monetary policy", "fiscal policy", "interest rate", "rate hike", "rate cut",
    "central bank", "quantitative easing", "quantitative tightening",
    "stimulus", "austerity", "budget deficit", "trade deficit", "surplus",
    "public debt", "sovereign debt", "credit rating",
    "economic growth", "economic output", "economic reform",
    # 재정·예산
    "budget", "fiscal year", "annual report", "fiscal stimulus",
    "tax cut", "tax hike", "tax reform", "tax rate", "income tax", "corporate tax",
    "debt", "national debt",
    # 금융·은행
    "bank", "banking", "central bank",
    "loan", "lending", "microfinance", "remittance",
    "financial stability", "financial sector",
    "stock market", "stock exchange", "equity market", "bond market", "capital market",
    "yield curve", "treasury", "etf", "hedge fund", "private equity",
    "ipo", "listing", "delisting", "dividend", "earnings", "revenue", "profit",
    "forex", "exchange rate", "currency", "currency depreciation", "currency appreciation",
    "share price", "equity shares",
    # 기업·투자·거래
    "trade deal", "merger", "acquisition", "m&a", "takeover", "joint venture",
    "investment", "venture capital", "fdi", "foreign direct investment",
    "startup", "unicorn",
    "bankruptcy", "default", "restructuring", "privatization", "nationalization",
    "subsidy", "tariff", "sanction", "trade war", "trade agreement",
    "trade balance", "trade surplus", "trade deficit", "trade volume",
    "supply chain", "export", "import", "current account",
    # 국제기구·고위직
    "imf", "world bank", "adb", "asian development bank",
    "wto", "g20", "g7", "oecd", "bis", "bank for international settlements",
    "finance minister", "minister of finance", "treasury minister",
    "central bank governor", "finance secretary",
    # 통화 (국가 귀속 통화를 금융 신호로 처리)
    "yen", "jpy", "yuan", "renminbi", "rmb", "cny",
    "rupee", "inr", "rupiah", "idr",
    "dong", "vnd", "riel", "khr", "kyat", "mmk",
    "us dollar", "usd",
    "pound sterling", "sterling", "gbp",
    "hong kong dollar", "hkd", "singapore dollar", "sgd",
    # 주요 주가지수·거래소
    "nikkei", "topix", "sensex", "nifty", "kospi", "kosdaq", "ihsg",
    "nasdaq", "s&p 500", "dow jones",
    "hang seng", "straits times index", "ftse", "ftse 100", "hkex", "sgx",
    # 중앙은행·금융감독 (약어)
    "the fed", "federal reserve", "pboc", "boj", "rbi", "sebi",
    "bank of japan", "bank indonesia", "ojk",
    "people's bank of china", "reserve bank of india",
    "bank of england", "boe", "hkma", "hong kong monetary authority",
    "monetary authority of singapore",
    # 에너지·원자재·EV
    "oil price", "oil market", "crude oil", "natural gas", "gas price",
    "commodit",
    "electric vehicle", "new energy vehicle", "energy transition", "energy storage",
    "solar power", "wind power", "clean energy",
    "nev sales", "ev market", "ev sales",
    # 은행 건전성·감독
    "non-performing loan", "npl ratio", "npl",
    "capital adequacy", "capital ratio", "tier 1 capital",
    "basel", "stress test", "bank run", "bank crisis", "bank failure",
    "financial stability board", "fsb",
    "loan-to-deposit", "liquidity ratio", "solvency",
    # 디지털금융·포용금융
    "digital banking", "mobile banking", "digital payment",
    "financial inclusion", "correspondent banking",
    "payment system", "real-time payment", "cross-border payment",
    # 규제·정책·핀테크
    "regulation", "deregulation", "compliance", "antitrust",
    "fintech", "digital asset", "cryptocurren", "bitcoin", "blockchain",
    "cbdc", "digital currency",
    # ESG·기후
    "esg", "carbon emission", "carbon tax", "carbon credit",
    "climate change", "net zero", "renewable energy", "green energy",
    "sustainability", "sustainable finance", "green bond", "social bond",
    "governance", "corporate governance", "disclosure", "csr",
    # 노동시장·고용 (경제 지표)
    "job market", "labor market", "labour market", "employment rate",
    "unemployment rate", "jobless", "nonfarm payroll", "payroll",
    "wage growth", "minimum wage", "workforce", "labor force",
    # 금융 소송·규제집행
    "lawsuit", "litigation", "legal action", "settlement",
    "financial penalty", "fine", "class action",
    "sec investigation", "doj", "department of justice",
    "financial fraud", "securities fraud", "ponzi",
    # 무역·공급망 보완
    "trade war", "trade tensions", "trade friction",
    "reshoring", "nearshoring", "onshoring",
    "chip ban", "export control", "technology transfer",
    # 부동산·자산시장
    "real estate market", "property market", "housing market",
    "home prices", "mortgage rate", "commercial real estate",
    # 소비·경기
    "consumer spending", "consumer confidence", "retail sales",
    "purchasing managers", "pmi", "manufacturing index",
    "economic indicator", "economic data", "economic outlook",
    # 물류·운송 (추가)
    "freight rate", "shipping rate", "container", "cargo volume",
    "port throughput", "logistics", "supply disruption",
    "trade route", "bulk carrier", "tanker",
    # 제조·산업 (추가)
    "manufacturing output", "factory output", "industrial output",
    "semiconductor", "chip maker", "foundry", "wafer fab",
    "auto sales", "vehicle sales", "production cut", "output cut",
    # 인프라·프로젝트 파이낸스 (추가)
    "infrastructure project", "project finance", "bond issuance",
    "development bank", "sovereign fund", "pension fund",
    # 소득·빈곤·노동 (추가)
    "per capita income", "household income", "poverty rate",
    "living wage", "income inequality", "gini",
    # 식량·원자재 (추가)
    "food price", "commodity price", "grain price", "wheat price",
    "palm oil", "rice price", "fertilizer",
    # 관광·서비스업 (경제 지표) (추가)
    "tourism revenue", "hotel occupancy", "airline revenue",
    # 금융기술·결제 (추가)
    "open banking", "buy now pay later", "bnpl", "neobank",
    # 아세안·지역 무역 협정
    "asean", "rcep", "cptpp",
    # 인도 디지털 금융 인프라
    "upi", "neft", "rtgs", "pli scheme",
    # 사회보험·국부펀드 약어
    "cpf", "epf",
    # 증시·실적·에너지·보험 보강 (2026-07 평가셋 기반)
    "stock", "shares", "equities",
    "opec", "oil production", "oil output", "oil shock",
    "fed chair", "fed chairman", "fomc", "rate decision",
    "insurance", "insurer", "reinsurance",
    "net loss", "net profit", "quarterly results", "quarterly profit",
    "multibagger",
]

# ---------------------------------------------------------------------------
# ② KB 거점국 지명·기관·통화·인명 키워드 (관리국 11개와 동기화)
# ---------------------------------------------------------------------------
COUNTRY_KEYWORDS: dict[str, list[str]] = {
    "KR": [
        "korea", "korean", "south korea", "seoul", "busan", "incheon",
        "korean won", " won ", "krw", "kospi", "kosdaq",
        "bank of korea", "bok", "financial services commission",
        "yoon suk", "lee jae",
    ],
    "US": [
        "united states", " u.s.", "america", "american",
        "washington d.c.", "new york", "wall street",
        "us dollar", "usd", "federal reserve", "the fed", "jerome powell",
        "nasdaq", "s&p 500", "dow jones", "treasury secretary",
        "trump", "donald trump", "white house",
    ],
    "CN": [
        "china", "chinese", "beijing", "shanghai", "guangzhou", "shenzhen",
        "yuan", "renminbi", "rmb", "cny",
        "people's bank of china", "pboc",
        "xi jinping", "li qiang", "cpc", "politburo",
    ],
    "JP": [
        "japan", "japanese", "tokyo", "osaka", "kyoto",
        "japanese yen", "yen", "jpy",
        "bank of japan", "boj", "kazuo ueda",
        "nikkei", "topix", "kishida", "ishiba",
    ],
    "ID": [
        "indonesia", "indonesian", "jakarta", "surabaya", "bandung",
        "indonesian rupiah", "rupiah", "idr",
        "bank indonesia", "ojk", "prabowo", "jokowi",
        "kb bukopin", "gojek", "tokopedia", "bukalapak",
    ],
    "VN": [
        "vietnam", "vietnamese", "hanoi", "ho chi minh city", "hcmc",
        "vietnamese dong", "vnd",
        "state bank of vietnam", "sbv",
        "to lam", "nguyen",
        "vn-index", "vingroup", "vinfast", "hose",
    ],
    "KH": [
        "cambodia", "cambodian", "phnom penh", "siem reap",
        "cambodian riel", "riel", "khr",
        "hun manet", "hun sen", "national bank of cambodia", "nbc cambodia",
        "prasac", "acleda", "aba bank", "wing money", "wing bank",
        "bakong", "amret", "hattha", "canadia bank",
        "cambodia development resource institute", "cdri",
        "garment sector", "garment industry",
    ],
    "MM": [
        "myanmar", "burmese", "burma", "yangon", "naypyidaw", "mandalay",
        "myanmar kyat", "kyat", "mmk",
        "central bank of myanmar", "tatmadaw", "sac", "min aung hlaing",
        "aung san suu kyi", "nld", "nug",
        "kbz bank", "aya bank", "myanmar economic bank", "meb",
        "myanmar payment union", "mpu",
        "myanmar sanctions", "myanmar economy",
    ],
    "IN": [
        "india", "indian", "new delhi", "mumbai", "bangalore", "bengaluru",
        "indian rupee", "rupee", "inr",
        "reserve bank of india", "rbi", "narendra modi", "sebi",
        "sensex", "nifty", "bse", "nse",
    ],
    # ── 거점 추가국 (세션3 HK 분리·SG 추가 시 키워드 미동기화 → 점검에서 보완) ──
    "GB": [
        "united kingdom", "britain", "british", "england", "london",
        "pound sterling", "sterling", "gbp",
        "bank of england", "boe", "ftse",
        "city of london", "downing street", "keir starmer",
    ],
    "HK": [
        "hong kong", "hongkong", "hksar",
        "hong kong dollar", "hkd", "hang seng", "hkex",
        "hkma", "hong kong monetary authority", "john lee",
    ],
    "SG": [
        "singapore", "singaporean",
        "singapore dollar", "sgd", "straits times index", "sgx",
        "monetary authority of singapore", "mas",
        "temasek", "gic", "lawrence wong",
    ],
}

# ---------------------------------------------------------------------------
# ③ 한국어 금융·경제 키워드
# ---------------------------------------------------------------------------
KOREAN_FINANCE_KEYWORDS: list[str] = [
    # 거시경제
    "금리", "기준금리", "금리인상", "금리인하", "금리동결",
    "환율", "원달러", "달러",
    "인플레이션", "물가", "소비자물가", "생산자물가",
    "경제성장", "성장률", "경기", "경기침체", "경기둔화", "불황",
    "gdp", "국내총생산",
    "무역", "교역", "수출", "수입", "무역수지", "경상수지",
    "재정", "예산", "재정적자", "재정흑자", "국가채무", "국채",
    "적자", "흑자",
    # 금융·은행
    "금융", "은행", "금융시장", "자본시장", "금융감독",
    "중앙은행", "기준금리",
    "대출", "여신", "수신", "부실대출",
    "주식", "증시", "주가", "코스피", "나스닥",
    "채권", "국채", "회사채",
    "외환", "외화", "달러화", "위안화", "엔화",
    # 기업·투자
    "투자", "외국인투자", "직접투자", "fdi",
    "인수합병", "m&a", "기업인수",
    "상장", "ipo", "기업공개",
    "파산", "부도", "채무불이행", "구조조정",
    "관세", "제재", "무역협정", "fta",
    "공급망", "반도체", "배터리", "전기차",
    # 국제기구·정책
    "imf", "세계은행", "아시아개발은행", "adb", "bis",
    "g20", "oecd",
    "통화정책", "재정정책", "경제정책", "규제",
    # ESG·에너지
    "esg", "탄소", "탄소중립", "넷제로",
    "유가", "원유", "에너지",
    "재생에너지", "태양광", "풍력",
]

KOREAN_COUNTRY_KEYWORDS: dict[str, list[str]] = {
    "US": ["미국", "미 연준", "연준", "연방준비제도", "트럼프", "바이든", "재무부"],
    "CN": ["중국", "위안화", "인민은행", "중국인민은행", "중국 경제"],
    "JP": ["일본", "엔화", "일본은행", "엔 ", "BOJ"],
    "IN": ["인도", "루피", "인도 경제", "인도중앙은행"],
    "ID": ["인도네시아", "루피아", "인도네시아 중앙은행"],
    "VN": ["베트남", "동화", "베트남 경제", "베트남 중앙은행"],
    "KH": ["캄보디아", "캄보디아 경제"],
    "MM": ["미얀마", "미얀마 경제"],
    "KR": ["한국", "한국은행", "기재부", "금융위", "코스피", "원화"],
    "GB": ["영국", "영란은행", "파운드", "런던"],
    "HK": ["홍콩", "홍콩달러", "항셍"],
    "SG": ["싱가포르", "싱가포르달러", "싱가포르 통화청"],
}

# ---------------------------------------------------------------------------
# ④ 베트남어 금융·경제 키워드
# ---------------------------------------------------------------------------
VIETNAMESE_FINANCE_KEYWORDS: list[str] = [
    "ngân hàng", "ngân hàng nhà nước", "lãi suất", "tỷ giá",
    "lạm phát", "tăng trưởng", "gdp", "kinh tế", "tài chính",
    "đầu tư", "chứng khoán", "thị trường", "cổ phiếu", "trái phiếu",
    "xuất khẩu", "nhập khẩu", "thương mại", "ngân sách", "thuế",
    "doanh nghiệp", "vốn", "tín dụng", "nợ xấu", "lợi nhuận",
    "fdi", "đồng việt nam", "vnd",
    "sbv", "bidv", "vietcombank", "vietinbank", "agribank", "vpbank",
]

# ---------------------------------------------------------------------------
# ⑤ 인도네시아어 금융·경제 키워드
# ---------------------------------------------------------------------------
INDONESIAN_FINANCE_KEYWORDS: list[str] = [
    "ekonomi", "keuangan", "moneter", "fiskal", "inflasi", "deflasi",
    "pertumbuhan", "resesi", "gdp", "pdb", "anggaran", "defisit", "surplus",
    "saham", "bursa", "ihsg", "obligasi", "investasi", "modal", "pasar modal",
    "dividen", "ipo", "emiten", "reksadana", "aset",
    "bank", "perbankan", "rupiah", "idr", "suku bunga", "kredit", "pinjaman",
    "bank indonesia", "ojk", "lkm",
    "bisnis", "perusahaan", "merger", "akuisisi", "ekspor", "impor",
    "perdagangan", "tarif", "sanksi", "subsidi", "privatisasi",
    "esg", "karbon", "energi", "keberlanjutan", "lingkungan", "tata kelola",
]

# ---------------------------------------------------------------------------
# ⑥ 제외 키워드 — 스포츠·연예·순수과학
# ---------------------------------------------------------------------------
EXCLUSION_KEYWORDS: list[str] = [
    # 스포츠
    "gold medal", "silver medal", "bronze medal",
    "powerlifter", "weightlift",
    "sea games", "asian games", "olympic",
    "world cup qualifier", "world cup match",
    "hat trick", "penalty shootout", "clean sheet",
    "sumo",
    "tennis match", "cricket match", "badminton match",
    "football match", "soccer match", "basketball game",
    "chess tournament", "chess championship",
    # 연예·문화
    "box office", "music festival", "concert tour", "music album",
    "grammy award", "academy award", "film festival",
    "chart-topping", "chart topping",
    "movie review", "book review", "art exhibition",
    # 부고·인물
    "dies at", "passed away", "in memoriam", "obituary",
    "saxophonist", "violinist", "pianist", "conductor",
    # 과학·자연
    "deep-sea", "new species", "newly discovered species",
    "paleontolog", "fossil discover",
    "marine biolog",
    # bank 비금융 오탐 방지
    "food bank", "blood bank", "seed bank", "eye bank",
    "river bank", "bank erosion", "bank robbery", "bank heist",
    "bank holiday", "piggy bank", "memory bank",
    # investment 비금융 오탐
    "investment in education", "investment in health", "investment in sport",
    # 기타 비금융 맥락
    "trade show", "trade fair", "trade expo", "trade union",
    "budget airline", "budget hotel", "budget travel",
    # 크리켓 (인도 매체 주요 노이즈)
    "ipl match", "ipl season", "ipl final", "ipl auction", "ipl trophy",
    "cricket tournament", "cricket series", "cricket board", "cricket team",
    "cricket stadium", "cricket score", "batting average", "bowling average",
    "t20 match", "t20 series", "test cricket", "one-day international",
    # 스포츠 베팅
    "sports betting", "betting odds", "bookmaker",
    # 연예·엔터테인먼트 (추가)
    "celebrity couple", "celebrity divorce", "box office hit",
    # 내비게이션·집계 페이지 (기사 아님 — 정밀도 보강, 2026-07)
    "live updates", "latest videos", "transcripts for",
    "ifsc code", "micr code", "more business stories", "more commentary stories",
    "photo gallery", "live blog", "live | ", "tags ",
    "music chart", "chart position", "streaming hit",
    # 선거 유세 (경제 내용 없는 정치 기사)
    "election rally", "campaign rally", "campaign trail",
    # 자연재해·기후 (경제 연결 없는 순수 재해 보도)
    "earthquake hit", "flood victim", "cyclone hit", "typhoon hit",
    "wildfire", "volcanic eruption",
]


# ---------------------------------------------------------------------------
# ⑦ 한국계 금융기관 태깅(축 F, 2026-08-26 회의 결정) — 관련성 필터와 별개.
# passed 기사 중 아래 기관이 언급되면 korean_fi 컬럼에 코드 태깅(멀티라벨 CSV).
# 짧은 단독 약어(kb/nh 등)는 오탐이 심해 제외 — 구별력 있는 표현·한글명만 사용.
# ---------------------------------------------------------------------------
KOREAN_FI: dict[str, dict] = {
    "SHINHAN": {"label": "신한",     "keywords": ["shinhan", "신한은행", "신한금융", "신한카드"]},
    "HANA":    {"label": "하나(KEB)", "keywords": ["hana bank", "hana financial", "keb hana",
                                                    "하나은행", "하나금융"]},
    "WOORI":   {"label": "우리",     "keywords": ["woori bank", "woori financial", "우리은행", "우리금융"]},
    "IBK":     {"label": "IBK기업",  "keywords": ["ibk", "industrial bank of korea", "기업은행"]},
    "KDB":     {"label": "KDB산업",  "keywords": ["kdb", "korea development bank", "산업은행"]},
    "NH":      {"label": "NH농협",   "keywords": ["nonghyup", "nh bank", "nh investment",
                                                    "농협은행", "농협금융"]},
    "KEXIM":   {"label": "수출입",   "keywords": ["kexim", "korea eximbank",
                                                    "export-import bank of korea", "수출입은행"]},
    # KB는 이미 전체 서비스의 핵심 축(국가브리핑·KB 시사점)이라 기본 제외.
    # KOREAN_FI_INCLUDE_KB=True로 바꾸면 포함(bare "kb"는 오탐 위험이 커 제외).
    "KB":      {"label": "KB",       "keywords": ["kb kookmin", "kb financial group",
                                                    "kookmin bank", "국민은행"]},
}
KOREAN_FI_INCLUDE_KB = False


# ---------------------------------------------------------------------------
# 인사동향(리더십 교체) 태깅 (v3, 2026-08-28 / 2차 정밀도 개선) — korean_fi와
# 별개 축·별개 스코프. 한국계 7개 기관으로 국한하지 않고 KB 네트워크(진출
# 11개국) + 미진출 14개국 전체의 금융기관·중앙은행·감독당국 리더십 동향을
# 폭넓게 태깅한다(새 수집원 없음).
#
# 1차(역할 복합어만 사용, 21건)를 실제 원문 대조한 결과 절반 이상이 "재직 중인
# CEO의 발언 인용"(예: "TD Bank CEO Says Trade Tensions...", "Bank CEOs see
# tariff dispute as manageable") 같은 단순 언급이었고 실제 교체/임명/사임 사건은
# 소수였음. 역할 단어(bank ceo 등)만으로는 "그 자리에 누가 있다"는 사실만
# 나타낼 뿐 "교체"를 의미하지 않기 때문 — 역할 단어 + 교체/이동 신호어가 함께
# 나올 때만(AND) 태깅하도록 변경. 재검증: 6건, 전부 실제 임명·사임·기소 등
# 인사 이벤트로 확인.
# ---------------------------------------------------------------------------
# v4 (2026-09-03): 역할어를 "연속 문자열"에서 정규식으로 교체.
# 실측 버그 — 인도네시아 중앙은행 총재 취임 기사 4건 중 3건이 미태깅이었다.
#   "Destry Damayanti sworn in as Bank Indonesia Governor"  → 미태깅
#   "Resmi! MA Lantik ... jadi Gubernur Bank Indonesia"     → 미태깅
#   "... first female central bank governor" (신화통신)      → 태깅
# 원인: 역할어가 "bank governor" 같은 **연속 문자열**이라 기관명이 사이에 끼면
# ("Bank *Indonesia* Governor") 매칭되지 않고, 현지어(id) 표현도 없었다.
# → 기관어·직함어를 분리해 사이 토큰을 허용하는 패턴으로 바꾸고, 수집 언어
#   (en/id/ko)의 현지어 역할·전환 표현을 추가. AND 게이트(역할+전환)는 유지.

# 기관어와 직함 사이에 낄 수 있는 부분: 연결어(of/the/negara…) 최대 2개 +
# 고유명사 1개. 서술어가 끼어 문장을 가로지르는 오탐("the bank said the
# governor…")을 막기 위해 흔한 기능어·서술어는 제외한다.
_P_GAP = (
    r"(?:\s+(?:of|the|de|negara|sentral|central))"
    r"{0,2}"
    # 낱말 전체가 기능어일 때만 배제(어두 일치 금지 — 'in'이 'indonesia'를 막던 버그)
    r"(?:\s+(?!(?:said|says|say|told|tells|and|or|but|that|which|who|when|where|"
    r"after|before|with|from|as|to|in|on|at|by|for|its|his|her|their|a|an|the|"
    r"is|was|are|were|has|have|had|will|would|also)(?![a-z0-9]))"
    r"[a-z0-9\'\u2019-]+)?"
    r"\s+"
)

# 금융 관련 기관어만(일반 기업 CEO 기사 배제)
_P_INSTITUTION = (
    r"(?:central\s+bank|reserve\s+bank|state\s+bank|people\'?s\s+bank|"
    r"monetary\s+authority|financial\s+regulator|banking\s+regulator|"
    r"insurance\s+regulator|insurance\s+authority|banking|bank)(?:\'s|\u2019s)?"
)

# 거버넌스 직함. chief economist(리서치 직책)는 인사동향이 아니라 제외.
_P_TITLE = (
    r"(?:governor|chairman|chairwoman|chairperson|ceo|chief\s+executive|"
    r"president|managing\s+director|deputy\s+governor|chief(?!\s+economist))"
)

# 역할 정규식(re.search). 여기 없는 표현은 태깅되지 않는다.
PERSONNEL_ROLE_PATTERNS: list[str] = [
    # en: "central bank governor", "Bank Indonesia Governor",
    #     "Reserve Bank of India Governor", "HDFC Bank CEO", "banking regulator chief"
    r"(?<![a-z])" + _P_INSTITUTION + _P_GAP + _P_TITLE + r"(?![a-z])",
    # en: "governor of Bank Indonesia", "CEO of the central bank"
    r"(?<![a-z])" + _P_TITLE + r"\s+of\s+(?:the\s+)?" + _P_INSTITUTION + r"(?![a-z])",
    # id: "Gubernur Bank Indonesia", "Gubernur BI", "Gubernur Senior BI"
    r"gubernur(?:\s+senior)?\s+(?:bank|bi)(?![a-z])",
    # id: "Direktur Utama Bank ...", "Dirut BNI"
    r"(?:direktur\s+utama|dirut)\s+(?:bank|bri|bni|bca|bi)(?![a-z])",
    # id: 감독당국(OJK) 수장
    r"(?:ketua|kepala)\s+(?:dewan\s+komisioner\s+)?ojk(?![a-z])",
    # ko: 직함과 인사 신호가 인접할 때만. 단독 "총재"는 통화정책 기사(총재 발언
    # 인용)에서 오탐이 나므로 쓰지 않는다.
    r"(?:신임|차기|전임|후임|새)\s*(?:총재|은행장|행장|회장|금융감독원장|금감원장|금융위원장)",
    r"(?:총재|은행장|행장|회장|금융감독원장|금감원장|금융위원장)\s*"
    r"(?:후보|취임|임명|선임|지명|내정|사임|사퇴|퇴임|교체|연임)",
]

# 위 역할어와 함께(본문 어디든) 나와야만 태깅 — 단독으로는 재직 중 발언
# 인용과 구분이 안 됨.
PERSONNEL_TRANSITION_TERMS: list[str] = [
    "named", "appointed", "appoints", "appointment", "nominated", "nominates",
    "nomination", "nominee", "sworn in", "succeeds", "successor",
    "steps down", "stepping down", "resigned", "resigns", "resignation",
    "quits", "ousted", "removed as", "exit", "exits", "outgoing", "incoming",
    "former", "ex-", "indicted", "replaces", "replacement", "candidate for",
    "takes office", "inaugurated", "term begins", "tenure",
    # id
    "dilantik", "melantik", "lantik", "ditunjuk", "menunjuk", "pelantikan",
    "calon", "pengganti", "menjabat", "mundur", "pengunduran diri",
    # ko
    "임명", "취임", "선임", "지명", "사임", "사퇴", "퇴임", "내정", "후보",
    "신임", "차기", "교체",
]

# 결합 없이 그 자체로 이미 "교체" 의미를 담은 복합 표현 — 단독 매치 허용
PERSONNEL_STRONG_PHRASES: list[str] = [
    "new central bank governor", "next central bank governor",
    "new bank ceo", "new bank chairman", "new bank president",
    "총재 후보", "신임 총재", "차기 총재", "총재 취임", "총재 지명",
    "은행장 선임", "행장 선임", "신임 은행장", "신임 행장",
    "gubernur bi baru", "gubernur baru",
]


_ROLE_PATTERN_CACHE: list[re.Pattern] = []


def _role_patterns() -> list[re.Pattern]:
    if not _ROLE_PATTERN_CACHE:
        _ROLE_PATTERN_CACHE.extend(re.compile(p) for p in PERSONNEL_ROLE_PATTERNS)
    return _ROLE_PATTERN_CACHE


def _first_role_match(text: str) -> str | None:
    for pat in _role_patterns():
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def _has_personnel_keyword(text: str) -> bool:
    if _first_match(text, PERSONNEL_STRONG_PHRASES) is not None:
        return True
    return (_first_role_match(text) is not None
            and _first_match(text, PERSONNEL_TRANSITION_TERMS) is not None)


def ensure_personnel_column(conn: sqlite3.Connection) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("personnel_move", "ALTER TABLE articles_raw ADD COLUMN personnel_move INTEGER"),
    ])


def run_personnel_tag(conn: sqlite3.Connection, recheck: bool = False) -> dict:
    """제목+본문에서 금융기관·중앙은행·감독당국 인사동향 언급을 키워드로
    태깅(personnel_move: 1/0). LLM 없음, 새 수집원 없음 — 기존 passed 기사만 검사.

    recheck=False(기본): personnel_move가 아직 NULL인 기사만(증분).
    """
    ensure_personnel_column(conn)
    cond = "" if recheck else "AND personnel_move IS NULL"
    rows = conn.execute(f"""
        SELECT article_id, title, summary FROM articles_raw
        WHERE filter_decision = 'passed' {cond}
    """).fetchall()

    cur = conn.cursor()
    tagged = 0
    for r in rows:
        text = _normalize(_clean_text(f"{r['title']} {r['summary'] or ''}"))
        flag = 1 if _has_personnel_keyword(text) else 0
        cur.execute(
            "UPDATE articles_raw SET personnel_move = ? WHERE article_id = ?",
            (flag, r["article_id"]),
        )
        if flag:
            tagged += 1
    conn.commit()

    log.info("인사동향 태깅 완료 — 검사=%d건  태깅=%d건", len(rows), tagged)
    return {"checked": len(rows), "tagged": tagged}


# ---------------------------------------------------------------------------
# DB 마이그레이션
# ---------------------------------------------------------------------------
def ensure_filter_columns(conn: sqlite3.Connection) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("filter_stage",
         "ALTER TABLE articles_raw ADD COLUMN filter_stage    INTEGER NOT NULL DEFAULT 0"),
        ("filter_decision",
         "ALTER TABLE articles_raw ADD COLUMN filter_decision TEXT    NOT NULL DEFAULT 'pending'"),
        ("filter_reason",
         "ALTER TABLE articles_raw ADD COLUMN filter_reason   TEXT"),
        ("filter_score",
         "ALTER TABLE articles_raw ADD COLUMN filter_score    INTEGER"),
    ])


def ensure_dedup_column(conn: sqlite3.Connection) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("duplicate_of",
         "ALTER TABLE articles_raw ADD COLUMN duplicate_of INTEGER REFERENCES articles_raw(article_id)"),
    ])


def ensure_korean_fi_column(conn: sqlite3.Connection) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("korean_fi", "ALTER TABLE articles_raw ADD COLUMN korean_fi TEXT"),
    ])


# ---------------------------------------------------------------------------
# 텍스트 정제 헬퍼
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def _clean_text(raw: str) -> str:
    text = _html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\xa0+", " ", text)
    text = re.sub(r"\s+-\s+[A-Za-z][\w\s]{1,60}$", "", text)
    text = re.sub(r"\s{2,}[A-Z][A-Za-z][\w\s]{1,60}$", "", text)
    return text.strip()


_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _kw_pattern(kw: str) -> re.Pattern:
    """키워드 패턴 (캐시). 순수 알파벳+숫자+공백 키워드는 단어 경계 적용.
    예: 'bis' → ibis/Bisnis 에서 오탐 방지, 'rbi' → Serbia/Gabriel 오탐 방지.
    특수문자 포함 키워드(m&a, s&p 등)는 기존 부분문자열 매칭 유지.
    """
    if kw not in _PATTERN_CACHE:
        if re.fullmatch(r"[a-z0-9 ]+", kw):
            # 끝에 선택적 복수형 s 허용: 'import'→'imports', 'stock'→'stocks'
            # (헤드라인은 복수형이 많아 단수만 매칭하면 재현율 손실)
            _PATTERN_CACHE[kw] = re.compile(
                r"(?<![a-z0-9])" + re.escape(kw) + r"s?(?![a-z0-9])"
            )
        else:
            _PATTERN_CACHE[kw] = re.compile(re.escape(kw))
    return _PATTERN_CACHE[kw]


def _first_match(haystack: str, keywords: list[str]) -> str | None:
    for kw in keywords:
        if _kw_pattern(kw).search(haystack):
            return kw
    return None


def _score_countries(title_text: str, body_text: str, kw_map: dict) -> tuple[int, str | None]:
    """국가 키워드 매칭(국가당 1회, 제목 우선) — 한국어/영문 분기에서 공통으로 쓰는 로직.

    Returns: (합산 점수, 첫 히트 국가의 reason 문자열 또는 None)
    """
    score = 0
    first_reason: str | None = None
    for country, kws in kw_map.items():
        if _first_match(title_text, kws):
            score += COUNTRY_SCORE_TITLE
            if first_reason is None:
                first_reason = f"country_title:{country}"
        elif _first_match(body_text, kws):
            score += COUNTRY_SCORE_BODY
            if first_reason is None:
                first_reason = f"country_body:{country}"
    return score, first_reason


# ---------------------------------------------------------------------------
# 핵심 필터 함수 — 제목/본문 분리 점수제 (v3)
# ---------------------------------------------------------------------------
def _apply_keyword_filter(
    title: str, summary: str, language: str = "en"
) -> tuple[str, str, int]:
    """
    제목/본문 분리 점수제 키워드 필터.

    Returns:
        (decision, reason, score)
        decision: 'passed' | 'rejected'
        reason:   주요 매칭 근거 문자열
        score:    합산 점수 (디버깅용)
    """
    title_text = _normalize(_clean_text(title))
    body_text  = _normalize(_clean_text(summary))
    full_text  = f"{title_text} {body_text}"

    score = 0
    top_reason: str | None = None
    title_finance_hit = False  # 제목에 금융 키워드 히트 여부

    # ── 제외 키워드 (전체 텍스트 대상) ──────────────────────
    excl_hit = _first_match(full_text, EXCLUSION_KEYWORDS)
    if excl_hit:
        score += EXCLUSION_SCORE

    if language == "ko":
        # ── 한국어 금융 키워드 ────────────────────────────────
        fin_t = _first_match(title_text, KOREAN_FINANCE_KEYWORDS)
        if fin_t:
            score += FINANCE_SCORE_TITLE
            title_finance_hit = True
            top_reason = f"ko_fin_title:{fin_t}"
        else:
            fin_b = _first_match(body_text, KOREAN_FINANCE_KEYWORDS)
            if fin_b:
                score += FINANCE_SCORE_BODY
                top_reason = f"ko_fin_body:{fin_b}"

        # ── 한국어 국가 키워드 (국가당 1회) ──────────────────
        c_score, c_reason = _score_countries(title_text, body_text, KOREAN_COUNTRY_KEYWORDS)
        score += c_score
        if top_reason is None:
            top_reason = c_reason

    else:
        # ── 영문/기타 금융 키워드 ─────────────────────────────
        fin_t = _first_match(title_text, FINANCE_KEYWORDS)
        if fin_t:
            score += FINANCE_SCORE_TITLE
            title_finance_hit = True
            top_reason = f"fin_title:{fin_t}"
        else:
            fin_b = _first_match(body_text, FINANCE_KEYWORDS)
            if fin_b:
                score += FINANCE_SCORE_BODY
                top_reason = f"fin_body:{fin_b}"

        # ── 인도네시아어 금융 키워드 ──────────────────────────
        if language == "id":
            id_t = _first_match(title_text, INDONESIAN_FINANCE_KEYWORDS)
            if id_t:
                score += FINANCE_SCORE_TITLE
                if top_reason is None:
                    top_reason = f"id_fin_title:{id_t}"
            else:
                id_b = _first_match(body_text, INDONESIAN_FINANCE_KEYWORDS)
                if id_b:
                    score += FINANCE_SCORE_BODY
                    if top_reason is None:
                        top_reason = f"id_fin_body:{id_b}"

        # ── 베트남어 금융 키워드 ──────────────────────────────
        if language == "vi":
            vi_t = _first_match(title_text, VIETNAMESE_FINANCE_KEYWORDS)
            if vi_t:
                score += FINANCE_SCORE_TITLE
                if top_reason is None:
                    top_reason = f"vi_fin_title:{vi_t}"
            else:
                vi_b = _first_match(body_text, VIETNAMESE_FINANCE_KEYWORDS)
                if vi_b:
                    score += FINANCE_SCORE_BODY
                    if top_reason is None:
                        top_reason = f"vi_fin_body:{vi_b}"

        # ── 국가 키워드 (국가당 1회) ──────────────────────────
        c_score, c_reason = _score_countries(title_text, body_text, COUNTRY_KEYWORDS)
        score += c_score
        if top_reason is None:
            top_reason = c_reason

    # ── 판정 ─────────────────────────────────────────────────
    # 제목에 금융 키워드가 없으면(body-only) 더 높은 기준 적용 → 오탐 감소
    threshold = PASS_THRESHOLD if title_finance_hit else BODY_ONLY_THRESHOLD
    if score >= threshold:
        return "passed", top_reason or "passed", score
    else:
        if excl_hit:
            return "rejected", f"excl:{excl_hit}(score:{score})", score
        return "rejected", f"score:{score}|{top_reason or 'no_match'}", score


# ---------------------------------------------------------------------------
# 메인 필터 실행
# ---------------------------------------------------------------------------
def run_keyword_filter(conn: sqlite3.Connection, refilter_all: bool = False) -> dict:
    """
    refilter_all=False : filter_decision='pending' 기사만 처리 (기본)
    refilter_all=True  : 전체 기사 재처리 (키워드·점수 기준 변경 후 사용)
    """
    ensure_filter_columns(conn)
    cur = conn.cursor()

    where = "1=1" if refilter_all else "a.filter_decision = 'pending'"
    rows = cur.execute(f"""
        SELECT a.article_id, a.title, a.summary, m.language, m.tier
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE {where}
    """).fetchall()

    stats = dict(total=len(rows), passed=0, rejected=0)

    for row in rows:
        if row["tier"] == 0:
            # Tier 0 공식기관 → 자동 통과 (레거시 데이터 호환)
            decision, reason, score, stage = "passed", "official_source", 99, 1
        else:
            decision, reason, score = _apply_keyword_filter(
                row["title"] or "", row["summary"] or "", row["language"] or "en"
            )
            stage = 2

        stats[decision] += 1
        cur.execute(
            """UPDATE articles_raw
               SET filter_stage = ?, filter_decision = ?, filter_reason = ?, filter_score = ?
               WHERE article_id = ?""",
            (stage, decision, reason, score, row["article_id"]),
        )

    conn.commit()
    log.info(
        "필터 완료 — 전체=%d  통과=%d  거부=%d  (통과율=%.1f%%)",
        stats["total"], stats["passed"], stats["rejected"],
        stats["passed"] / stats["total"] * 100 if stats["total"] else 0,
    )
    return stats


# ---------------------------------------------------------------------------
# 중복 기사 탐지 및 표시 (v3 신규)
# ---------------------------------------------------------------------------
def _title_similarity(a: str, b: str) -> float:
    """제목 유사도 (SequenceMatcher ratio, 0~1)."""
    a_n = re.sub(r"[^a-z0-9 ]", "", a.lower()).strip()
    b_n = re.sub(r"[^a-z0-9 ]", "", b.lower()).strip()
    if not a_n or not b_n:
        return 0.0
    return SequenceMatcher(None, a_n, b_n).ratio()


def run_dedup(conn: sqlite3.Connection, recheck: bool = False) -> dict:
    """
    같은 날짜·국가의 유사 제목 기사를 중복으로 표시 (duplicate_of 설정).

    동작:
      1. passed 기사를 (국가, 발행일) 그룹으로 묶음
      2. 그룹 내 제목 유사도 ≥ DEDUP_THRESHOLD 이면 중복 판정
      3. Tier 낮은(=품질 높은) 쪽을 원본으로 유지, 나머지에 duplicate_of 설정
      4. 중복 기사는 AI 랭킹 대상에서 제외됨

    recheck=True : duplicate_of가 이미 설정된 기사도 재처리

    Returns:
        {"checked": N, "duplicates": M}
    """
    ensure_dedup_column(conn)

    cond = "" if recheck else "AND a.duplicate_of IS NULL"
    rows = conn.execute(f"""
        SELECT a.article_id, a.title,
               DATE(datetime(COALESCE(a.published_at, a.fetched_at), '+9 hours')) AS art_date,
               m.primary_country_code AS cc,
               m.tier
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.filter_decision = 'passed'
          AND a.title IS NOT NULL
          {cond}
        ORDER BY m.tier ASC, a.published_at DESC NULLS LAST
    """).fetchall()

    # (cc, date) 별로 그룹화
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r["cc"] or "GLOBAL", r["art_date"] or "unknown")
        groups[key].append(dict(r))

    total_checked = len(rows)
    dup_count = 0
    cur = conn.cursor()

    for (cc, date), articles in groups.items():
        if len(articles) < 2:
            continue

        # 이미 그룹 내 순서가 품질 순 (tier ASC → Tier0 먼저, 그다음 1, 2)
        # in-place 중복 표시
        marked: list[bool] = [False] * len(articles)

        for i in range(len(articles)):
            if marked[i]:
                continue
            primary = articles[i]
            for j in range(i + 1, len(articles)):
                if marked[j]:
                    continue
                sim = _title_similarity(primary["title"], articles[j]["title"])
                if sim >= DEDUP_THRESHOLD:
                    marked[j] = True
                    cur.execute(
                        "UPDATE articles_raw SET duplicate_of = ? WHERE article_id = ?",
                        (primary["article_id"], articles[j]["article_id"]),
                    )
                    dup_count += 1

    conn.commit()
    log.info(
        "중복 탐지 완료 — 검사=%d건  중복표시=%d건  (%.1f%%)",
        total_checked, dup_count,
        dup_count / total_checked * 100 if total_checked else 0,
    )
    return {"checked": total_checked, "duplicates": dup_count}


# ---------------------------------------------------------------------------
# 한국계 금융기관 태깅 (v3 신규, 2026-08-26) — 관련성 필터와 완전히 별개 축.
# LLM 없음(순수 키워드 매칭), 새 수집원 없이 기존 passed 기사만 태깅.
# ---------------------------------------------------------------------------
def _korean_fi_codes(text: str) -> list[str]:
    hits = []
    for code, meta in KOREAN_FI.items():
        if code == "KB" and not KOREAN_FI_INCLUDE_KB:
            continue
        if _first_match(text, meta["keywords"]):
            hits.append(code)
    return hits


def run_korean_fi_tag(conn: sqlite3.Connection, recheck: bool = False) -> dict:
    """제목+본문에서 한국계 금융기관(KOREAN_FI) 언급을 키워드로 태깅.

    recheck=False(기본): korean_fi가 아직 NULL인 passed 기사만(증분).
    매치 없으면 빈 문자열로 저장(다음 실행 때 재검사 안 하도록 NULL과 구분).

    Returns:
        {"checked": N, "tagged": M}  (M = 1개 이상 기관이 매치된 기사 수)
    """
    ensure_korean_fi_column(conn)
    cond = "" if recheck else "AND korean_fi IS NULL"
    rows = conn.execute(f"""
        SELECT article_id, title, summary FROM articles_raw
        WHERE filter_decision = 'passed' {cond}
    """).fetchall()

    cur = conn.cursor()
    tagged = 0
    for r in rows:
        text = _normalize(_clean_text(f"{r['title']} {r['summary'] or ''}"))
        codes = _korean_fi_codes(text)
        cur.execute(
            "UPDATE articles_raw SET korean_fi = ? WHERE article_id = ?",
            (",".join(codes), r["article_id"]),
        )
        if codes:
            tagged += 1
    conn.commit()

    log.info("한국계 금융기관 태깅 완료 — 검사=%d건  태깅=%d건", len(rows), tagged)
    return {"checked": len(rows), "tagged": tagged}


# ---------------------------------------------------------------------------
# 필터 결과 리포트
# ---------------------------------------------------------------------------
def build_filter_report(conn: sqlite3.Connection) -> str:
    ensure_filter_columns(conn)

    tot = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN filter_decision='passed'   THEN 1 ELSE 0 END) AS passed,
               SUM(CASE WHEN filter_decision='rejected' THEN 1 ELSE 0 END) AS rejected,
               SUM(CASE WHEN filter_decision='pending'  THEN 1 ELSE 0 END) AS pending
        FROM articles_raw
    """).fetchone()

    dup_stats = conn.execute("""
        SELECT COUNT(*) AS total_dups
        FROM articles_raw
        WHERE duplicate_of IS NOT NULL
    """).fetchone()

    placeholders = ",".join("?" * len(GLOBAL_CATEGORIES))
    glob = conn.execute(f"""
        SELECT COUNT(DISTINCT a.article_id) AS total,
               SUM(CASE WHEN a.filter_decision='passed'   THEN 1 ELSE 0 END) AS passed,
               SUM(CASE WHEN a.filter_decision='rejected' THEN 1 ELSE 0 END) AS rejected
        FROM articles_raw a
        WHERE EXISTS (
            SELECT 1 FROM media_category_map mc
            WHERE mc.source_id = a.source_id
              AND mc.category_code IN ({placeholders})
        )
    """, GLOBAL_CATEGORIES).fetchone()

    country_rows = conn.execute("""
        SELECT m.primary_country_code AS country,
               COUNT(*) AS total,
               SUM(CASE WHEN a.filter_decision='passed'   THEN 1 ELSE 0 END) AS passed,
               SUM(CASE WHEN a.filter_decision='rejected' THEN 1 ELSE 0 END) AS rejected,
               SUM(CASE WHEN a.duplicate_of IS NOT NULL   THEN 1 ELSE 0 END) AS dups
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        GROUP BY m.primary_country_code
        ORDER BY passed DESC
    """).fetchall()

    top_hits = conn.execute("""
        SELECT filter_reason, COUNT(*) AS cnt
        FROM articles_raw
        WHERE filter_decision='passed' AND filter_stage=2
        GROUP BY filter_reason
        ORDER BY cnt DESC
        LIMIT 20
    """).fetchall()

    excl_hits = conn.execute("""
        SELECT filter_reason, COUNT(*) AS cnt
        FROM articles_raw
        WHERE filter_decision='rejected' AND filter_reason LIKE 'excl:%'
        GROUP BY filter_reason
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()

    rejected_samples = conn.execute("""
        SELECT m.media_name, a.title, a.filter_reason
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.filter_decision='rejected'
        ORDER BY RANDOM()
        LIMIT 15
    """).fetchall()

    def pct(n, d):
        return f"{n/d*100:.1f}%" if d else "-"

    ts = datetime.now(timezone.utc).isoformat()
    lines = [
        "# 2단계 키워드 필터 결과 (v3 — 제목/본문 분리 점수)",
        "",
        f"_생성 시각: {ts}_",
        "",
        "## 점수 체계 (v3)",
        "",
        "| 신호 | 점수 |",
        "|---|---|",
        f"| 금융·ESG 키워드 — 제목 히트 | +{FINANCE_SCORE_TITLE} |",
        f"| 금융·ESG 키워드 — 본문 히트 | +{FINANCE_SCORE_BODY} |",
        f"| 국가 키워드 — 제목 히트 (국가당 1회) | +{COUNTRY_SCORE_TITLE} |",
        f"| 국가 키워드 — 본문 히트 (국가당 1회) | +{COUNTRY_SCORE_BODY} |",
        f"| 스포츠·연예 제외 키워드 히트 | {EXCLUSION_SCORE} |",
        f"| **통과 기준** | **≥ {PASS_THRESHOLD}** |",
        "",
        "## 전체 요약",
        "",
        "| 항목 | 건수 | 비율 |",
        "|---|---|---|",
        f"| 전체 수집 기사 | {tot['total']:,} | 100% |",
        f"| ✅ 통과 (passed) | {tot['passed']:,} | {pct(tot['passed'], tot['total'])} |",
        f"| ❌ 거부 (rejected) | {tot['rejected']:,} | {pct(tot['rejected'], tot['total'])} |",
        f"| ⏳ 미처리 (pending) | {tot['pending']:,} | {pct(tot['pending'], tot['total'])} |",
        f"| 🔁 중복 표시 | {dup_stats['total_dups']:,} | - |",
        "",
        "## 글로벌 매체 키워드 필터 상세",
        "",
        "| 항목 | 건수 | 비율 |",
        "|---|---|---|",
        f"| 글로벌 매체 기사 | {glob['total']:,} | 100% |",
        f"| ✅ 통과 | {glob['passed']:,} | {pct(glob['passed'], glob['total'])} |",
        f"| ❌ 거부 | {glob['rejected']:,} | {pct(glob['rejected'], glob['total'])} |",
        "",
        "## 국가별 수집·필터 현황",
        "",
        "| 국가 | 수집 | 통과 | 거부 | 중복 | 통과율 |",
        "|---|---|---|---|---|---|",
    ]
    for r in country_rows:
        lines.append(
            f"| {r['country']} | {r['total']:,} | {r['passed']:,} | "
            f"{r['rejected']:,} | {r['dups']:,} | {pct(r['passed'], r['total'])} |"
        )

    lines += [
        "",
        "## 통과 기사 주요 매칭 키워드 (TOP 20)",
        "",
        "| 매칭 근거 | 건수 |",
        "|---|---|",
    ]
    for r in top_hits:
        lines.append(f"| `{r['filter_reason']}` | {r['cnt']:,} |")

    lines += [
        "",
        "## 제외 키워드 히트 현황 (TOP 10)",
        "",
        "| 제외 근거 | 건수 |",
        "|---|---|",
    ]
    for r in excl_hits:
        lines.append(f"| `{r['filter_reason']}` | {r['cnt']:,} |")

    lines += [
        "",
        "## 거부 기사 샘플 (랜덤 15건)",
        "",
        "> 정상적으로 걸러졌는지 확인 → 놓친 금융 기사 있으면 키워드 추가",
        "",
        "| 매체 | 제목 | 거부 근거 |",
        "|---|---|---|",
    ]
    for r in rejected_samples:
        title  = (r["title"]  or "")[:70]
        reason = (r["filter_reason"] or "")[:40]
        lines.append(f"| {r['media_name']} | {title} | `{reason}` |")

    return "\n".join(lines) + "\n"
