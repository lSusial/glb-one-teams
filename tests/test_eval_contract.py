import unittest

import llm_ranker
from eval import run_eval


class RankerEvalContractTests(unittest.TestCase):
    def test_presence_eval_uses_live_ranker_prompt_and_context(self):
        request = run_eval.build_requests([
            {"cc": "JP", "title": "BOJ raises rates", "summary": "Policy changed."}
        ], production=True)[0]
        _, system, user, max_tokens = request
        self.assertEqual(system, llm_ranker._system_prompt())
        self.assertIn("KB 도쿄지점", user)
        self.assertIn("제목: BOJ raises rates", user)
        self.assertEqual(max_tokens, 700)

    def test_non_presence_eval_uses_live_light_prompt(self):
        request = run_eval.build_requests([
            {"cc": "PH", "title": "BSP decision", "summary": "Policy changed."}
        ], production=True)[0]
        self.assertEqual(request[1], llm_ranker._system_prompt_light())
        self.assertNotIn("거점 맥락", request[2])

    def test_production_scores_use_live_factor_formula(self):
        scores = run_eval.parse_scores({"0": {"score_factors": {
            "directness": 3, "magnitude": 2, "urgency": 3, "novelty": 2,
        }, "ai_score": 99}}, 1, production=True)
        self.assertEqual(scores, [67])

    def test_invalid_response_is_reported_as_missing(self):
        self.assertEqual(run_eval.parse_scores({"0": {}}, 1, production=True), [None])


if __name__ == "__main__":
    unittest.main()
