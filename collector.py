"""
glb-news-rss 수집기 (프로토타입)

실행계획_v3.md Phase 1의 "수집 MVP" 부분을 Python으로 구현.
- sources.yaml에서 매체/피드 정의를 읽어 DB에 동기화
- 각 피드를 feedparser로 가져와 articles_raw에 저장 (중복 제거)
- 매체별 가용성(HTTP 코드, 기사 수, 마지막 게시 시각) 기록

필터링(섹션/키워드/LLM)은 다음 단계에서 추가.
"""
from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import hashlib
import html as _html_mod
import random
import re as _re_mod
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import certifi
import feedparser
import requests
import yaml

import config
import db

# 수집 튜닝 상수는 config.py 로 일원화 (이름 유지 — 본문 참조 호환).
USER_AGENT              = config.USER_AGENT
REQUEST_TIMEOUT_SEC     = config.REQUEST_TIMEOUT_SEC
MAX_PARALLEL_FETCH      = config.MAX_PARALLEL_FETCH
RETRY_DELAYS            = config.RETRY_DELAYS
RATE_LIMIT_STATUSES     = config.RATE_LIMIT_STATUSES
RATE_LIMIT_RETRY_DELAYS = config.RATE_LIMIT_RETRY_DELAYS
RATE_LIMIT_JITTER_FRAC  = config.RATE_LIMIT_JITTER_FRAC
GNEWS_FETCH_DELAY       = config.GNEWS_FETCH_DELAY
GNEWS_MAX_PARALLEL      = config.GNEWS_MAX_PARALLEL

# certifi 번들을 명시적으로 사용 — macOS 시스템 Python의 SSL 인증서 미설치 회피.
_CA_BUNDLE = certifi.where()

log = logging.getLogger("collector")

# RSS 피드가 실제 기사 대신 워드프레스 캐시 인덱스·카테고리 아카이브·페이지네이션을
# 내놓는 경우(2026-10-06: 소스 보강 중 발견 — 미얀마·캄보디아 매체 다수가 이 패턴).
# eval/display_audit.py의 JUNK_RE(표시 단계)와 같은 철학이지만 더 이르게, 수집
# 단계에서 걸러 prefilter LLM 호출까지 안 가게 한다.
_JUNK_TITLE_RE = _re_mod.compile(
    r"^index of /|wp-content|^404\b|not found|just a moment|access denied|attention required|"
    r"enable javascript|captcha|cloudflare|are you a robot|^subscribe|^sign in|^log in|"
    r"page unavailable|\barchives?\s*$|\barchives?\s*-|"
    r"-\s*page\s*\d+\s*(of\s*\d+)?\s*-|^videos?\s*-\s*page\s*\d+",
    _re_mod.I)


def ensure_article_columns(conn: sqlite3.Connection) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("publisher_name", "ALTER TABLE articles_raw ADD COLUMN publisher_name TEXT"),
    ])


def _publisher_name(entry, feed_url: str, title: str) -> str | None:
    """RSS의 기사별 원발행사명. Google News는 ``source.title``을 제공한다.

    일부 과거/변형 피드에서 source가 빠지면 Google News 제목의 마지막 `` - 매체``
    접미사만 제한적으로 사용한다. 일반 직접 RSS 제목은 임의로 자르지 않는다.
    """
    source = entry.get("source") if entry is not None else None
    if isinstance(source, dict):
        name = str(source.get("title") or "").strip()
    else:
        name = str(source or "").strip()
    if not name and _is_google_news(feed_url) and " - " in (title or ""):
        name = title.rsplit(" - ", 1)[-1].strip()
    if not name or len(name) > 120 or name.lower() in {"google news", "google"}:
        return None
    return name


def backfill_publisher_names(conn: sqlite3.Connection) -> int:
    """기존 Google News 기사에서 제목 접미사로 비어 있는 원발행사명을 보정한다."""
    ensure_article_columns(conn)
    rows = conn.execute(
        """SELECT a.article_id, a.title, f.feed_url
           FROM articles_raw a
           JOIN media_source_feeds f ON f.feed_id = a.feed_id
           WHERE COALESCE(a.publisher_name, '') = ''
             AND f.feed_url LIKE '%news.google.com%'"""
    ).fetchall()
    updates = []
    for row in rows:
        name = _publisher_name(None, row["feed_url"], row["title"] or "")
        if name:
            updates.append((name, row["article_id"]))
    if updates:
        conn.executemany("UPDATE articles_raw SET publisher_name=? WHERE article_id=?", updates)
        conn.commit()
    return len(updates)


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class FetchResult:
    feed_id: int
    feed_url: str
    status: int          # HTTP 코드. -1 = 네트워크/파싱 에러
    new_count: int = 0
    dup_count: int = 0
    error: str | None = None
    hits_429: int = 0    # 이번 fetch 중 429를 받은 횟수(최종 성공해도 기록 — 차단 조짐 관측용)
    hits_503: int = 0    # 이번 fetch 중 503을 받은 횟수(위와 동일)


# ---------------------------------------------------------------------------
# DB 초기화 & sources.yaml 동기화
# ---------------------------------------------------------------------------
def init_db(db_path: Path, schema_path: Path) -> sqlite3.Connection:
    """표준 PRAGMA(db.open_conn)를 적용한 연결에 스키마를 실행한다."""
    conn = db.open_conn(db_path)
    with open(schema_path, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    return conn


def sync_sources(conn: sqlite3.Connection, sources_yaml: Path) -> None:
    """sources.yaml의 정의를 DB(media_sources, media_source_feeds, map)에 upsert."""
    with open(sources_yaml, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    cur = conn.cursor()
    for src in data["sources"]:
        # active: false 로 표시된 소스는 피드 비활성화만 처리 (DB 레코드 유지)
        is_active_source = src.get("active", True)

        cur.execute(
            """
            INSERT INTO media_sources (media_name, primary_country_code, language, tier)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(media_name) DO UPDATE SET
                primary_country_code = excluded.primary_country_code,
                language             = excluded.language,
                tier                 = excluded.tier
            """,
            (src["media_name"], src["country"], src["language"], src["tier"]),
        )
        cur.execute("SELECT source_id FROM media_sources WHERE media_name = ?",
                    (src["media_name"],))
        source_id = cur.fetchone()["source_id"]

        # 카테고리 매핑 재설정
        cur.execute("DELETE FROM media_category_map WHERE source_id = ?", (source_id,))
        for cat in src.get("categories", []):
            cur.execute(
                "INSERT INTO media_category_map (source_id, category_code) VALUES (?, ?)",
                (source_id, cat),
            )

        # 소스가 비활성(active: false)이면 모든 피드를 비활성화하고 건너뜀
        if not is_active_source:
            cur.execute(
                "UPDATE media_source_feeds SET is_active = 0 WHERE source_id = ?",
                (source_id,),
            )
            log.debug("비활성 소스 건너뜀: %s", src["media_name"])
            continue

        # 피드 upsert
        current_urls = {feed["url"] for feed in src.get("feeds", [])}
        for feed in src.get("feeds", []):
            cur.execute(
                """
                INSERT INTO media_source_feeds (source_id, feed_url, feed_section)
                VALUES (?, ?, ?)
                ON CONFLICT(feed_url) DO UPDATE SET
                    source_id    = excluded.source_id,
                    feed_section = excluded.feed_section,
                    is_active    = 1
                """,
                (source_id, feed["url"], feed["section"]),
            )

        # sources.yaml에서 제거된 구 URL 비활성화
        if current_urls:
            placeholders = ",".join("?" * len(current_urls))
            cur.execute(
                f"""UPDATE media_source_feeds
                    SET is_active = 0
                    WHERE source_id = ? AND feed_url NOT IN ({placeholders})""",
                (source_id, *current_urls),
            )
        else:
            cur.execute(
                "UPDATE media_source_feeds SET is_active = 0 WHERE source_id = ?",
                (source_id,),
            )

    # sources.yaml에서 완전히 제거된 매체의 피드 비활성화
    active_names = [src["media_name"] for src in data["sources"]]
    if active_names:
        placeholders = ",".join("?" * len(active_names))
        cur.execute(
            f"""UPDATE media_source_feeds SET is_active = 0
                WHERE source_id IN (
                    SELECT source_id FROM media_sources
                    WHERE media_name NOT IN ({placeholders})
                )""",
            active_names,
        )
    conn.commit()


# ---------------------------------------------------------------------------
# 단일 피드 수집
# ---------------------------------------------------------------------------
def _strip_html(text: str) -> str:
    """RSS description에서 HTML 태그·엔티티 제거 후 순수 텍스트 반환."""
    text = _html_mod.unescape(text or "")
    text = _re_mod.sub(r"<[^>]+>", " ", text)
    text = _re_mod.sub(r"[ \xa0]{2,}", " ", text)
    return text.strip()


def _content_hash(title: str, link: str) -> str:
    return hashlib.sha256(f"{title}\x1f{link}".encode("utf-8")).hexdigest()


_GNEWS_HOST = "news.google.com"


def _is_google_news(url: str) -> bool:
    return _GNEWS_HOST in url


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After 헤더(초 또는 HTTP-date)를 대기 초로 변환. 파싱 실패 시 None."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


# Google News 전용 페이싱 — 풀 병렬 수(GNEWS_MAX_PARALLEL)와 무관하게
# "요청 사이 최소 간격"을 보장한다 (여러 워커가 동시에 디스패치해도 이 락으로 직렬화).
_gnews_pace_lock = threading.Lock()
_gnews_last_dispatch = 0.0


def _gnews_pace() -> None:
    global _gnews_last_dispatch
    target_gap = GNEWS_FETCH_DELAY * (1 + random.uniform(0, RATE_LIMIT_JITTER_FRAC))
    with _gnews_pace_lock:
        now = time.monotonic()
        wait = _gnews_last_dispatch + target_gap - now
        if wait > 0:
            time.sleep(wait)
        _gnews_last_dispatch = time.monotonic()


def _parse_published(entry) -> str | None:
    """feedparser가 파싱한 published_parsed 또는 published 문자열에서 ISO 8601 추출."""
    if getattr(entry, "published_parsed", None):
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
    if getattr(entry, "updated_parsed", None):
        return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).isoformat()
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            return None
    return None


def fetch_feed(feed_id: int, source_id: int, url: str) -> tuple[FetchResult, list[tuple]]:
    """피드 1개를 가져오고 (메타, 기사 row 리스트) 반환. DB에는 쓰지 않음.

    requests로 먼저 raw bytes를 받아 certifi CA 번들로 SSL 검증한 뒤,
    feedparser에 bytes를 직접 넘긴다 — feedparser 내부 urllib보다 SSL/리다이렉트가 안정적.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
        "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    }
    if _is_google_news(url):
        _gnews_pace()  # 풀 병렬 수와 무관하게 요청 간 최소 간격 보장

    last_err: str | None = None
    resp = None
    hits_429 = hits_503 = 0
    generic_tries = rate_limit_tries = 0

    while True:
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SEC,
                verify=_CA_BUNDLE,
                allow_redirects=True,
            )
        except requests.exceptions.SSLError as e:
            # SSL 오류는 재시도해도 해결 안 됨
            return FetchResult(feed_id, url, -1, error=f"ssl_error: {e!r}"[:200]), []
        except requests.exceptions.RequestException as e:
            last_err = f"request_error: {e!r}"[:200]
            if generic_tries >= len(RETRY_DELAYS):
                return FetchResult(feed_id, url, -1, error=last_err,
                                    hits_429=hits_429, hits_503=hits_503), []
            log.debug("retry %d/%d  %s  (%s)", generic_tries + 1, len(RETRY_DELAYS), url, last_err[:60])
            time.sleep(RETRY_DELAYS[generic_tries])
            generic_tries += 1
            continue

        if resp.status_code in RATE_LIMIT_STATUSES:
            if resp.status_code == 429:
                hits_429 += 1
            else:
                hits_503 += 1
            last_err = f"http {resp.status_code}"
            if rate_limit_tries >= len(RATE_LIMIT_RETRY_DELAYS):
                return FetchResult(feed_id, url, resp.status_code, error=last_err,
                                    hits_429=hits_429, hits_503=hits_503), []
            wait = _parse_retry_after(resp.headers.get("Retry-After"))
            if wait is None:
                base = RATE_LIMIT_RETRY_DELAYS[rate_limit_tries]
                wait = base * (1 + random.uniform(0, RATE_LIMIT_JITTER_FRAC))
            log.debug("rate-limit retry %d/%d  %s  wait=%.1fs (%s)",
                       rate_limit_tries + 1, len(RATE_LIMIT_RETRY_DELAYS), url, wait, last_err)
            time.sleep(wait)
            rate_limit_tries += 1
            continue

        if resp.status_code >= 500:
            last_err = f"http {resp.status_code}"
            if generic_tries >= len(RETRY_DELAYS):
                return FetchResult(feed_id, url, resp.status_code, error=last_err,
                                    hits_429=hits_429, hits_503=hits_503), []
            log.debug("retry %d/%d  %s  (%s)", generic_tries + 1, len(RETRY_DELAYS), url, last_err)
            time.sleep(RETRY_DELAYS[generic_tries])
            generic_tries += 1
            continue

        break  # 성공 또는 재시도 불필요한 4xx

    status = resp.status_code
    if status >= 400:
        return FetchResult(feed_id, url, status, error=f"http {status}",
                            hits_429=hits_429, hits_503=hits_503), []

    try:
        parsed = feedparser.parse(resp.content)
    except Exception as e:  # noqa: BLE001
        return FetchResult(feed_id, url, status, error=f"parse_exception: {e!r}"[:200],
                            hits_429=hits_429, hits_503=hits_503), []

    bozo_exc = parsed.get("bozo_exception")
    if not parsed.entries and bozo_exc:
        return FetchResult(feed_id, url, status, error=f"bozo: {bozo_exc!r}"[:200],
                            hits_429=hits_429, hits_503=hits_503), []

    fetched_now = datetime.now(timezone.utc).isoformat()
    rows = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        if _JUNK_TITLE_RE.search(title):
            continue
        summary = _strip_html(entry.get("summary") or entry.get("description") or "")
        published = _parse_published(entry) or fetched_now
        publisher = _publisher_name(entry, url, title)
        rows.append((
            feed_id,
            source_id,
            title[:1000],
            link[:2000],
            summary[:4000],
            published,
            _content_hash(title, link),
            publisher,
        ))

    return FetchResult(feed_id, url, status, hits_429=hits_429, hits_503=hits_503), rows


# ---------------------------------------------------------------------------
# 전체 실행
# ---------------------------------------------------------------------------
def list_active_feeds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT feed_id, source_id, feed_url FROM media_source_feeds WHERE is_active = 1"
    ))


def run_fetch_all(conn: sqlite3.Connection) -> int:
    """전체 활성 피드를 병렬로 수집. 새 run_id 반환.

    direct RSS(MAX_PARALLEL_FETCH=8, 딜레이 없음)와 Google News
    (GNEWS_MAX_PARALLEL=3 + 요청 간 딜레이)는 별도 풀로 분리해 Google 쪽
    요청 패턴을 얌전하게 유지한다 — direct RSS 커버리지·속도는 그대로.
    """
    ensure_article_columns(conn)
    backfilled = backfill_publisher_names(conn)
    if backfilled:
        log.info("기존 Google News 원발행사 보정=%d건", backfilled)
    cur = conn.cursor()
    cur.execute("INSERT INTO fetch_runs DEFAULT VALUES")
    run_id = cur.lastrowid
    conn.commit()

    feeds = list_active_feeds(conn)
    direct_feeds = [f for f in feeds if not _is_google_news(f["feed_url"])]
    gnews_feeds = [f for f in feeds if _is_google_news(f["feed_url"])]
    log.info("fetching %d feeds (direct RSS %d, Google News %d)",
              len(feeds), len(direct_feeds), len(gnews_feeds))

    total = ok = failed = new_total = dup_total = 0
    hits_429_total = hits_503_total = 0
    rate_limited_feeds = rate_limited_final_fail = 0
    started = time.time()

    with cf.ThreadPoolExecutor(max_workers=MAX_PARALLEL_FETCH) as direct_pool, \
         cf.ThreadPoolExecutor(max_workers=GNEWS_MAX_PARALLEL) as gnews_pool:
        futures = {
            direct_pool.submit(fetch_feed, f["feed_id"], f["source_id"], f["feed_url"]): f
            for f in direct_feeds
        }
        futures.update({
            gnews_pool.submit(fetch_feed, f["feed_id"], f["source_id"], f["feed_url"]): f
            for f in gnews_feeds
        })
        for fut in cf.as_completed(futures):
            f = futures[fut]
            try:
                result, rows = fut.result()
            except Exception as e:  # noqa: BLE001
                result = FetchResult(f["feed_id"], f["feed_url"], -1, error=repr(e))
                rows = []

            total += 1
            if result.hits_429 or result.hits_503:
                rate_limited_feeds += 1
                hits_429_total += result.hits_429
                hits_503_total += result.hits_503
                if result.error and result.status in RATE_LIMIT_STATUSES:
                    rate_limited_final_fail += 1

            if result.error:
                failed += 1
                log.warning("FAIL  %-3s  %s  (%s)",
                            result.status, result.feed_url, result.error[:80])
            else:
                ok += 1

            # DB write (직렬 — SQLite 단일 writer)
            new_count = dup_count = 0
            for row in rows:
                try:
                    cur.execute(
                        """INSERT INTO articles_raw
                           (feed_id, source_id, title, link, summary, published_at, content_hash,
                            publisher_name)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        row,
                    )
                    new_count += 1
                except sqlite3.IntegrityError:
                    # 기존 행도 새 RSS 응답이 제공한 원발행사 정보로 보강한다.
                    if row[-1]:
                        cur.execute(
                            "UPDATE articles_raw SET publisher_name=COALESCE(NULLIF(publisher_name,''), ?) "
                            "WHERE content_hash=?",
                            (row[-1], row[-2]),
                        )
                    dup_count += 1
            new_total += new_count
            dup_total += dup_count

            cur.execute(
                """UPDATE media_source_feeds
                   SET last_status = ?, last_fetched = CURRENT_TIMESTAMP, last_error = ?
                   WHERE feed_id = ?""",
                (result.status, result.error, result.feed_id),
            )
            conn.commit()
            if not result.error:
                log.info("OK    %-3s  new=%-3d dup=%-3d  %s",
                         result.status, new_count, dup_count, result.feed_url)


    cur.execute(
        """UPDATE fetch_runs
           SET finished_at  = CURRENT_TIMESTAMP,
               feeds_total  = ?,
               feeds_ok     = ?,
               feeds_failed = ?,
               new_articles = ?,
               dup_articles = ?
           WHERE run_id = ?""",
        (total, ok, failed, new_total, dup_total, run_id),
    )
    conn.commit()

    if hits_429_total or hits_503_total:
        log.info("rate-limit 신호: 429=%d회 503=%d회 (영향 피드 %d개, 최종실패 %d개) — "
                  "빈발 시 GNEWS_FETCH_DELAY/GNEWS_MAX_PARALLEL 조정 고려",
                  hits_429_total, hits_503_total, rate_limited_feeds, rate_limited_final_fail)
    else:
        log.info("rate-limit 신호: 없음 (429/503 미검출)")

    log.info("done in %.1fs — feeds %d (ok %d / fail %d), new %d, dup %d",
             time.time() - started, total, ok, failed, new_total, dup_total)
    return run_id


# ---------------------------------------------------------------------------
# 가용성 리포트
# ---------------------------------------------------------------------------
def build_availability_report(conn: sqlite3.Connection) -> str:
    rows = conn.execute("""
        SELECT m.media_name, m.primary_country_code, m.tier,
               f.feed_section, f.feed_url, f.last_status, f.last_error,
               COALESCE(s.article_count, 0) AS article_count,
               s.last_published
        FROM media_source_feeds f
        JOIN media_sources m ON m.source_id = f.source_id
        LEFT JOIN (
            SELECT feed_id, COUNT(*) AS article_count, MAX(published_at) AS last_published
            FROM articles_raw
            GROUP BY feed_id
        ) s ON s.feed_id = f.feed_id
        WHERE f.is_active = 1
        ORDER BY m.primary_country_code, m.media_name, f.feed_section
    """).fetchall()

    lines = ["# 매체 가용성 리포트", "",
             f"_생성 시각: {datetime.now(timezone.utc).isoformat()}_", "",
             "| 국가 | 매체 | Tier | 섹션 | HTTP | 기사수 | 마지막 게시 | 에러 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        err = (r["last_error"] or "")[:60]
        lines.append(
            f"| {r['primary_country_code']} | {r['media_name']} | T{r['tier']} | "
            f"{r['feed_section']} | {r['last_status'] or '-'} | "
            f"{r['article_count']} | {r['last_published'] or '-'} | {err} |"
        )

    # 국가별 요약
    summary = conn.execute("""
        SELECT m.primary_country_code AS country,
               COUNT(DISTINCT m.source_id) AS media_count,
               COUNT(DISTINCT f.feed_id)   AS feed_count,
               SUM(CASE WHEN f.last_status BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS feeds_ok,
               SUM(CASE WHEN f.last_status BETWEEN 200 AND 299 THEN 0 ELSE 1 END) AS feeds_bad,
               COALESCE(SUM(s.article_count), 0) AS articles
        FROM media_sources m
        LEFT JOIN media_source_feeds f ON f.source_id = m.source_id AND f.is_active = 1
        LEFT JOIN (
            SELECT feed_id, COUNT(*) AS article_count
            FROM articles_raw
            GROUP BY feed_id
        ) s ON s.feed_id = f.feed_id
        GROUP BY m.primary_country_code
        ORDER BY m.primary_country_code
    """).fetchall()
    lines += ["", "## 국가별 요약", "",
              "| 국가 | 매체 수 | 피드 수 | OK | 실패 | 기사 수 |",
              "|---|---|---|---|---|---|"]
    for s in summary:
        lines.append(f"| {s['country']} | {s['media_count']} | {s['feed_count']} | "
                     f"{s['feeds_ok']} | {s['feeds_bad']} | {s['articles']} |")

    return "\n".join(lines) + "\n"
