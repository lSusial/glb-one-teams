"""
eval/build_eval_v2.py — eval_set v2 생성 (랭커 평가용, 등급 0-3)

사용법:
  python eval/build_eval_v2.py           # 배치 API (기본, 50% 할인)
  python eval/build_eval_v2.py --sync    # 동기 호출 (즉시 결과, 디버깅)

출력:
  eval/eval_set_v2.jsonl      — 파이프라인 eval 입력
  eval/eval_set_v2_review.csv — 사람 검수용 (grade_final 컬럼 비워둠)

등급 기준:
  3 = 핵심  (DIRECT)       ai_score 75-100: 중앙은행 결정·자본통제·제재·KB 직접 영향
  2 = 중요  (CONTEXTUAL)   ai_score 50-74:  주요 환율·Fed·은행 M&A·거점 지정학 이벤트
  1 = 참고  (BACKGROUND)   ai_score 25-49:  글로벌 트렌드·간접 거시·일반 산업 리서치
  0 = 무관  (NOISE)        ai_score 0-24:   스포츠·연예·소형주 추천·지역 가십
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _load_dotenv(path: Path | None = None) -> None:
    path = path or Path(__file__).parent.parent / ".env"
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

from llm_provider import get_provider

EVAL_DIR   = Path(__file__).parent
OUT_JSONL  = EVAL_DIR / "eval_set_v2.jsonl"
OUT_CSV    = EVAL_DIR / "eval_set_v2_review.csv"
SEED_JSONL = EVAL_DIR / "eval_set.jsonl"
DB_PATH    = Path(__file__).parent.parent / "data" / "news.db"

random.seed(42)

# ── 등급 생성 시스템 프롬프트 ─────────────────────────────────────────────
_GRADE_SYSTEM = """\
당신은 KB금융그룹 글로벌 거점 인텔리전스 분석가다.
KB는 GB·US·HK·CN·JP·SG·IN·VN·MM·ID·KH에 지점·법인·자회사를 운영한다.
아래 기사에 KB 관점 중요도 등급(0-3)을 부여하고 이유를 30자 이내로 적어라.

등급 기준:
  3 = 핵심(DIRECT): KB 거점이 오늘 직접 영향받는 사건.
      예) 거점 국가 중앙은행 금리·환율 조치, 자본통제, 제재, 국가신용등급 변화,
          프라삭/KB부코핀 등 KB 자회사 관련 규제
  2 = 중요(CONTEXTUAL): KB 담당자가 당일 읽어야 하는 사안.
      예) 거점 국가 주요 통화 급변동, Fed/BoJ 정책 전환, 지역 지정학 이벤트,
          은행산업 M&A·건전성 이슈, 원자재 가격 급변, 대형 기업 실적·ESG 리스크
  1 = 참고(BACKGROUND): 배경 지식. 근기간 KB 액션 불필요.
      예) 글로벌 핀테크·ESG 트렌드, 선진국 간접 거시, 일반 산업 리서치
  0 = 무관(NOISE): KB 경영과 무관.
      예) 스포츠·연예·범죄, 개별 소형주 매매 추천, 지역 생활 정보

JSON만 출력(설명 없이): {"grade": 0|1|2|3, "reason": "30자 이내 한국어"}"""


# ── 언어 감지 ────────────────────────────────────────────────────────────
_VN_CHARS = set("ắặẩẫẻẹẽốồổỗộơớờởỡợụùúũưửữựỷỹỵàáâãèéêìíòóôõùúýăđảẽứịọẫấậ")
_ID_WORDS  = {"saham", "rupiah", "persen", "ihsg", "bi rate", "emiten",
              "naik", "turun", "melemah", "menguat", "perdagangan", "bursa"}


def detect_lang(title: str, cc: str) -> str:
    t = title.lower()
    # 일본어 (히라가나·카타카나·CJK)
    if any('　' <= c <= '鿿' for c in title):
        if cc == "JP":
            return "ja"
        if cc in ("CN", "HK", "SG"):
            return "zh"
    # 크메르어
    if any('ក' <= c <= '៿' for c in title):
        return "km"
    # 미얀마어
    if any('က' <= c <= '႟' for c in title):
        return "my"
    # 베트남어 (diacritics)
    if any(c in _VN_CHARS for c in title):
        return "vi"
    # 인도네시아어 (Latin + 특징 단어)
    if cc == "ID" and any(w in t for w in _ID_WORDS):
        return "id"
    return "en"


# ── DB 샘플링 ─────────────────────────────────────────────────────────────
_SAMPLE_PLAN = [
    # (cc, target_total, prefer_native_lang)
    ("KH",     12, True),
    ("GB",     12, False),
    ("HK",     12, False),
    ("SG",     10, False),
    ("MM",      8, True),
    ("VN",      6, True),
    ("ID",      6, True),
    ("JP",      6, False),
    ("CN",      4, False),
    ("US",      4, False),
    ("GLOBAL",  3, False),
]

# 기존 eval_set에서 제외할 기사 ID
_EXISTING_IDS: set[int] = set()


def _is_garbage(title: str) -> bool:
    """빈 제목·반복성 네비게이션 제목 필터."""
    if not title or not title.strip():
        return True
    garbage = [
        "all news", "home |", "more business", "more commentary",
        "latest videos", "cnn transcripts", "- bank of england",
        "ธนาคารជាតិ",  # NBC 반복 헤더
    ]
    tl = title.lower().strip()
    if len(tl) < 10:
        return True
    return any(g in tl for g in garbage)


def sample_from_db(conn: sqlite3.Connection) -> list[dict]:
    candidates: list[dict] = []
    seen_titles: set[str] = set()  # 전체 샘플 내 제목 중복 방지

    for cc, target, prefer_native in _SAMPLE_PLAN:
        # 현지어 우선 → 부족하면 영어로 보충
        pools: list[list] = []

        if prefer_native:
            native_rows = conn.execute("""
                SELECT a.article_id AS id, a.title, a.summary,
                       m.primary_country_code AS cc
                FROM articles_raw a
                JOIN media_sources m ON m.source_id = a.source_id
                WHERE m.primary_country_code = ?
                  AND a.filter_decision = 'passed'
                  AND a.summary IS NOT NULL AND a.summary != ''
                  AND a.article_id NOT IN ({})
                ORDER BY a.filter_score DESC
            """.format(",".join("?" * len(_EXISTING_IDS)) if _EXISTING_IDS else "SELECT 0"),
                [cc] + list(_EXISTING_IDS)).fetchall()

            native = [dict(r) for r in native_rows
                      if detect_lang(r["title"], cc) != "en"
                      and not _is_garbage(r["title"])]
            pools.append(native)

        # 영어 풀 (filter_score 중간~상위 범위에서 샘플링)
        all_rows = conn.execute("""
            SELECT a.article_id AS id, a.title, a.summary,
                   m.primary_country_code AS cc
            FROM articles_raw a
            JOIN media_sources m ON m.source_id = a.source_id
            WHERE m.primary_country_code = ?
              AND a.filter_decision = 'passed'
              AND a.summary IS NOT NULL AND a.summary != ''
              AND a.article_id NOT IN ({})
            ORDER BY a.filter_score DESC
            LIMIT 200
        """.format(",".join("?" * len(_EXISTING_IDS)) if _EXISTING_IDS else "SELECT 0"),
            [cc] + list(_EXISTING_IDS)).fetchall()

        en_pool = [dict(r) for r in all_rows
                   if not _is_garbage(r["title"])]
        pools.append(en_pool)

        selected: list[dict] = []
        seen_ids: set[int] = set()

        for pool in pools:
            # 앞 33%는 고점수, 나머지는 랜덤 (다양성 확보)
            high  = pool[:max(1, len(pool) // 3)]
            rest  = pool[len(pool) // 3:]
            picks = high[:max(1, target // 2)]
            rest_n = target - len([p for p in picks if p["id"] not in seen_ids])
            picks += random.sample(rest, min(len(rest), rest_n + 5))
            for item in picks:
                title_key = item["title"].strip().lower()[:60]
                if (item["id"] not in seen_ids
                        and title_key not in seen_titles
                        and len(selected) < target):
                    seen_ids.add(item["id"])
                    seen_titles.add(title_key)
                    selected.append(item)

        for item in selected:
            item["lang"] = detect_lang(item["title"], item["cc"])
            item["old_label"] = None
        candidates.extend(selected)

    return candidates


# ── 기존 eval_set 로드 ────────────────────────────────────────────────────
def load_existing() -> list[dict]:
    items = []
    with open(SEED_JSONL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            items.append({
                "id":        e.get("id"),
                "cc":        e.get("cc", "GLOBAL"),
                "lang":      detect_lang(e.get("title", ""), e.get("cc", "")),
                "title":     e.get("title", ""),
                "summary":   e.get("summary", ""),
                "old_label": e.get("label"),
            })
            _EXISTING_IDS.add(e["id"])
    return items


# ── LLM 등급 초안 생성 ────────────────────────────────────────────────────
def run_grading(items: list[dict], use_batch: bool) -> list[dict]:
    provider = get_provider("smart", use_batch=None if use_batch else False)

    reqs = []
    for i, e in enumerate(items):
        user = (
            f"[거점: {e['cc']}  언어: {e['lang']}]\n"
            f"제목: {e['title']}\n"
            f"요약: {(e.get('summary') or '')[:500]}"
        )
        reqs.append((str(i), _GRADE_SYSTEM, user, 120))

    print(f"  LLM 등급 초안 생성 — {len(items)}건 "
          f"({'배치' if use_batch else '동기'})...")
    results = provider.complete_json_batch(reqs)

    graded = []
    ok = fail = 0
    for i, e in enumerate(items):
        data = results.get(str(i)) or {}
        try:
            grade = int(data.get("grade"))
            grade = max(0, min(3, grade))
            ok += 1
        except (TypeError, ValueError):
            grade = None
            fail += 1
        item = dict(e)
        item["grade_draft"] = grade
        item["reason"]      = str(data.get("reason") or "")[:60]
        graded.append(item)

    print(f"  완료: 성공={ok}건  실패={fail}건")
    return graded


# ── 저장 ─────────────────────────────────────────────────────────────────
def save_jsonl(items: list[dict]) -> None:
    with open(OUT_JSONL, "w", encoding="utf-8") as f:
        for e in items:
            f.write(json.dumps({
                "id":          e["id"],
                "cc":          e["cc"],
                "lang":        e["lang"],
                "title":       e["title"],
                "summary":     (e.get("summary") or "")[:600],
                "grade_draft": e.get("grade_draft"),
                "reason":      e.get("reason", ""),
                "old_label":   e.get("old_label"),
            }, ensure_ascii=False) + "\n")
    print(f"  저장: {OUT_JSONL}  ({len(items)}건)")


def save_csv(items: list[dict]) -> None:
    # 등급별 정렬 (3→0 내림차순, 같은 등급 내에서 cc 알파벳순)
    rows = sorted(items, key=lambda x: (-(x.get("grade_draft") or -1), x["cc"]))

    # cc별 등급 분포 집계
    from collections import Counter
    grade_dist: dict[int, int] = Counter(
        e["grade_draft"] for e in items if e.get("grade_draft") is not None
    )
    lang_dist: dict[str, int] = Counter(e["lang"] for e in items)
    cc_dist:   dict[str, int] = Counter(e["cc"]   for e in items)

    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)

        # 헤더 메타
        w.writerow(["# eval_set_v2 검수용 CSV"])
        w.writerow(["# 총건수", len(items)])
        w.writerow(["# 등급분포(초안)",
                    f"3={grade_dist.get(3,0)}  2={grade_dist.get(2,0)}  "
                    f"1={grade_dist.get(1,0)}  0={grade_dist.get(0,0)}  "
                    f"미분류={grade_dist.get(None,0)}"])
        w.writerow(["# 언어분포",
                    "  ".join(f"{k}={v}" for k, v in sorted(lang_dist.items()))])
        w.writerow(["# 거점분포",
                    "  ".join(f"{k}={v}" for k, v in sorted(cc_dist.items()))])
        w.writerow([])

        # 컬럼 헤더
        w.writerow([
            "no", "id", "cc", "lang", "old_label",
            "grade_draft", "grade_final",   # grade_final: 검수자 기입란
            "reason", "notes",              # notes: 검수자 메모란
            "title",
        ])

        for i, e in enumerate(rows, 1):
            w.writerow([
                i,
                e["id"],
                e["cc"],
                e["lang"],
                e.get("old_label", ""),
                e.get("grade_draft", ""),
                "",                          # grade_final (검수자 기입)
                e.get("reason", ""),
                "",                          # notes (검수자 메모)
                e["title"],
            ])

    print(f"  저장: {OUT_CSV}  ({len(rows)}건)")


# ── 통계 출력 ─────────────────────────────────────────────────────────────
def print_summary(items: list[dict]) -> None:
    from collections import Counter
    grade_dist = Counter(e.get("grade_draft") for e in items)
    cc_dist    = Counter(e["cc"]   for e in items)
    lang_dist  = Counter(e["lang"] for e in items)

    print(f"\n{'='*60}")
    print(f"  eval_set v2  총 {len(items)}건")
    print(f"{'='*60}")
    print("  등급 분포 (초안):")
    for g in [3, 2, 1, 0, None]:
        n = grade_dist.get(g, 0)
        label = {3:"핵심", 2:"중요", 1:"참고", 0:"무관", None:"미분류"}.get(g)
        bar = "█" * (n // 2)
        print(f"    {str(g):4s} {label:4s}: {n:3d}건  {bar}")

    print("\n  거점 분포:")
    for cc, n in sorted(cc_dist.items(), key=lambda x: -x[1]):
        print(f"    {cc:8s}: {n}건")

    print("\n  언어 분포:")
    for lang, n in sorted(lang_dist.items(), key=lambda x: -x[1]):
        label = {"en":"영어","ja":"일본어","zh":"중국어","id":"인니어",
                 "vi":"베트남어","km":"크메르어","my":"미얀마어"}.get(lang, lang)
        print(f"    {lang:4s} {label:8s}: {n}건")


# ── 메인 ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true", help="동기 호출 (디버깅용)")
    ap.add_argument("--no-grade", action="store_true",
                    help="LLM 등급 생성 건너뜀 (샘플링 결과만 확인)")
    args = ap.parse_args()

    # 1. 기존 70건 로드
    print("[1/4] 기존 eval_set.jsonl 로드...")
    existing = load_existing()
    print(f"  기존: {len(existing)}건  (id 풀 {len(_EXISTING_IDS)}개 등록)")

    # 2. DB 신규 샘플
    print("[2/4] DB 신규 기사 샘플링...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    new_items = sample_from_db(conn)
    conn.close()
    print(f"  신규: {len(new_items)}건")
    from collections import Counter
    for cc, n in sorted(Counter(e["cc"] for e in new_items).items()):
        print(f"    {cc:8s}: {n}건")

    all_items = existing + new_items
    print(f"  합산: {len(all_items)}건")

    if args.no_grade:
        # 등급 없이 저장 (확인용)
        for e in all_items:
            e["grade_draft"] = None
            e["reason"] = ""
        save_jsonl(all_items)
        save_csv(all_items)
        return

    # 3. LLM 등급 초안
    print("[3/4] LLM 등급 초안 생성...")
    graded = run_grading(all_items, use_batch=not args.sync)

    # 4. 저장
    print("[4/4] 저장...")
    save_jsonl(graded)
    save_csv(graded)
    print_summary(graded)


if __name__ == "__main__":
    main()
