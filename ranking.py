"""
ranking.py — 표시용 복합 랭킹 점수(rank_score)

문제(2026-09 실측, news.db 최근 14일):
  llm_ranker 의 ai_score(0~100)는 LLM 절대채점이라 값이 몇 개로 양자화된다.
  ACTIVE(>=55) 460건 중 459건이 60~64 구간, ACTIVE 구간 고유 점수값 단 5개.
  → "ai_score 순" 정렬은 사실상 전부 동점 → Top Issues·피드 순서가 무작위.

해결:
  ai_score 는 ACTIVE/WATCH 게이트와 국가 온도(mood)에만 그대로 쓰고,
  **표시용 정렬은 이미 DB에 있는 신호를 조합한 rank_score 로** 한다.
  결정적 계산이라 LLM 비용 0. 가장 강한 신호는 다매체 커버리지
  (duplicate_of 로 세는 형제 기사 수 = '몇 개 매체가 다뤘나') — 지금 정렬에 안 쓰이던 것.

  rank_score = ai_score
             + W_CLUSTER * log2(1 + 다매체수)
             + 매체 tier 보너스 + 최신성 보너스 + 진출국 보너스
             + 이벤트유형 보너스 + 한국계금융 보너스 + 인사이동 보너스

가중치는 아래 상수. 2026-09-03 튜닝(근거):
  - 정답 B: daily_highlights 이력(LLM이 고른 '오늘의 핵심') 46건 중 기사 매칭 30건, 6일치.
    기준선(ai_score순) R@10 0.323 → 튜닝 0.423. 상위 구간 일관 신호: tier↑(1.5~2×), 다매체 6→3, 이벤트 2, 최신성 1×.
  - 정답 A: eval/eval_set_v2.jsonl 사람 등급(149건) — tier0 평균등급 2.0 > tier1 1.31 > tier2 0.97 (tier 방향·크기 뒷받침).
  - 진출국 보너스는 정답 B에서 측정 불가(후보 전부 진출국) → 원칙값 3 유지. 한국계금융·인사는 표본 부족으로 미튜닝.
  - 표본이 작아(30건) 극단값 대신 안정 구간 중앙값 채택. 재튜닝: _backup_rank/tune_results.json 참고, 라벨 늘려 재실행.
"""
from __future__ import annotations

import datetime
import math

import config

# ── 가중치 (튜닝 대상: 여기만 고치면 전체 반영) ──────────────────────
W_CLUSTER       = 3.0                          # × log2(1 + 다매체 형제 수)  [튜닝: 6→3]
TIER_BONUS      = {0: 9.0, 1: 6.0, 2: 3.0}     # media_sources.tier (낮을수록 고품질)  [튜닝: 1.5×]
RECENCY_BONUS   = ((0, 3.0), (1, 1.5), (2, 0.5))  # (일수 이하, 보너스) — 위→아래 첫 매칭
PRESENCE_BONUS  = 3.0                          # KB 진출국 소스
EVENT_BONUS     = 2.0                          # 규제/거래·투자/사건사고 이벤트
KOREAN_FI_BONUS = 2.0                          # 한국계 금융기관 관련
PERSONNEL_BONUS = 1.0                          # 인사 이동


def cluster_sizes(conn) -> dict[int, int]:
    """대표기사 article_id → 그 기사를 duplicate_of 로 가리키는 형제(중복) 수.
    한 export 실행에서 한 번만 계산해 order()에 cluster_map 으로 넘겨 재사용 권장."""
    out: dict[int, int] = {}
    for rep, n in conn.execute(
        "SELECT duplicate_of, COUNT(*) FROM articles_raw "
        "WHERE duplicate_of IS NOT NULL GROUP BY duplicate_of"
    ):
        if rep is not None:
            out[int(rep)] = int(n)
    return out


def _keys(row):
    try:
        return set(row.keys())               # sqlite3.Row
    except AttributeError:
        return set(row)                      # dict


def _get(row, key, default=None):
    if key not in _keys(row):
        return default
    v = row[key]
    return default if v is None else v


def _days_ago(published_at, now: datetime.date) -> int:
    try:
        d = datetime.date.fromisoformat(str(published_at)[:10])
        return (now - d).days
    except Exception:
        return 99


def rank_score(row, cluster: int = 0, now: datetime.date | None = None) -> float:
    """단일 기사의 복합 랭킹 점수.

    row 에서 읽는 키(없으면 해당 보너스 0):
      ai_score(사실상 필수), published_at, tier, cc 또는 primary_country_code,
      event_type, korean_fi, personnel_move.
    cluster: cluster_sizes()[article_id] 값(형제 수, 없으면 0).
    """
    now = now or datetime.date.today()
    s = float(_get(row, "ai_score", 0) or 0)

    if cluster and cluster > 0:
        s += W_CLUSTER * math.log2(1 + cluster)

    tier = _get(row, "tier", None)
    if tier is not None:
        s += TIER_BONUS.get(int(tier), 0.0)

    d = _days_ago(_get(row, "published_at", ""), now)
    for lim, bonus in RECENCY_BONUS:
        if d <= lim:
            s += bonus
            break

    cc = _get(row, "cc", None) or _get(row, "primary_country_code", None)
    if cc and config.is_presence(cc):
        s += PRESENCE_BONUS

    if _get(row, "event_type", ""):
        s += EVENT_BONUS
    if _get(row, "korean_fi", ""):
        s += KOREAN_FI_BONUS
    if _get(row, "personnel_move", 0):
        s += PERSONNEL_BONUS

    return round(s, 2)


def order(conn, rows, now: datetime.date | None = None, cluster_map: dict | None = None):
    """rows 를 rank_score 내림차순으로 안정 정렬해 반환.
    각 row 는 article_id 를 포함해야 다매체 신호가 반영된다(없으면 cluster=0)."""
    cm = cluster_map if cluster_map is not None else cluster_sizes(conn)
    now = now or datetime.date.today()

    def key(r):
        aid = _get(r, "article_id", None)
        cl = cm.get(int(aid), 0) if aid is not None else 0
        return rank_score(r, cl, now)

    return sorted(rows, key=key, reverse=True)
