"""금액 원문 유지 지시가 최초 생성·번역·재시도·보정에 빠지지 않아야 한다."""
import unittest

import amount_repair
import briefing
import llm_expand
import llm_ranker
import llm_translate


class MoneyCopyPolicyTests(unittest.TestCase):
    def test_generation_and_repair_prompts_keep_original_monetary_notation(self):
        prompts = {
            'rank': llm_ranker._system_prompt(),
            'rank_light': llm_ranker._system_prompt_light(),
            'translate_ko': llm_translate._SYS['ko'],
            'translate_en': llm_translate._SYS['en'],
            'expand': llm_expand._SYS,
            'expand_thin': llm_expand._SYS_THIN,
            'expand_retry': llm_expand._SYS_KO_RETRY,
            'expand_keep': llm_expand._SYS_KO_KEEP,
            'weekly': briefing._SYSTEM,
            'daily': briefing._SYSTEM_DAILY,
            'weekly_retry': briefing._SYSTEM_WEEKLY_KO,
            'highlights': briefing._system_highlights(10),
            'quiz': briefing._SYSTEM_QUIZ,
            'repair_article': amount_repair.SYSTEM,
            'repair_brief': amount_repair._BRIEF_SYSTEM,
        }
        for name, prompt in prompts.items():
            with self.subTest(name=name):
                self.assertIn('Copy monetary expressions verbatim', prompt)
                self.assertIn('₹14,500 crore', prompt)
                self.assertIn('Rp 831.55 billion', prompt)
                self.assertIn('headline-length', prompt)
                self.assertNotIn('exact Korean amount notations', prompt)
                self.assertNotIn('form from the provided hint list', prompt)


if __name__ == '__main__':
    unittest.main()
