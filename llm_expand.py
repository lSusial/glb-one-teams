"""
모달 전용 긴 요약 (llm_expand.py)

카드용 짧은 요약(summary_ko/summary_en, 2~4문장)과 별개로, 기사 상세 모달에서
보여줄 긴 요약(expanded_summary/_en)을 생성한다.
분량 변경 이력:
  기존 10~20줄 → 2026-09-08 15~35줄로 확대 → 같은 날 **약 2/3로 축소**(아래가 현재값).
  실측(노출 99건) 중앙값 989자·6문단이 모달에서 과하게 길다는 피드백 → 목표 660자·4문단.

비용 관리: 실제로 화면에 노출되는 기사(ACTIVE, ai_score>=임계, 중복 아님)에만
생성 — 전량 생성 금지. 증분(expanded_summary IS NULL인 것만), Haiku + Batches.

다출처 종합: llm_ranker._cluster_sources()가 만드는 duplicate_of 클러스터(같은
사건, 다른 매체)를 그대로 재사용해 함께 프롬프트에 넣는다 — 단일 기사 패러프레이즈가
아니라 여러 출처를 종합한 문단으로 만들어 저작권 리스크를 완화한다.
"""
from __future__ import annotations

import logging

import config
import db
import numeric_guard
from llm_provider import LLMProvider, get_provider
from llm_ranker import _cluster_sources, _published_line, _source_snippet

log = logging.getLogger("llm_expand")

_SYS = (
    "You are a news desk editor at KB Financial Group writing a DETAILED briefing for a modal "
    "popup that readers open when they want more depth than the card's short summary.\n\n"
    "If multiple numbered sources are given below (\"Source 1:\", \"Source 2:\", ...), synthesize "
    "them into ONE independent account combining facts from all of them — do not paraphrase a "
    "single source or copy its sentence structure. If only one source is given, write from that "
    "source alone.\n\n"
    "You MUST always output the JSON below with a non-empty summary, no matter how little source "
    "text is given — even a bare headline plus a one-sentence snippet is enough to write a short "
    "summary from. NEVER refuse, ask for more information, or claim the source content is missing "
    "or insufficient; that is not an acceptable response. Do the best job possible with whatever "
    "text is provided, and let the LENGTH instruction below (not this note) decide how long that is. "
    "Output ONLY this JSON, nothing else:\n"
    '{"expanded_summary_en": "...", "expanded_summary_ko": "..."}\n\n'
    "Length: aim for a CONSISTENT 3-4 short paragraphs (roughly 10-16 lines when displayed, "
    "~500-700 Korean characters) — keep the length steady across articles so summaries do not "
    "vary wildly in size. This is a modal the reader opens for a bit more depth than the card — "
    "not a full article. Separate paragraphs with "
    "a blank line (\\n\\n). Before writing, mine the source text(s) for everything usable: exact "
    "figures and dates, named people/institutions and their stated positions, direct or paraphrased "
    "quotes, comparisons to prior periods or similar events, dissenting or alternative views if "
    "present, and any process/procedural detail (how a decision was reached, what happens next, "
    "who decides). Pick the most load-bearing of that material and leave the rest out — this is a "
    "tight briefing, so prefer one dense paragraph over two thin ones. Cover, in this order: "
    "(1) what happened — the core facts; (2) background/context — why now, what led here; "
    "(3) concrete numbers/details, key named parties, and any quotes or stated positions; "
    "(4) likely knock-on effects or outlook. If a source is genuinely just a headline plus a "
    "one-sentence snippet with nothing more to extract, write however many paragraphs (even just "
    "one or two) that snippet actually supports — a short, honest summary from thin material is "
    "correct and expected in that case, not a failure. Never invent facts to hit the target, and "
    "never restate the same fact reworded across multiple paragraphs just to reach the target "
    "length — if you notice a new paragraph would only rephrase a point already made, stop there "
    "instead of adding it.\n"
    "Do NOT add a KB-implication section — that is handled elsewhere.\n"
    "Stay strictly within the facts given in the source(s); never invent numbers, quotes, or "
    "events not present in the source text.\n"
    "Preserve trading-session qualifiers in both languages: first-session close means 오전장 마감, "
    "not the full-day close. Label figures from different sessions separately.\n"
    "Tone: dry newspaper-desk, facts first. Ban AI-ish filler and clichés (\"in a significant "
    "move\", \"marks a pivotal moment\", \"underscores\", \"delve\", \"realm\", \"tapestry\", "
    "\"game-changer\", \"in today's fast-paced/evolving landscape\", \"in conclusion\"), no "
    "meta-commentary about the article itself, no stacked hedging qualifiers.\n"
    "expanded_summary_en in English; expanded_summary_ko is a natural Korean rendering of the "
    "SAME content (not a separate re-summary) — '~다' 체, 신문 기사 톤."
)

# 소스가 헤드라인+한두 문장뿐인 얇은 경우(페이월로 스니펫만 남은 Bloomberg/WSJ 등) 전용.
# 위 _SYS의 "7~8문단 목표"를 그대로 두면 모델이 같은 얘기를 문단마다 다르게 돌려 말해
# 억지로 채운다(실측 확인) — 분량 목표 자체를 없애고 "쓸 수 있는 만큼만" 쓰게 한다.
_SYS_THIN = (
    "Preserve trading-session qualifiers: first-session close = 오전장 마감, intraday = 장중, "
    "full-day close = 종가. Never drop a session qualifier. "
    "You are a news desk editor at KB Financial Group writing a briefing for a modal popup. "
    "The source material below is very thin (just a headline and a short snippet, likely because "
    "the full article is paywalled).\n\n"
    "Write however many short paragraphs (typically 1-3) that thin material actually supports — "
    "a short, honest summary is correct here, not a failure. Do NOT pad, speculate, or restate the "
    "same fact reworded across multiple paragraphs to appear longer; never invent facts, numbers, "
    "or quotes not present in the source. If multiple numbered sources are given, synthesize them "
    "into one account instead of paraphrasing a single one.\n\n"
    "You MUST always output the JSON below with a non-empty summary — never refuse or ask for more "
    "information. Output ONLY this JSON, nothing else:\n"
    '{"expanded_summary_en": "...", "expanded_summary_ko": "..."}\n\n'
    "Do NOT add a KB-implication section — that is handled elsewhere.\n"
    "Tone: dry newspaper-desk, facts first. Ban AI-ish filler and clichés (\"in a significant "
    "move\", \"marks a pivotal moment\", \"underscores\", \"delve\", \"realm\", \"tapestry\", "
    "\"game-changer\", \"in today's fast-paced/evolving landscape\", \"in conclusion\"), no "
    "meta-commentary about the article itself, no stacked hedging qualifiers.\n"
    "expanded_summary_en in English; expanded_summary_ko is a natural Korean rendering of the "
    "SAME content (not a separate re-summary) — '~다' 체, 신문 기사 톤."
)

# 긴 요약 한국어 금액 재요청 — 검증 통과한 영문만 주고 번역만 다시 받는다. 같은 소스로
# 전체를 재생성하면 매번 같은 crore 10배 오환산이 반복됐다(2026-10-21 21건 매일 거부).
_SYS_KO_RETRY = (
    "Translate the English briefing below into natural Korean ('~다' 체, 신문 기사 톤), keeping "
    "every paragraph and fact. A previous Korean version converted money amounts with the wrong "
    "unit scale (e.g. crore/billion → 억/조 off by 10x). For every amount, copy the exact Korean "
    "form from the provided hint list instead of converting units yourself. "
    'Output ONLY this JSON: {"expanded_summary_ko": "..."}'
)

_THIN_SOURCE_MAXLEN = 300  # 이 길이(자) 이하면 _SYS_THIN 사용


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("expanded_summary",    "ALTER TABLE articles_raw ADD COLUMN expanded_summary    TEXT"),
        ("expanded_summary_en", "ALTER TABLE articles_raw ADD COLUMN expanded_summary_en TEXT"),
        ("publisher_name",      "ALTER TABLE articles_raw ADD COLUMN publisher_name      TEXT"),
    ])


def run_expand(conn, provider: LLMProvider | None = None,
                limit: int | None = None, use_batch: bool | None = None,
                redo_days: int | None = None) -> dict:
    """노출 기사(ACTIVE: ai_score>=임계, 중복 아님)에만 모달용 긴 요약을 생성.
    기본은 증분 — expanded_summary IS NULL 인 것만 대상(이미 있으면 스킵).
    전일+당일 기사를 먼저 처리하고, 남은 한도로 오래된 기사를 채운다.

    redo_days=N 이면 최근 N일 게시 기사는 이미 요약이 있어도 다시 생성한다.
    프롬프트(길이·구성)를 바꾼 뒤 기존분에 소급 적용할 때만 사용 — 재생성분만큼
    LLM 비용이 그대로 추가되므로 창을 좁게 잡을 것.
    """
    ensure_columns(conn)
    provider = provider or get_provider("fast", use_batch=use_batch)   # 저비용(Haiku)
    limit = limit or 200

    if redo_days:
        # 재생성 모드: 창 안의 기사는 요약 유무와 무관하게 대상
        date_clause, date_params = db.days_clause_now(redo_days, alias="a")
        have_clause = ""
    else:
        date_clause, date_params = "", []
        have_clause = " AND a.expanded_summary IS NULL"

    rows = conn.execute(
        f"""SELECT a.article_id, a.title, a.summary, a.full_text, a.link, a.published_at,
                   m.primary_country_code AS cc, m.media_name
            FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
            WHERE a.ai_score >= ? AND a.duplicate_of IS NULL
              {have_clause}{date_clause}
            ORDER BY CASE WHEN substr(a.published_at,1,10) >= date(
                (SELECT MAX(substr(published_at,1,10)) FROM articles_raw), '-1 day')
                THEN 0 ELSE 1 END,
                a.ai_score DESC, a.published_at DESC, a.article_id ASC
            LIMIT ?""",
        (config.AI_SCORE_ACTIVE_THRESHOLD, *date_params, limit),
    ).fetchall()

    stats = dict(total=len(rows), written=0, synthesized=0)

    requests, row_by_id = [], {}
    for i, r in enumerate(rows):
        cid = str(i)
        siblings = _cluster_sources(conn, r["article_id"], r["media_name"])
        snippets = [_source_snippet(r)] + [_source_snippet(s) for s in siblings]
        blocks = [f"Source 1 ({r['media_name']}): {r['title']}\n{snippets[0]}"]
        for n, src in enumerate(siblings, start=2):
            blocks.append(f"Source {n} ({src['media_name']}): {src['title']}\n{snippets[n-1]}")
        if siblings:
            stats["synthesized"] += 1
        user = _published_line(r) + f"매체: {r['media_name']}  국가: {r['cc']}\n" + "\n\n".join(blocks)
        hint = numeric_guard.korean_amount_hints("\n".join(blocks))
        if hint:
            user += f"\n\n{hint}"
        sys_prompt = _SYS_THIN if sum(len(s) for s in snippets) <= _THIN_SOURCE_MAXLEN else _SYS
        requests.append((cid, sys_prompt, user, 4000))
        row_by_id[cid] = r

    results = provider.complete_json_batch(requests) if requests else {}

    cur = conn.cursor()
    retry: dict[str, tuple] = {}
    for cid, r in row_by_id.items():
        data = results.get(cid) or {}
        en = str(data.get("expanded_summary_en") or "").strip()[:7000]
        ko = str(data.get("expanded_summary_ko") or "").strip()[:7000]
        if not (en or ko):
            continue
        if en and ko and numeric_guard.amount_mismatch(en, ko):
            # 한국어 금액 단위 오변환($18.65 billion → "$18.65억") — 영문만 저장, 한국어는 다음 실행에서 재생성
            log.warning("긴 요약 금액 불일치 — 한국어 재요청(article_id=%s)", r["article_id"])
            retry[cid] = (r, en)
            ko = ""
        cur.execute(
            "UPDATE articles_raw SET expanded_summary = ?, expanded_summary_en = ? WHERE article_id = ?",
            (ko or None, en or None, r["article_id"]),
        )
        stats["written"] += 1

    if retry:
        hints = {cid: numeric_guard.korean_amount_hints(en) for cid, (_, en) in retry.items()}
        again = provider.complete_json_batch(
            [(cid, _SYS_KO_RETRY, f"{en}\n\n{hints[cid]}", 4000) for cid, (_, en) in retry.items()])
        for cid, (r, en) in retry.items():
            ko = str((again.get(cid) or {}).get("expanded_summary_ko") or "").strip()[:7000]
            if not ko or numeric_guard.amount_mismatch(en, ko):
                log.warning("긴 요약 금액 불일치 — 한국어 저장 안 함(article_id=%s)", r["article_id"])
                continue
            cur.execute("UPDATE articles_raw SET expanded_summary = ? WHERE article_id = ?",
                        (ko, r["article_id"]))
    conn.commit()

    log.info(
        "긴 요약 완료 — 대상=%d  작성=%d  다출처종합=%d",
        stats["total"], stats["written"], stats["synthesized"],
    )
    return stats
