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


if __name__ == '__main__':
    unittest.main()
