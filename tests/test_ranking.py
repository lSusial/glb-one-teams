import sqlite3
import unittest

import ranking


class RankingSourceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''
            CREATE TABLE media_sources(source_id INTEGER PRIMARY KEY, media_name TEXT);
            INSERT INTO media_sources VALUES
                (1, 'Publisher A'), (2, 'Publisher B'), (3, 'Publisher C');
            CREATE TABLE articles_raw(
                article_id INTEGER PRIMARY KEY, source_id INTEGER, duplicate_of INTEGER
            );
        ''')
        self.addCleanup(self.db.close)

    def test_cluster_counts_independent_publishers_not_article_rows(self):
        self.db.executemany('INSERT INTO articles_raw VALUES(?,?,?)', [
            (10, 1, None),       # representative: Publisher A
            (11, 1, 10),         # same publisher repeats do not add weight
            (12, 1, 10),
            (13, 2, 10),         # one independent publisher
            (20, 1, None),
            (21, 2, 20),
            (22, 3, 20),         # two independent publishers
        ])
        self.assertEqual(ranking.cluster_sizes(self.db), {10: 1, 20: 2})

    def test_same_publisher_only_gets_no_cluster_bonus(self):
        self.db.executemany('INSERT INTO articles_raw VALUES(?,?,?)', [
            (10, 1, None), (11, 1, 10), (12, 1, 10),
        ])
        self.assertEqual(ranking.cluster_sizes(self.db), {})


if __name__ == '__main__':
    unittest.main()
