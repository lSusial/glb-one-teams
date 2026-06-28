-- glb-news-rss 프로토타입 스키마 (SQLite)
-- Java/Postgres 본 시스템으로 이관할 때 필드명이 그대로 매핑되도록 유지.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 매체 본체
CREATE TABLE IF NOT EXISTS media_sources (
    source_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    media_name           TEXT    NOT NULL UNIQUE,
    primary_country_code TEXT    NOT NULL,
    language             TEXT    NOT NULL,
    tier                 INTEGER NOT NULL,
    is_active            INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 매체-카테고리 매핑 (N:M)
CREATE TABLE IF NOT EXISTS media_category_map (
    source_id     INTEGER NOT NULL,
    category_code TEXT    NOT NULL,
    PRIMARY KEY (source_id, category_code),
    FOREIGN KEY (source_id) REFERENCES media_sources(source_id) ON DELETE CASCADE
);

-- 매체별 RSS 피드
CREATE TABLE IF NOT EXISTS media_source_feeds (
    feed_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER NOT NULL,
    feed_url     TEXT    NOT NULL UNIQUE,
    feed_section TEXT    NOT NULL,
    is_active    INTEGER NOT NULL DEFAULT 1,
    last_status  INTEGER,
    last_fetched TEXT,
    last_error   TEXT,
    FOREIGN KEY (source_id) REFERENCES media_sources(source_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_feeds_source ON media_source_feeds(source_id);

-- 수집된 기사 (파이프라인 전 컬럼 포함)
-- 기존 DB: 하위 컬럼은 각 모듈의 ensure_*_columns() ALTER TABLE 마이그레이션으로 추가됨
CREATE TABLE IF NOT EXISTS articles_raw (
    article_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id           INTEGER NOT NULL,
    source_id         INTEGER NOT NULL,
    title             TEXT    NOT NULL,
    link              TEXT    NOT NULL,
    summary           TEXT,
    published_at      TEXT,
    fetched_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    content_hash      TEXT    NOT NULL,
    -- 키워드 필터 (keyword_filter.py)
    filter_stage      INTEGER NOT NULL DEFAULT 0,
    filter_decision   TEXT    NOT NULL DEFAULT 'pending',
    filter_reason     TEXT,
    filter_score      INTEGER,
    -- 중복 탐지 (keyword_filter.py)
    duplicate_of      INTEGER REFERENCES articles_raw(article_id),
    -- LLM 1차 관문 (llm_prefilter.py)
    llm_prefilter     TEXT,
    llm_reject_reason TEXT,
    -- AI 분석 (llm_ranker.py)
    ai_score          INTEGER,
    summary_ko        TEXT,
    ai_model          TEXT,
    topics            TEXT,
    kb_implication    TEXT,   -- KB 시사점 (UI 'k') — llm_ranker.py 생성
    FOREIGN KEY (feed_id)   REFERENCES media_source_feeds(feed_id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES media_sources(source_id) ON DELETE CASCADE,
    UNIQUE (content_hash)
);

CREATE INDEX IF NOT EXISTS idx_articles_source     ON articles_raw(source_id);
CREATE INDEX IF NOT EXISTS idx_articles_feed       ON articles_raw(feed_id);
CREATE INDEX IF NOT EXISTS idx_articles_published  ON articles_raw(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_filter     ON articles_raw(filter_decision);
CREATE INDEX IF NOT EXISTS idx_articles_llmpre     ON articles_raw(llm_prefilter);
CREATE INDEX IF NOT EXISTS idx_articles_aiscore    ON articles_raw(ai_score);
CREATE INDEX IF NOT EXISTS idx_articles_dedup      ON articles_raw(duplicate_of);

-- 수집 실행 이력
CREATE TABLE IF NOT EXISTS fetch_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at  TEXT,
    feeds_total  INTEGER NOT NULL DEFAULT 0,
    feeds_ok     INTEGER NOT NULL DEFAULT 0,
    feeds_failed INTEGER NOT NULL DEFAULT 0,
    new_articles INTEGER NOT NULL DEFAULT 0,
    dup_articles INTEGER NOT NULL DEFAULT 0
);

-- 국가별 브리핑 (briefing.py에서 관리)
CREATE TABLE IF NOT EXISTS country_briefings (
    briefing_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    cc              TEXT NOT NULL,
    briefing_date   TEXT NOT NULL,
    briefing_type   TEXT NOT NULL DEFAULT 'weekly',
    generated_at    TEXT,
    summary         TEXT,
    issues          TEXT,
    outlook         TEXT,
    keywords        TEXT,
    key_stat        TEXT,
    model           TEXT,
    article_count   INTEGER,
    source_articles TEXT,
    UNIQUE(cc, briefing_date, briefing_type)
);

CREATE INDEX IF NOT EXISTS idx_briefings_cc_date ON country_briefings(cc, briefing_date DESC);
