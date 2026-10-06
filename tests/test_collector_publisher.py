import unittest

from feedparser import FeedParserDict

import collector


class PublisherIdentityTests(unittest.TestCase):
    def test_google_news_source_metadata_wins(self):
        entry = FeedParserDict(source=FeedParserDict(title='Reuters'))
        self.assertEqual(collector._publisher_name(
            entry, 'https://news.google.com/rss/search?q=bank',
            'Headline with a dash - Wrong Suffix'), 'Reuters')

    def test_google_news_title_suffix_is_safe_fallback(self):
        self.assertEqual(collector._publisher_name(
            FeedParserDict(), 'https://news.google.com/rss/search?q=bank',
            'Bank announces rate decision - Bloomberg'), 'Bloomberg')

    def test_direct_rss_title_is_not_guessed(self):
        self.assertIsNone(collector._publisher_name(
            FeedParserDict(), 'https://example.com/rss', 'Policy outlook - analysis'))


class JunkTitleFilterTests(unittest.TestCase):
    """2026-10-06 소스 보강 중 발견: 미얀마·캄보디아·라오스 매체 다수가 워드프레스
    캐시 인덱스·카테고리 아카이브·페이지네이션을 RSS 항목으로 내보낸다. 수집 단계에서
    걸러야 prefilter LLM 호출까지 안 간다(eval/display_audit.py의 표시 단계 JUNK_RE와
    같은 철학, 더 이른 지점)."""

    def test_wordpress_cache_index_is_junk(self):
        self.assertTrue(collector._JUNK_TITLE_RE.search(
            'Index of /wp-content/cache/page_enhanced/www.irrawaddy.com/news/ethnic-issues'))

    def test_category_archive_is_junk(self):
        self.assertTrue(collector._JUNK_TITLE_RE.search('Business Archives - Phnom Penh Post'))
        self.assertTrue(collector._JUNK_TITLE_RE.search('Trade Restrictions Archives - The Irrawaddy'))

    def test_pagination_is_junk(self):
        self.assertTrue(collector._JUNK_TITLE_RE.search('Videos - Page 8 of 11 - Phnom Penh Post'))

    def test_real_headline_is_not_junk(self):
        self.assertFalse(collector._JUNK_TITLE_RE.search(
            'Chinese-Backed Solar Power, Northern Shan Border Trade, and More - The Irrawaddy'))
        self.assertFalse(collector._JUNK_TITLE_RE.search(
            'NBC refutes false rumours of banking instability - khmertimeskh.com'))
        self.assertFalse(collector._JUNK_TITLE_RE.search(
            'Laos Inflation Rises to 7.8 Percent in September as Education Costs Climb'))


if __name__ == '__main__':
    unittest.main()
