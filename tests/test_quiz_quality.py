import unittest

import briefing


def _quiz(q, choices, idx=0, q_en="Q?", exp="근거."):
    return {"question_ko": q, "question_en": q_en, "choices_ko": choices,
            "choices_en": [f"c{i}" for i in range(len(choices))], "correct_index": idx,
            "explanation_ko": exp, "explanation_en": "why."}


class QuizQualityTests(unittest.TestCase):
    """2026-11-20 품질 점검에서 나온 실제 불량 문항으로 만든 회귀 테스트."""

    def test_mixed_unit_distractors_rejected(self):
        q = _quiz("DBS의 11월 6일 종가는?", ["69.72달러", "5,050.43포인트", "28.1억 달러", "S$15 billion"])
        issues = briefing._quiz_issues(q, "DBS closed at 69.72", "2026-11-06")
        self.assertTrue(any("단위" in i for i in issues))

    def test_fx_quote_with_other_currencies_rejected(self):
        q = _quiz("루피화는 달러당 얼마?", ["달러당 94.04루피", "달러당 18,221루피아", "달러당 160엔"])
        self.assertTrue(briefing._quiz_issues(q, "rupee 94.04 per dollar", "2026-11-12"))

    def test_untranslated_magnitude_rejected(self):
        q = _quiz("중국 무역흑자는?", ["1.04 trillion 달러", "1,335억 달러"])
        self.assertIn("한국어에 번역 안 된 단위", briefing._quiz_issues(q, "1.04 trillion", "2026-11-10"))

    def test_year_not_in_source_rejected(self):
        q = _quiz("2025년 11월 12일 기사 기준 루피화 종가는?", ["94.04루피", "95.22루피"])
        issues = briefing._quiz_issues(q, "rupee closed at 94.04 on Nov 12", "2026-11-12")
        self.assertTrue(any("연도" in i for i in issues))

    def test_legit_questions_pass(self):
        ok = [
            _quiz("한국은행 기준금리 결정은?", ["0.25%p 인상", "0.25%p 인하", "2.50%로 동결"], 2),
            _quiz("ELS 제재 금액은?", ["9,000억 원", "1.2조 원", "5,000억 원"], 1),
            _quiz("허가 대상이 아닌 나라는?", ["말레이시아", "베트남", "싱가포르", "태국"], 1),
            _quiz("MAS 결정은?", ["기준금리를 25bp 인상했다", "정책을 동결했다", "환율밴드를 넓혔다"], 1),
        ]
        src = "BOK held at 2.50%, 1.2조 원 penalty, Vietnam not included, MAS held policy"
        for q in ok:
            self.assertEqual(briefing._quiz_issues(q, src, "2026-11-20"), [], q["question_ko"])

    def test_numeric_limit(self):
        q = _quiz("소비자물가 상승률은?", ["2.5%", "3.9%", "5.1%"], 1)
        self.assertIn("숫자형 문제 제한 초과", briefing._quiz_issues(q, "CPI rose 3.9%", "2026-11-07", numeric_allowed=False))

    def test_shuffle_keeps_answer_and_language_alignment(self):
        q = _quiz("?", ["A", "B", "C", "D"], 0)
        q["choices_en"] = ["a", "b", "c", "d"]
        positions = set()
        for d in range(30):
            s = briefing._shuffle_quiz(q, f"2026-11-{d:02d}:1")
            self.assertEqual(s["choices_ko"][s["correct_index"]], "A")
            self.assertEqual([c.upper() for c in s["choices_en"]], s["choices_ko"])
            positions.add(s["correct_index"])
        self.assertEqual(positions, {0, 1, 2, 3})


if __name__ == "__main__":
    unittest.main()
