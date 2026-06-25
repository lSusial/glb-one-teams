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
import re as _re_mod
import logging
import sqlite3
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import certifi
import feedparser
import requests
import yaml

# 한국 일부 매체는 봇 UA를 403으로 차단하므로 일반 브라우저 UA 사용.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SEC = 20
MAX_PARALLEL_FETCH = 8
RETRY_DELAYS = (1, 3)  # 실패 시 재시도 대기(초): 1회→1s, 2회→3s

# certifi 번들을 명시적으로 사용 — macOS 시스템 Python의 SSL 인증서 미설치 회피.
_CA_BUNDLE = certifi.where()

log = logging.getLogger("collector")


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


# ---------------------------------------------------------------------------
# DB 초기화 & sources.yaml 동기화
# ---------------------------------------------------------------------------
def init_db(db_path: Path, schema_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
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
    last_err: str | None = None
    resp = None
    for attempt, delay in enumerate([-1] + list(RETRY_DELAYS)):
        if delay >= 0:
            time.sleep(delay)
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
            log.debug("retry %d/%d  %s  (%s)", attempt + 1, len(RETRY_DELAYS) + 1, url, last_err[:60])
            continue

        if resp.status_code >= 500:
            last_err = f"http {resp.status_code}"
            log.debug("retry %d/%d  %s  (%s)", attempt + 1, len(RETRY_DELAYS) + 1, url, last_err)
            continue

        break  # 성공 또는 4xx(재시도 불필요)
    else:
        return FetchResult(feed_id, url, -1, error=last_err or "unknown"), []

    status = resp.status_code
    if status >= 400:
        return FetchResult(feed_id, url, status, error=f"http {status}"), []

    try:
        parsed = feedparser.parse(resp.content)
    except Exception as e:  # noqa: BLE001
        return FetchResult(feed_id, url, status, error=f"parse_exception: {e!r}"[:200]), []

    bozo_exc = parsed.get("bozo_exception")
    if not parsed.entries and bozo_exc:
        return FetchResult(feed_id, url, status, error=f"bozo: {bozo_exc!r}"[:200]), []

    fetched_now = datetime.now(timezone.utc).isoformat()
    rows = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = _strip_html(entry.get("summary") or entry.get("description") or "")
        published = _parse_published(entry) or fetched_now
        rows.append((
            feed_id,
            source_id,
            title[:1000],
            link[:2000],
            summary[:4000],
            published,
            _content_hash(title, link),
        ))

    return FetchResult(feed_id, url, status), rows


# ---------------------------------------------------------------------------
# Google News 리다이렉트 URL 해소
# ---------------------------------------------------------------------------
_GNEWS_PREFIX = "https://news.google.com/"
_URL_RESOLVE_WORKERS = 30
_URL_RESOLVE_TIMEOUT = 5


def _resolve_single_url(url: str) -> str:
    """Google News 리다이렉트 URL → 실제 기사 URL (best-effort)."""
    if _GNEWS_PREFIX not in url:
        return url
    try:
        r = requests.get(
            url,
            allow_redirects=True,
            timeout=_URL_RESOLVE_TIMEOUT,
            verify=_CA_BUNDLE,
            headers={"User-Agent": USER_AGENT},
            stream=True,
        )
        r.close()
        final = r.url
        return final if _GNEWS_PREFIX not in final else url
    except Exception:
        return url


def resolve_google_news_links(
    conn: sqlite3.Connection, article_ids: list[int]
) -> int:
    """새로 삽입된 기사 중 Google News URL을 실제 기사 URL로 교체."""
    if not article_ids:
        return 0
    placeholders = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"SELECT article_id, link FROM articles_raw "
        f"WHERE article_id IN ({placeholders}) "
        f"AND link LIKE 'https://news.google.com/%'",
        article_ids,
    ).fetchall()
    if not rows:
        return 0

    log.info("Google News URL 해소 시작: %d건", len(rows))
    id_to_orig = {r["article_id"]: r["link"] for r in rows}

    with cf.ThreadPoolExecutor(max_workers=_URL_RESOLVE_WORKERS) as pool:
        futures = {
            pool.submit(_resolve_single_url, orig): art_id
            for art_id, orig in id_to_orig.items()
        }
        cur = conn.cursor()
        resolved = 0
        for fut in cf.as_completed(futures):
            art_id = futures[fut]
            new_url = fut.result()
            if new_url != id_to_orig[art_id]:
                cur.execute(
                    "UPDATE articles_raw SET link = ? WHERE article_id = ?",
                    (new_url[:2000], art_id),
                )
                resolved += 1

    conn.commit()
    log.info("Google News URL 해소 완료: %d건 업데이트 (/%d건)", resolved, len(rows))
    return resolved


# ---------------------------------------------------------------------------
# 전체 실행
# ---------------------------------------------------------------------------
def list_active_feeds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT feed_id, source_id, feed_url FROM media_source_feeds WHERE is_active = 1"
    ))


def run_fetch_all(conn: sqlite3.Connection) -> int:
    """전체 활성 피드를 병렬로 수집. 새 run_id 반환."""
    cur = conn.cursor()
    cur.execute("INSERT INTO fetch_runs DEFAULT VALUES")
    run_id = cur.lastrowid
    conn.commit()

    feeds = list_active_feeds(conn)
    log.info("fetching %d feeds", len(feeds))

    total = ok = failed = new_total = dup_total = 0
    new_article_ids: list[int] = []
    started = time.time()

    with cf.ThreadPoolExecutor(max_workers=MAX_PARALLEL_FETCH) as pool:
        futures = {
            pool.submit(fetch_feed, f["feed_id"], f["source_id"], f["feed_url"]): f
            for f in feeds
        }
        for fut in cf.as_completed(futures):
            f = futures[fut]
            try:
                result, rows = fut.result()
            except Exception as e:  # noqa: BLE001
                result = FetchResult(f["feed_id"], f["feed_url"], -1, error=repr(e))
                rows = []

            total += 1
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
                           (feed_id, source_id, title, link, summary, published_at, content_hash)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        row,
                    )
                    new_article_ids.append(cur.lastrowid)
                    new_count += 1
                except sqlite3.IntegrityError:
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
            log.info("OK    %-3s  new=%-3d dup=%-3d  %s",
                     result.status, new_count, dup_count, result.feed_url)

    resolve_google_news_links(conn, new_article_ids)

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
