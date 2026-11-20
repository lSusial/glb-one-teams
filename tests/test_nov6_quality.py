"""11/6 운영 감사 사례: 환율 관계·기간·언어·발표 중복·실제 노출 집계."""
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import export_json as E
import numeric_guard as N
import quality_report
import llm_translate
import text_quality


class Nov6QualityTests(unittest.TestCase):
    def test_dollar_amount_with_korean_particle_is_not_missing(self):
        source={'summary':'Brent fell to $93.82 a barrel.'}
        for particle in ('를','에서','까지'):
            self.assertEqual(N.usd_values('$93.82'+particle+' 기록했다.'),[93.82])
        self.assertFalse(N.bilingual_amount_issues(source,{'summary':'배럴당 $93.82를 기록했다.'}))
        self.assertTrue(N.bilingual_amount_issues(source,{'summary':'배럴당 $938.2를 기록했다.'}))
        self.assertEqual(N.usd_values('$186.5억'),[186.5e8])

    def article_db(self, title='Deposit competition grows', ko='추세가 強ま면서 경쟁이 커졌다.'):
        c=sqlite3.connect(':memory:'); c.row_factory=sqlite3.Row; self.addCleanup(c.close)
        c.executescript((Path(__file__).parents[1]/'schema.sql').read_text())
        c.execute("INSERT INTO media_sources(source_id,media_name,primary_country_code,language,tier) VALUES(1,'News','US','en',1)")
        c.execute("INSERT INTO media_source_feeds(feed_id,source_id,feed_url,feed_section) VALUES(1,1,'https://x/rss','main')")
        c.execute("""INSERT INTO articles_raw(article_id,source_id,feed_id,title,title_en,title_ko,summary_en,summary_ko,
            link,content_hash,published_at,ai_score) VALUES(1,1,1,?,?,'기존 제목','Deposit competition grows.',?,'https://x/a','h',?,70)""",
            (title,title,ko,date.today().isoformat()))
        c.commit()
        return c

    def translate(self,c,responses):
        class Provider:
            def __init__(self):self.requests=[]
            def complete_json_batch(self,requests):
                self.requests.extend(requests)
                return {r[0]: responses.pop(0) for r in requests}
        p=Provider()
        with patch.object(llm_translate,'_long_window_visible_ids',return_value=set()), \
             patch.object(E,'country_watch_candidate_ids',return_value={}):
            result=llm_translate.run_translate(c,p)
        return result,p

    def test_cached_kana_translation_is_repaired_and_good_title_kept(self):
        c=self.article_db()
        result,p=self.translate(c,[{'title_ko':'바뀐 제목','summary':'예금 경쟁이 커졌다.'}])
        self.assertEqual(result['ko'],1)
        r=c.execute('SELECT summary_ko,title_ko FROM articles_raw').fetchone()
        self.assertEqual(tuple(r),('예금 경쟁이 커졌다.','기존 제목'))
        self.assertEqual(len(p.requests),1)

    def test_failed_retranslation_keeps_cached_fields_after_one_retry(self):
        c=self.article_db(); before=tuple(c.execute('SELECT summary_ko,title_ko FROM articles_raw').fetchone())
        bad={'title_ko':'변경','summary':'추세가 強ま면서 경쟁이 커졌다.'}
        result,p=self.translate(c,[bad,bad])
        self.assertEqual(result['ko'],0)
        self.assertEqual(len(p.requests),2)
        self.assertEqual(tuple(c.execute('SELECT summary_ko,title_ko FROM articles_raw').fetchone()),before)

    def test_period_retranslation_keeps_good_summary_and_uses_canonical_title(self):
        c=self.article_db("IHSG down 16.7% in October",ko='기존 정상 요약이다.')
        c.execute("UPDATE articles_raw SET title_ko='IHSG 16.7% 급락'")
        result,p=self.translate(c,[{'title_ko':'IHSG 16.7% 급락','summary':'교체하면 안 된다.'},
                                  {'title_ko':'IHSG 10월 16.7% 하락','summary':'교체하면 안 된다.'}])
        self.assertEqual(result['ko'],1)
        self.assertEqual(c.execute('SELECT summary_ko FROM articles_raw').fetchone()[0],'기존 정상 요약이다.')
        self.assertIn('in October',p.requests[0][2])

    def test_valid_cached_translation_does_not_call_model(self):
        c=self.article_db(ko='정상 요약이다.')
        result,p=self.translate(c,[])
        self.assertEqual(result['total'],0)
        self.assertEqual(p.requests,[])

    def test_fx_repair_revalidates_brief_and_keeps_english(self):
        import amount_repair
        c=self.article_db()
        en='The rupee slid to 95.22 per dollar.'
        c.execute("INSERT INTO country_briefings(cc,briefing_date,briefing_type,summary,summary_en) VALUES('IN','2026-11-02','weekly',?,?)",
                  ('루피가 95.22달러 수준까지 하락했다.',en))
        class Provider:
            def complete_json(self,*args,**kw):return {'summary':'루피가 달러당 95.22루피 수준까지 하락했다.'}
        result=amount_repair.repair(c,{'briefs':[['IN','weekly']]},provider=Provider())
        row=c.execute('SELECT summary,summary_en FROM country_briefings').fetchone()
        self.assertIn('달러당 95.22루피',row['summary'])
        self.assertEqual(row['summary_en'],en)
        self.assertTrue(any('수정' in x for x in result))

    def test_period_validation_allows_omission_of_number_and_point_in_time_rate(self):
        self.assertEqual(text_quality.period_issues('IHSG fell 16.7% in October','IHSG 하락'),[])
        self.assertEqual(text_quality.period_issues('Bank raised the rate to 4% in November','은행 금리 4%'),[])
        self.assertEqual(text_quality.period_issues('IHSG fell 16.7% in October','다른 지수 116.7% 상승'),[])

    def test_quarter_and_comparison_basis_survive_percentage_headlines(self):
        en='Profit grew 5% quarter-on-quarter in Q3.'
        self.assertTrue(text_quality.period_issues(en,'이익 5% 증가'))
        self.assertEqual(text_quality.period_issues(en,'3분기 이익 전분기 대비 5% 증가'),[])

    def test_fx_quote_cannot_become_dollars(self):
        en = 'The rupee slid for three sessions to 95.22 per dollar.'
        self.assertTrue(N.amount_mismatch(en, '루피가 95.22달러 수준까지 밀렸다.'))
        self.assertFalse(N.amount_mismatch(en, '루피가 달러당 95.22루피로 하락했다.'))
        self.assertEqual(N.bilingual_amount_issues({'summary': en}, {'summary': '루피가 달러당 95.22루피로 하락했다.'}),[])
        self.assertTrue(N.bilingual_amount_issues({'summary': en}, {'summary': '루피가 95.22달러로 하락했다.'}))

    def test_fx_quote_does_not_reject_a_separate_oil_price(self):
        en = 'The rupee slid to 95.22 per dollar. Oil cost $94 a barrel.'
        self.assertFalse(N.amount_mismatch(en, '루피는 달러당 95.22루피, 유가는 배럴당 $94다.'))
        self.assertFalse(N.amount_mismatch('Oil rose to $95.22 a barrel.', '유가가 배럴당 $95.22로 올랐다.'))

    def test_monthly_percentage_and_kana_in_title_are_rejected(self):
        import llm_ranker
        en = "Jakarta's IHSG down 16.7% in October"
        self.assertEqual(llm_ranker._checked_title_ko('IHSG 16.7% 급락', '', en, ''), '')
        self.assertEqual(llm_ranker._checked_title_ko('IHSG 10월 16.7% 하락', '', en, ''), 'IHSG 10월 16.7% 하락')
        self.assertEqual(llm_ranker._checked_title_ko('예금 경쟁 強まる', '', 'Deposit competition grows', ''), '')

    def row(self, aid, title, summary='', day='2026-11-05'):
        return dict(article_id=aid,title=title,title_en=title,title_ko='',summary_en=summary,
                    published_at=day,ai_score=70,tier=1,cc='US')

    def test_same_daily_jobless_release_is_folded(self):
        a=self.row(1,'US weekly jobless claims at 57-year lows amid low layoffs')
        b=self.row(2,'U.S. Jobless Claims Slide Lower Again')
        a['cc']='GLOBAL'; a['primary_country']='US'
        reps,_,members=E._dedup_country_feed([a,b],{})
        self.assertEqual(len(reps),1)
        self.assertEqual(len(members[reps[0]['article_id']]),1)
        b['title_en']='U.S. continuing jobless claims fall'
        self.assertEqual(len(E._dedup_country_feed([a,b],{})[0]),2)

    def test_cyber_warning_requires_the_same_named_campaign(self):
        a=self.row(1,'CERT-In warns of AI cyberattacks',"An advisory names a group it calls 'Crafty Falcon'.")
        b=self.row(2,'CERT-In warns banks of fund transfer fraud',"The advisory identifies the campaign 'Crafty Falcon'.")
        self.assertTrue(E._same_release(a,b))
        self.assertEqual(len(E._dedup_country_feed([a,b],{})[0]),1)
        b['summary_en']='An advisory warns of a campaign, dubbed "Crafty Falcon", against banks.'
        self.assertTrue(E._same_release(a,b))
        self.assertEqual(len(E._dedup_country_feed([a,b],{})[0]),1)
        b['summary_en']="An advisory identifies the campaign 'Other Falcon'."
        self.assertFalse(E._same_release(a,b))

    def test_rate_decision_fold_requires_same_day_bank_action_and_target(self):
        a=self.row(1,'Bank of England raises interest rates to 4% in bid to tame inflation')
        b=self.row(2,'Bank of England raises rates for first time in three years, signals more may follow',
                   'The Bank of England raised its benchmark interest rate to 4%.')
        self.assertEqual(len(E._dedup_country_feed([a,b],{})[0]),1)
        for other in (self.row(3,'Bank of England raises rates to 4.25%'),
                      self.row(4,'Bank of Japan raises rates to 4%'),
                      self.row(5,b['title'],b['summary_en'],day='2026-11-04')):
            # This guard must not label these a same-release pair, even if a generic similarity rule exists.
            self.assertFalse(E._same_release(a,other))
            self.assertEqual(len(E._dedup_country_feed([a,other],{})[0]),2)
        b=self.row(2,'Bank of England raises rates for first time since 2023, signals more hikes may follow')
        self.assertEqual(len(E._dedup_country_feed([a,b],{})[0]),1)

    def test_informationless_index_recap_is_not_a_news_card(self):
        a=self.row(1,'How major US stock indexes fared Wednesday 11/4/2026',
                   'The source excerpt contains only a headline; closing figures are not available.')
        self.assertEqual(E._dedup_country_feed([a],{})[0],[])
        a['summary_en']='The S&P 500 rose 1.44% to close at 7,700.'
        self.assertEqual(len(E._dedup_country_feed([a],{})[0]),1)

    def test_quality_uses_content_date_to_match_snapshot_and_real_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); c=sqlite3.connect(':memory:'); c.row_factory=sqlite3.Row
            self.addCleanup(c.close)
            c.executescript((Path(__file__).parents[1]/'schema.sql').read_text())
            c.execute("INSERT INTO media_sources(source_id,media_name,primary_country_code,language,tier) VALUES(1,'News','US','en',1)")
            c.execute("INSERT INTO media_source_feeds(feed_id,source_id,feed_url,feed_section) VALUES(1,1,'https://x/rss','main')")
            today=date.today().isoformat()
            c.execute("INSERT INTO articles_raw(article_id,source_id,feed_id,title,link,content_hash,published_at,ai_score) VALUES(1,1,1,'News','https://x/a','h',?,70)",(today,))
            (p/'countries.json').write_text(json.dumps({'snapshot_date':(date.today()+timedelta(days=1)).isoformat(),
                'content_date':today,'countries':[{'cc':'US','articles':[]}]}))
            us=next(x for x in quality_report.build_report(c,export_dir=p)['countries'] if x['cc']=='US')
            self.assertEqual(us['displayed'],0)


if __name__=='__main__':
    unittest.main()
