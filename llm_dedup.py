"""
LLM 근접중복 판정 (llm_dedup.py, 2026-09-16)

토큰 유사도(keyword_filter의 duplicate_of, 제목 0.75)로 못 잡는 '같은 사건 다른 표현'
근접중복을 LLM 의미판단으로 묶는다.
예: "Rupee falls 30 paise to 95.84" vs "Rupee slumps 38 paise to 95.92" (같은 날 루피 하락).

범위: 노출 후보(최근 N일, ai_score 있고 아직 비중복)만 — 국가별 전체 후보를 한 요청에서 비교한다.
출력: 각 그룹의 대표를 정해 나머지는 duplicate_of=대표id + dup_by_ai=1.
재실행 안전: 유효 응답을 받은 국가의 검사 대상만 원자적으로 교체한다.
표시 피드(국가·모니터링)는 이미 duplicate_of IS NULL 로 거르므로 export 변경 불필요.

신뢰성 보강(2026-09-21):
 · 대표 선정 = (예고·프리뷰가 아닌 기사 → ai_score 최고 → 게시 최신 → id 작은 것).
   예전엔 점수 동점이면 가장 오래된 id가 대표가 돼, '결정 임박' 프리뷰가 대표로 뜨고
   실제 결정 기사(NYT·FT 등 11건)가 자식으로 숨었다.
 · 오묶음 가드 — LLM이 묶은 그룹을 요약·제목 토큰 겹침으로 검증해, 서로 이어지지 않는
   기사는 그룹에서 풀어 그대로 노출한다(잘못 묶으면 별개 뉴스가 화면에서 사라진다).
 · repair_existing() — 이미 저장된 그룹에 같은 규칙을 소급(LLM 비용 0, 기본 dry-run).
"""
from __future__ import annotations

import logging
import re
from collections import Counter

import config
import db
from llm_provider import LLMProvider, get_provider

log = logging.getLogger("llm_dedup")

_SYSTEM = (
    "당신은 뉴스 근접중복 판정기다. 같은 국가 헤드라인 목록에서 '같은 하나의 사건'을 보도한 "
    "기사들만 묶는다.\n"
    "★ 비대칭 원칙: 잘못 묶으면 별개의 뉴스가 독자 화면에서 사라진다. 놓친 중복은 비슷한 기사가 "
    "조금 더 보일 뿐이라 비용이 훨씬 작다. 그러므로 확신이 없으면 묶지 마라.\n"
    "묶는 경우 — 같은 주체(기관·기업·인물·정책)의 같은 사건·발표·결정이라, 헤드라인만 읽어도 "
    "'같은 뉴스'임이 분명할 때:\n"
    "  · 같은 발표·결정·거래·인사를 여러 매체가 반복 보도(수치·표현·매체만 다름). 예: 같은 "
    "중앙은행의 '한 번의' 금리결정을 여러 매체가 보도.\n"
    "  · 그 결정의 시장 반응·회견·분석·정치권 반응 — 헤드라인에 그 결정이 명시된 경우만.\n"
    "묶지 않는 경우:\n"
    "  · 같은 나라·같은 분야(금리·환율·수출·투자·은행·규제)라는 이유만으로는 묶지 마라.\n"
    "  · 서로 다른 지표·기업·기관·정책은 각각 둔다. 실제 오판정 예: 「홍콩 기준금리 인상」 ↔ "
    "「홍콩 5개년 계획」, 「KB은행 순이익 급감」 ↔ 「업계 여신 성장」, 「캄보디아 양허차입 급증」 ↔ "
    "「美 기업 사절단 방문」, 「재무장관 교체」 ↔ 「은행 실적」, 「IMF의 호주 금리 전망」 ↔ "
    "「호주 정부 부채 만기」.\n"
    "  · 다른 날짜의 별개 결정, 다른 기업·종목의 거래, 같은 지표의 다른 날 수치.\n"
    "그룹마다 묶인 사건을 15자 안팎의 한 줄(event)로 적어라. 한 줄로 못 쓰면 하나의 사건이 아니니 "
    "묶지 마라.\n"
    "JSON만 출력(설명 없이): {\"groups\": [{\"event\": \"...\", \"ids\": [id, id, ...]}, ...]}  "
    "— 2건 이상 묶인 그룹만 넣고, 단독 기사는 생략."
)


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("dup_by_ai", "ALTER TABLE articles_raw ADD COLUMN dup_by_ai INTEGER DEFAULT 0"),
    ])


# ── 대표 선정·오묶음 가드 (2026-09-21) ────────────────────────────────────────
_STOP = set("the and for with from that this are was were has have had will its into over than "
            "after amid says said new news via not but out per his her their who what when how "
            "more than also can may could would year years first two".split())

# 사건 '전' 예고·프리뷰 헤드라인 — 같은 그룹에 결과 기사가 있으면 대표에서 뒤로 민다.
_PREVIEW_RE = re.compile(
    r"\b(ahead of|expected to|what to expect|preview|poised to|girds?|braces?|brace for|"
    r"awaits?|looms?|counting the votes|to decide|will decide|likely to)\b|임박|앞두고|전망",
    re.I)


def _tokens(text: str | None) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP}


def overlap(a: set, b: set) -> float:
    """겹침 계수(교집합/min) — 길이가 다른 헤드라인+요약 쌍에서도 안정적."""
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def is_preview(*titles: str | None) -> bool:
    return any(t and _PREVIEW_RE.search(t) for t in titles)


def pick_rep(group, meta, subject: str | None = None) -> int:
    """대표 = 예고/프리뷰 아님 → (subject 국가의) 현지언론 → ai_score 최고 → 게시 최신 → id 작은 것.
    meta[aid] = {"score", "pub", "titles", "media_cc"}. 시간순 사건에서 결정 기사가 대표가 되게 하고,
    국가 탭이 매체국적 기준이라 대표가 그 나라 현지언론이어야 탭에서 사라지지 않는다
    (예: 인니 재무장관 교체 그룹의 대표가 Reuters가 되면 인도네시아 탭에서 통째로 빠진다)."""
    return max(group, key=lambda aid: (
        not is_preview(*meta[aid]["titles"]),
        bool(subject) and meta[aid].get("media_cc") == subject,
        meta[aid]["score"] or 0,
        meta[aid]["pub"] or "",
        -aid,
    ))


def split_by_overlap(group, meta, threshold: float | None = None) -> list[list[int]]:
    """LLM이 묶은 그룹을 겹침 그래프의 연결요소로 쪼갠다(간선: 요약·제목 토큰 겹침 ≥ 임계).
    서로 이어지지 않는 기사는 다른 요소가 되어, 단독이면 그룹에서 풀린다(그대로 노출)."""
    th = config.DEDUP_MIN_OVERLAP if threshold is None else threshold
    ids = list(group)
    parent = {a: a for a in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if overlap(meta[a]["tok"], meta[b]["tok"]) >= th:
                parent[find(a)] = find(b)
    comps: dict[int, list[int]] = {}
    for a in ids:
        comps.setdefault(find(a), []).append(a)
    return list(comps.values())


def _normalize_groups(groups):
    """응답 groups → [[id,...],...]. 옛 형식([[id,id]])과 새 형식([{"event","ids"}]) 모두 수용.
    형식이 어긋나면 None(무효)."""
    if not isinstance(groups, list):
        return None
    out = []
    for g in groups:
        if isinstance(g, dict):
            g = g.get("ids")
        if not isinstance(g, list):
            return None
        out.append(g)
    return out


def run_dedup(conn, provider: LLMProvider | None = None,
              days: int | None = 3, use_batch: bool | None = None,
              only_cc: str | None = None, dry_run: bool = False) -> dict:
    """노출 후보를 국가별로 LLM 근접중복 판정. only_cc 지정 시 그 국가만(디버그).
    dry_run=True면 LLM은 호출하되 DB는 바꾸지 않고, 가드까지 통과한 최종 그룹을 결과의
    "groups"({국가: [[id,...],...]})로 돌려준다 — 프롬프트·가드를 실데이터로 평가할 때 쓴다
    (eval/eval_dedup_guard.py --live)."""
    ensure_columns(conn)
    provider = provider or get_provider("fast", use_batch=use_batch)

    date_clause, params = db.days_clause_now(days)
    rows = conn.execute(
        f"""
        SELECT a.article_id, COALESCE(a.title_ko, a.title) AS t, a.title AS raw_title,
               a.summary_en AS s_en, a.ai_score, a.published_at,
               m.primary_country_code AS media_cc,
               COALESCE(NULLIF(a.primary_country, ''), m.primary_country_code) AS cc
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.ai_score IS NOT NULL
          AND (a.duplicate_of IS NULL OR a.dup_by_ai = 1)
          AND a.ai_model LIKE '%:%'{date_clause}
        ORDER BY a.ai_score DESC, a.published_at DESC, a.article_id ASC
        """,
        params,
    ).fetchall()

    by_cc: dict[str, list] = {}
    for r in rows:
        cc = r["cc"] or "??"
        if only_cc and cc != only_cc:
            continue
        by_cc.setdefault(cc, []).append(r)

    requests, ctx, metas = [], {}, {}
    oversized = 0
    for cc, arts in by_cc.items():
        if len(arts) < 2:
            continue
        lines = "\n".join(f"{a['article_id']}: [{(a['published_at'] or '')[:10]}] {(a['t'] or '')[:160]}" for a in arts)
        # 광범위한 백필에서 컨텍스트를 넘기지 않는다. 일부만 잘라 검사하지 않고
        # 기존 결과를 보존하며 명시적으로 실패 집계한다(더 좁은 days로 재실행).
        if len(lines) > 80000:
            oversized += len(arts)
            log.warning("AI 중복판정 입력 초과 — 국가=%s 후보=%d, days를 줄여 재실행 필요", cc, len(arts))
            continue
        cid = str(cc)
        requests.append((cid, _SYSTEM, f"[국가:{cc}] 헤드라인 목록:\n{lines}", min(16000, max(1024, len(arts) * 32))))
        ctx[cid] = {a["article_id"]: (a["ai_score"] or 0) for a in arts}
        metas[cid] = {a["article_id"]: {
            "score": a["ai_score"] or 0, "pub": a["published_at"] or "",
            "titles": [a["t"], a["raw_title"]], "media_cc": a["media_cc"],
            "tok": _tokens((a["raw_title"] or "") + " " + (a["s_en"] or ""))} for a in arts}

    results = provider.complete_json_batch(requests) if requests else {}

    marked = reviewed = released = 0
    failed = oversized
    collected: dict[str, list] = {}
    for cid, scores in ctx.items():
        res = results.get(cid)
        groups = _normalize_groups(res.get("groups")) if isinstance(res, dict) else None
        # 누락/파싱 실패와 명시적인 groups=[]를 구분한다. 중복 ID/겹치는 그룹은
        # 순환 연결을 만들 수 있으므로 국가 응답 전체를 거부하고 기존 결과를 유지한다.
        seen = set()
        valid = isinstance(groups, list)
        if valid:
            for group in groups:
                if (not isinstance(group, list) or len(group) < 2
                        or any(type(aid) is not int or aid not in scores for aid in group)
                        or len(set(group)) != len(group) or seen.intersection(group)):
                    valid = False
                    break
                seen.update(group)
        if not valid:
            failed += len(scores)
            log.warning("AI 중복판정 응답 무효 — 국가=%s 기존 결과 보존, 후보=%d", cid, len(scores))
            continue
        # API 호출 중에는 기존 연결을 유지한다. 결과 검증 후에만 같은 트랜잭션에서 교체.
        meta = metas[cid]
        # 오묶음 가드: LLM 그룹을 겹침 그래프의 연결요소로 쪼개 2건 이상인 요소만 묶는다.
        final_groups = []
        for group in groups:
            comps = split_by_overlap(group, meta)
            kept = [c for c in comps if len(c) >= 2]
            released += len(group) - sum(len(c) for c in kept)
            final_groups.extend(kept)
        if dry_run:
            collected[cid] = final_groups
            marked += sum(len(g) - 1 for g in final_groups)
            reviewed += len(scores)
            continue
        with conn:
            conn.executemany(
                "UPDATE articles_raw SET duplicate_of=NULL, dup_by_ai=0 "
                "WHERE article_id=? AND dup_by_ai=1", [(aid,) for aid in scores])
            for group in final_groups:
                rep = pick_rep(group, meta, cid)
                others = [aid for aid in group if aid != rep]
                conn.executemany(
                    "UPDATE articles_raw SET duplicate_of=?, dup_by_ai=1 WHERE article_id=?",
                    [(rep, aid) for aid in others])
                # 키워드 중복(dup_by_ai=0)이 강등된 기사를 가리키면 새 대표로 옮겨 체인을 막는다.
                conn.executemany(
                    "UPDATE articles_raw SET duplicate_of=? WHERE duplicate_of=? AND dup_by_ai=0",
                    [(rep, aid) for aid in others])
                marked += len(others)
        reviewed += len(scores)

    log.info("AI 중복판정 완료 — 국가=%d 검사=%d 실패보존=%d 중복마킹=%d건 가드해제=%d건",
             len(requests), reviewed, failed, marked, released)
    out = {"countries": len(requests), "marked": marked,
           "reviewed": reviewed, "failed": failed, "released": released}
    if dry_run:
        out["groups"] = collected
    return out


def repair_existing(conn, threshold: float | None = None, apply: bool = False,
                    sample: int = 12) -> dict:
    """이미 저장된 AI 그룹(dup_by_ai=1)에 가드·대표선정 규칙을 소급한다 — LLM 비용 0.

    ① 겹침 가드로 서로 이어지지 않는 기사는 그룹에서 풀어 화면에 되돌린다.
    ② 남은 그룹은 대표를 새 규칙(프리뷰 아님 → 점수 → 게시 최신)으로 다시 뽑아,
       '결정 임박' 프리뷰가 대표로 남고 실제 결정 기사가 숨는 문제를 바로잡는다.
    ③ 대표가 바뀌면 키워드 중복(dup_by_ai=0)이 가리키던 대상도 새 대표로 옮긴다.
    apply=False(기본)면 DB를 바꾸지 않고 집계·표본만 돌려준다(dry-run)."""
    rows = conn.execute(
        "SELECT article_id, duplicate_of FROM articles_raw "
        "WHERE dup_by_ai = 1 AND duplicate_of IS NOT NULL").fetchall()
    groups: dict[int, list[int]] = {}
    for r in rows:
        groups.setdefault(int(r["duplicate_of"]), []).append(int(r["article_id"]))
    ids = set(groups) | {c for ch in groups.values() for c in ch}
    meta: dict[int, dict] = {}
    titles: dict[int, str] = {}
    for i in range(0, len(ids), 500):
        chunk = list(ids)[i:i + 500]
        q = ",".join("?" * len(chunk))
        for a in conn.execute(
                f"SELECT a.article_id, a.title, a.title_ko, a.summary_en, a.ai_score, a.published_at, "
                f"m.primary_country_code AS media_cc, "
                f"COALESCE(NULLIF(a.primary_country, ''), m.primary_country_code) AS cc "
                f"FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id "
                f"WHERE a.article_id IN ({q})", chunk):
            aid = int(a["article_id"])
            meta[aid] = {"score": a["ai_score"] or 0, "pub": a["published_at"] or "",
                         "titles": [a["title_ko"], a["title"]], "media_cc": a["media_cc"],
                         "cc": a["cc"],
                         "tok": _tokens((a["title"] or "") + " " + (a["summary_en"] or ""))}
            titles[aid] = (a["title"] or "")[:90]

    updates: list[tuple[str, tuple]] = []     # (sql, params)
    stats = {"groups": 0, "children": 0, "released": 0, "rep_changed": 0,
             "keyword_repointed": 0, "apply": bool(apply)}
    rep_changes: list[str] = []
    released_examples: list[str] = []
    for rep, children in groups.items():
        members = [rep] + [c for c in children if c in meta]
        if rep not in meta or len(members) < 2:
            continue
        stats["groups"] += 1
        stats["children"] += len(members) - 1
        subject = Counter(meta[m]["cc"] for m in members).most_common(1)[0][0]
        new_root_of: dict[int, int] = {}        # 옛 멤버 → 새 대표(또는 자기 자신)
        for comp in split_by_overlap(members, meta, threshold):
            if len(comp) < 2:
                aid = comp[0]
                new_root_of[aid] = aid
                if aid != rep:                  # 옛 자식이 풀려남
                    stats["released"] += 1
                    updates.append(("UPDATE articles_raw SET duplicate_of=NULL, dup_by_ai=0 "
                                    "WHERE article_id=?", (aid,)))
                    if len(released_examples) < sample:
                        released_examples.append(f"{titles[aid]}  ⟵ 대표였던 '{titles[rep]}'에서 분리")
                continue
            new_rep = pick_rep(comp, meta, subject)
            for aid in comp:
                new_root_of[aid] = new_rep
            if new_rep != rep and rep in comp:
                stats["rep_changed"] += 1
                if len(rep_changes) < sample:
                    rep_changes.append(f"대표 교체: '{titles[rep]}' → '{titles[new_rep]}'")
            elif rep not in comp:               # 옛 대표가 다른 요소로 갈라짐 — 새 그룹 탄생
                stats["rep_changed"] += 1
            updates.append(("UPDATE articles_raw SET duplicate_of=NULL, dup_by_ai=0 "
                            "WHERE article_id=?", (new_rep,)))
            for aid in comp:
                if aid != new_rep:
                    updates.append(("UPDATE articles_raw SET duplicate_of=?, dup_by_ai=1 "
                                    "WHERE article_id=?", (new_rep, aid)))
        # 키워드 중복이 옛 멤버를 가리키면 그 멤버의 새 대표로 옮긴다(대표 자신이 그대로면 변화 없음).
        for old, root in new_root_of.items():
            if root != old:
                n = conn.execute("SELECT COUNT(*) FROM articles_raw WHERE duplicate_of=? "
                                 "AND dup_by_ai=0", (old,)).fetchone()[0]
                if n:
                    stats["keyword_repointed"] += n
                    updates.append(("UPDATE articles_raw SET duplicate_of=? "
                                    "WHERE duplicate_of=? AND dup_by_ai=0", (root, old)))
    stats["rep_change_examples"] = rep_changes
    stats["released_examples"] = released_examples
    if apply and updates:
        with conn:
            for sql, params in updates:
                conn.execute(sql, params)
    log.info("AI 그룹 소급 보정%s — 그룹=%d 자식=%d 풀림=%d 대표교체=%d 키워드중복이동=%d",
             "" if apply else "(dry-run)", stats["groups"], stats["children"],
             stats["released"], stats["rep_changed"], stats["keyword_repointed"])
    return stats
