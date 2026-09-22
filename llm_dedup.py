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

_PAIR_SYSTEM = (
    "당신은 뉴스 기사쌍 중복 판정기다. 각 pair_id를 다른 쌍과 독립적으로 판정한다. 두 제목이 "
    "독자에게 사실상 같은 뉴스 묶음이면 SAME이다. 다음은 SAME으로 판정한다: 같은 정책결정·지표발표·"
    "인사·거래·프로젝트의 반복 보도, 같은 중앙은행 회의의 전망·결정·직접 시장반응, 같은 날 같은 "
    "자산 움직임을 같은 원인으로 보도한 기사, 같은 수치를 반올림하거나 후속 기사에서 갱신한 경우. "
    "예: BOJ 금리인상 전망 ↔ 실제 금리인상 ↔ 그 결정 직후 엔화 반응은 SAME이다. 게시일 차이는 "
    "신디케이션·후속보도일 수 있으므로 그것만으로 DIFFERENT로 두지 마라. 서로 다른 기업·기관·지표·"
    "정책, 같은 기관의 별도 발표, 같은 국가·분야라는 공통점만 있는 기사는 DIFFERENT다. "
    "제목이 다른 표현을 쓰더라도 주체·핵심 사건·시기가 맞으면 SAME으로 판정하라. "
    "모든 입력 pair_id를 정확히 한 번씩 판정해야 한다. JSON만 출력한다: "
    "{\"decisions\": [{\"pair_id\": \"p1\", \"same\": true}, "
    "{\"pair_id\": \"p2\", \"same\": false}]}. 입력 순서를 지키고 ID를 만들거나 생략하지 마라."
)


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("dup_by_ai", "ALTER TABLE articles_raw ADD COLUMN dup_by_ai INTEGER DEFAULT 0"),
    ])


# ── 대표 선정·오묶음 가드 (2026-09-21) ────────────────────────────────────────
_STOP = set("the and for with from that this are was were has have had will its into over than "
            "after amid says said new news via not but out per his her their who what when how "
            "more than also can may could would year years first two".split())
_EVENT_GENERIC = {"rate", "rates", "hike", "cut", "decision", "policy", "market", "markets",
                  "bank", "banks", "central", "government", "economy", "economic", "financial"}

# 사건 '전' 예고·프리뷰 헤드라인 — 같은 그룹에 결과 기사가 있으면 대표에서 뒤로 민다.
_PREVIEW_RE = re.compile(
    r"\b(ahead of|expected to|what to expect|preview|poised to|girds?|braces?|brace for|"
    r"awaits?|looms?|counting the votes|to decide|will decide|likely to)\b|임박|앞두고|전망",
    re.I)


def _tokens(text: str | None) -> set:
    text = (text or "").lower()
    aliases = {
        "bank of japan": "boj", "hong kong monetary authority": "hkma",
        "bank of england": "boe", "reserve bank of india": "rbi",
        "federal reserve": "fed",
    }
    for full, short in aliases.items():
        text = text.replace(full, short)
    forms = {"rates": "rate", "raises": "raise", "raised": "raise", "raising": "raise",
             "hikes": "hike", "hiked": "hike", "cuts": "cut", "cutting": "cut"}
    return {forms.get(w, w) for w in re.findall(r"[a-z0-9]+", text)
            if len(w) > 2 and w not in _STOP}


def overlap(a: set, b: set) -> float:
    """겹침 계수(교집합/min) — 길이가 다른 헤드라인+요약 쌍에서도 안정적."""
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def pair_passes_guard(a: int, b: int, meta, threshold: float | None = None) -> bool:
    """LLM SAME 간선의 오묶음 가드.

    LLM 판정 뒤에도 제목에 공통 고유 주체가 있는지 확인한다. repair_existing이 넘기는
    명시적 임계값과 신규 pair 판정의 낮은 임계값은 서로 독립적으로 운용한다.
    """
    th = config.DEDUP_PAIR_MIN_OVERLAP if threshold is None else threshold
    ta = meta[a].get("title_tok") or meta[a]["tok"]
    tb = meta[b].get("title_tok") or meta[b]["tok"]
    sim = overlap(ta, tb)
    common = ta & tb
    anchors = common - _EVENT_GENERIC
    return sim >= th and bool(anchors)


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
            if pair_passes_guard(a, b, meta, th):
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


def _candidate_pairs(arts, meta) -> list[tuple[int, int, float]]:
    """국가 전체를 LLM에 넣지 않고 기사별 상위 유사 후보쌍만 만든다.

    낮은 1차 임계로 recall을 확보하되 기사당 이웃 수를 제한한다. 기존 AI 연결은
    유사도와 무관하게 반드시 후보에 넣어 재검토·보존 대상에서 빠지지 않게 한다.
    """
    ids = [int(a["article_id"]) for a in arts]
    selected: set[tuple[int, int]] = set()
    scored: dict[tuple[int, int], float] = {}
    exact_buckets: dict[frozenset, list[int]] = {}
    for aid in ids:
        exact_buckets.setdefault(frozenset(meta[aid]["candidate_tok"]), []).append(aid)
    for members in exact_buckets.values():
        if len(members) < 2:
            continue
        anchor = members[0]
        for aid in members[1:]:
            key = (min(anchor, aid), max(anchor, aid))
            selected.add(key); scored[key] = 1.0

    for i, aid in enumerate(ids):
        neighbors = []
        for bid in ids[i + 1:]:
            if meta[aid]["candidate_tok"] == meta[bid]["candidate_tok"]:
                continue
            sim = overlap(meta[aid]["candidate_tok"], meta[bid]["candidate_tok"])
            if sim >= config.DEDUP_CANDIDATE_OVERLAP:
                neighbors.append((sim, bid))
                scored[(min(aid, bid), max(aid, bid))] = sim
        for sim, bid in sorted(neighbors, reverse=True)[:config.DEDUP_MAX_NEIGHBORS]:
            selected.add((min(aid, bid), max(aid, bid)))

    # 한쪽에서 상위 이웃에 들지 못했어도 반대 방향 상위 후보가 될 수 있으므로 역방향도 본다.
    for j, bid in enumerate(ids):
        neighbors = []
        for aid in ids[:j]:
            if meta[aid]["candidate_tok"] == meta[bid]["candidate_tok"]:
                continue
            key = (min(aid, bid), max(aid, bid))
            sim = scored.get(key)
            if sim is None:
                sim = overlap(meta[aid]["candidate_tok"], meta[bid]["candidate_tok"])
            if sim >= config.DEDUP_CANDIDATE_OVERLAP:
                neighbors.append((sim, aid))
                scored[key] = sim
        for sim, aid in sorted(neighbors, reverse=True)[:config.DEDUP_MAX_NEIGHBORS]:
            selected.add((min(aid, bid), max(aid, bid)))

    for aid in ids:
        old_rep = meta[aid].get("old_rep")
        if old_rep in meta and old_rep != aid:
            key = (min(aid, old_rep), max(aid, old_rep))
            selected.add(key)
            scored.setdefault(key, overlap(meta[key[0]]["candidate_tok"], meta[key[1]]["candidate_tok"]))
    return sorted((a, b, scored.get((a, b), 0.0)) for a, b in selected)


def _pair_chunks(cc: str, pairs, meta):
    """후보쌍을 작은 독립 요청으로 직렬화한다."""
    out = []
    size = config.DEDUP_PAIRS_PER_REQUEST
    for n, start in enumerate(range(0, len(pairs), size)):
        chunk = pairs[start:start + size]
        lookup = {}
        blocks = []
        for i, (a, b, _sim) in enumerate(chunk, 1):
            pid = f"p{i}"
            lookup[pid] = (a, b)
            ma, mb = meta[a], meta[b]
            blocks.append(
                f"{pid}\nA {a} [{ma['pub'][:10]}] {ma['display_title'][:220]}\n"
                f"B {b} [{mb['pub'][:10]}] {mb['display_title'][:220]}"
            )
        cid = f"{cc}__{n:03d}"
        user = f"[국가:{cc}] 후보 기사쌍\n\n" + "\n\n".join(blocks)
        out.append((cid, lookup, user))
    return out


def _groups_from_edges(edges, meta, subject: str | None = None) -> list[list[int]]:
    """SAME 간선에서 대표와 직접 연결된 기사만 한 그룹으로 묶는다.

    A-B, B-C 판정만으로 A-C까지 자동 병합하는 전이 오류를 막는다. 연결요소마다 표시
    대표를 먼저 고른 뒤 그 대표와 직접 SAME인 기사만 붙이고, 남은 기사는 다시 처리한다.
    """
    adj: dict[int, set[int]] = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    groups = []
    pending = set(adj)
    while pending:
        start = min(pending)
        component, stack = set(), [start]
        while stack:
            aid = stack.pop()
            if aid in component or aid not in pending:
                continue
            component.add(aid)
            stack.extend(adj.get(aid, ()) & pending)
        rep = pick_rep(component, meta, subject)
        group = {rep} | (adj.get(rep, set()) & component)
        if len(group) >= 2:
            groups.append(sorted(group))
        pending.difference_update(group)
    return groups


def _valid_same_pairs(response, lookup):
    """응답을 검증해 SAME 후보쌍을 반환한다. 무효 응답은 None."""
    if not isinstance(response, dict):
        return None
    if "decisions" in response:
        decisions = response["decisions"]
        if not isinstance(decisions, list) or len(decisions) != len(lookup):
            return None
        seen, same = set(), []
        for item in decisions:
            if not isinstance(item, dict):
                return None
            pid, verdict = item.get("pair_id"), item.get("same")
            if pid not in lookup or pid in seen or type(verdict) is not bool:
                return None
            seen.add(pid)
            if verdict:
                same.append(lookup[pid])
        return same if seen == set(lookup) else None
    if "same_pair_ids" in response:
        ids = response["same_pair_ids"]
        if (not isinstance(ids, list)
                or any(not isinstance(x, str) or x not in lookup for x in ids)
                or len(ids) != len(set(ids))):
            return None
        return [lookup[x] for x in ids]

    # 이전 groups 응답 형식은 테스트·과도기 프로바이더 호환용으로만 수용한다.
    groups = _normalize_groups(response.get("groups"))
    if groups is None:
        return None
    allowed_ids = {aid for pair in lookup.values() for aid in pair}
    pairs = []
    seen = set()
    for group in groups:
        if (len(group) < 2 or any(type(aid) is not int or aid not in allowed_ids for aid in group)
                or len(group) != len(set(group))):
            return None
        group_set = set(group)
        matched = {tuple(sorted(pair)) for pair in lookup.values() if set(pair) <= group_set}
        if not matched or seen.intersection(matched):
            return None
        seen.update(matched)
        pairs.extend(matched)
    return pairs


def run_dedup(conn, provider: LLMProvider | None = None,
              days: int | None = 3, use_batch: bool | None = None,
              only_cc: str | None = None, dry_run: bool = False) -> dict:
    """노출 후보에서 유사 후보쌍을 만들고 작은 요청으로 LLM 근접중복을 판정한다.

    국가 전체 단일 요청을 없애 입력 초과·응답 한 건 오류가 국가 전체 recall을 무너뜨리지
    않게 한다. 무효 청크에 포함된 기존 AI 그룹만 보존하고 나머지는 정상 결과로 갱신한다.
    """
    ensure_columns(conn)
    provider = provider or get_provider("fast", use_batch=use_batch)

    date_clause, params = db.days_clause_now(days)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles_raw)")}
    title_en_sql = "a.title_en" if "title_en" in cols else "NULL"
    rows = conn.execute(
        f"""
        SELECT a.article_id, COALESCE(a.title_ko, a.title) AS t, a.title AS raw_title,
               {title_en_sql} AS title_en,
               a.summary_en AS s_en, a.ai_score, a.published_at,
               a.duplicate_of,
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

    requests, chunk_info, metas, chunks_by_cc = [], {}, {}, {}
    for cc, arts in by_cc.items():
        if len(arts) < 2:
            continue
        meta = {a["article_id"]: {
            "score": a["ai_score"] or 0, "pub": a["published_at"] or "",
            "titles": [a["t"], a["title_en"], a["raw_title"]], "media_cc": a["media_cc"],
            "display_title": a["title_en"] or a["t"] or a["raw_title"] or "",
            "old_rep": a["duplicate_of"],
            # 기존 오병합 그룹은 같은 요약이 복제될 수 있다. 그 요약을 다시 후보·가드에
            # 쓰면 오병합이 자기강화되므로 원문/영문 제목만 판정 근거로 사용한다.
            "candidate_tok": _tokens(" ".join(x or "" for x in
                                                (a["title_en"], a["raw_title"]))),
            "title_tok": _tokens(a["title_en"] or a["raw_title"] or ""),
            "tok": _tokens(a["title_en"] or a["raw_title"] or "")} for a in arts}
        pairs = _candidate_pairs(arts, meta)
        if not pairs:
            continue
        metas[cc] = meta
        chunks = _pair_chunks(cc, pairs, meta)
        chunks_by_cc[cc] = chunks
        for cid, lookup, user in chunks:
            chunk_info[cid] = (cc, lookup)
            requests.append((cid, _PAIR_SYSTEM, user, max(512, min(4096, len(lookup) * 80))))

    results = provider.complete_json_batch(requests) if requests else {}

    marked = reviewed = released = 0
    failed = 0
    collected: dict[str, list] = {}
    for cc, chunks in chunks_by_cc.items():
        meta = metas[cc]
        accepted_edges: set[tuple[int, int]] = set()
        invalid_ids: set[int] = set()
        valid_ids: set[int] = set()
        for cid, lookup, _user in chunks:
            pairs = _valid_same_pairs(results.get(cid), lookup)
            ids_here = {aid for pair in lookup.values() for aid in pair}
            if pairs is None:
                invalid_ids.update(ids_here)
                log.warning("AI 중복판정 응답 무효 — 청크=%s 기존 연결 보존, 기사=%d", cid, len(ids_here))
                continue
            valid_ids.update(ids_here)
            for a, b in pairs:
                if pair_passes_guard(a, b, meta):
                    accepted_edges.add((min(a, b), max(a, b)))
                else:
                    released += 1

        # 무효 청크가 기존 그룹 일부를 건드렸다면 그룹 전체를 보호해 끊어진 참조를 막는다.
        protected = set(invalid_ids)
        changed = True
        while changed:
            changed = False
            for aid, m in meta.items():
                rep = m.get("old_rep")
                if rep in meta and ((aid in protected) != (rep in protected)):
                    protected.update((aid, rep)); changed = True
        failed += len(protected)
        reviewed += len(valid_ids - protected)
        accepted_edges = {e for e in accepted_edges if not set(e).intersection(protected)}

        active_ids = {aid for aid in meta if aid not in protected}
        final_groups = _groups_from_edges(accepted_edges, meta, cc)

        if dry_run:
            # 무효 청크의 기존 그룹은 평가 결과에 보존 상태로 포함한다.
            old_edges = [(aid, m["old_rep"]) for aid, m in meta.items()
                         if aid in protected and m.get("old_rep") in protected]
            if old_edges:
                p2 = {aid: aid for aid in protected}
                def f2(x):
                    while p2[x] != x:
                        p2[x] = p2[p2[x]]; x = p2[x]
                    return x
                for a, b in old_edges: p2[f2(a)] = f2(b)
                old_comps = {}
                for aid in protected: old_comps.setdefault(f2(aid), []).append(aid)
                final_groups += [g for g in old_comps.values() if len(g) >= 2]
            collected[cc] = final_groups
            marked += sum(len(g) - 1 for g in final_groups)
            continue
        with conn:
            conn.executemany(
                "UPDATE articles_raw SET duplicate_of=NULL, dup_by_ai=0 "
                "WHERE article_id=? AND dup_by_ai=1", [(aid,) for aid in active_ids])
            for group in final_groups:
                rep = pick_rep(group, meta, cc)
                others = [aid for aid in group if aid != rep]
                conn.executemany(
                    "UPDATE articles_raw SET duplicate_of=?, dup_by_ai=1 WHERE article_id=?",
                    [(rep, aid) for aid in others])
                # 키워드 중복(dup_by_ai=0)이 강등된 기사를 가리키면 새 대표로 옮겨 체인을 막는다.
                conn.executemany(
                    "UPDATE articles_raw SET duplicate_of=? WHERE duplicate_of=? AND dup_by_ai=0",
                    [(rep, aid) for aid in others])
                marked += len(others)

    log.info("AI 중복판정 완료 — 국가=%d 요청=%d 검사=%d 실패보존=%d 중복마킹=%d건 가드해제=%d건",
             len(chunks_by_cc), len(requests), reviewed, failed, marked, released)
    out = {"countries": len(chunks_by_cc), "requests": len(requests), "marked": marked,
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
