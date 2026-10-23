import unittest

import briefing
import llm_prefilter
import llm_ranker


def _it(codes, name):
    return {"country_codes": codes, "headline_ko": name}


class HighlightsBalanceTests(unittest.TestCase):
    def test_cap_pool_limits_each_country(self):
        rows = [{"subject_cc": "ID", "cc": "ID"}] * 10 + [{"subject_cc": "GLOBAL", "cc": "US"}] * 3
        pool = briefing._cap_pool(rows, 40, 6)
        self.assertEqual(sum(r["subject_cc"] == "ID" for r in pool), 6)
        self.assertEqual(sum(r["subject_cc"] == "GLOBAL" for r in pool), 3)

    def test_select_caps_country_and_fills_from_rest(self):
        items = [_it(["ID"], f"id{i}") for i in range(5)] + [_it(["IN"], "in0"), _it(["JP"], "jp0")]
        out = briefing._select_highlights(items, 5, 2, 0, 3)
        names = [i["headline_ko"] for i in out]
        self.assertEqual(names[:4], ["id0", "id1", "in0", "jp0"])
        self.assertEqual(len(out), 5)   # 상한 탈락분으로 빈 슬롯을 채운다

    def test_select_reserves_global_slots(self):
        items = [_it([c], c) for c in ["ID", "IN", "JP", "VN", "CN"]] + [_it(["GLOBAL"], "g0"), _it(["GLOBAL"], "g1")]
        out = briefing._select_highlights(items, 5, 2, 2, 3)
        names = [i["headline_ko"] for i in out]
        self.assertEqual(sum(n.startswith("g") for n in names), 2)
        self.assertEqual(names[:3], ["ID", "IN", "JP"])   # 중요도 높은 앞쪽은 유지

    def test_select_caps_global(self):
        items = [_it(["GLOBAL"], f"g{i}") for i in range(5)] + [_it(["JP"], "jp")]
        out = briefing._select_highlights(items, 4, 2, 2, 3)
        self.assertEqual([i["headline_ko"] for i in out], ["g0", "g1", "g2", "jp"])

    def test_prompts_keep_financial_cyber_incidents(self):
        self.assertIn("PHP", llm_prefilter._SYSTEM)
        self.assertIn("critical software vulnerability", llm_ranker._SCORE_FACTORS_BLOCK)


if __name__ == "__main__":
    unittest.main()
