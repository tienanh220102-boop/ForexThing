import copy
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import unittest
from test_recovery import T, bar, order
import news_calendar as news
import pa_controls as controls
import pa_observations as obs
import pa_ledger as ledger


def event(ts=T+7200, impact='High', country='USD'):
    return {'date':datetime.fromtimestamp(ts,timezone.utc).isoformat(),
            'impact':impact,'country':country,'title':'Test event'}


class CalendarTests(unittest.TestCase):
    def test_empty_naive_wrong_week_and_bad_row_fail(self):
        for rows in ([],[dict(event(),date='2026-06-01T02:00:00')],
                     [event(T-7*86400)],[event(),dict(event(),impact='???')]):
            with self.assertRaises((ValueError,KeyError)):news.parse_week(rows,T)

    def test_hard_has_priority_over_medium_and_ignores_other_currency(self):
        snap=news.parse_week([event(T+300,'Medium'),event(T+600)],T)
        self.assertEqual(news.evaluate(snap,T)[0],'HARD')
        snap=news.parse_week([event(T+300,country='JPY')],T)
        self.assertEqual(news.evaluate(snap,T)[0],'PASS')

    def test_stale_future_cache_and_week_boundary_block(self):
        snap=news.parse_week([event()],T)
        for ts in (T-1,T+901):self.assertEqual(news.evaluate(snap,ts)[0],'UNKNOWN')
        end=snap['coverage_end'];snap['fetched_at']=end-100
        self.assertEqual(news.evaluate(snap,end-100)[1],'CALENDAR_COVERAGE_GAP')

    def test_http_failure_stale_age_and_invalid_body_fail_closed(self):
        for response in (Mock(status_code=403),Mock(status_code=200,headers={'Age':'901'}),
                         Mock(status_code=200,headers={},json=lambda:[])):
            with patch.object(news.requests,'get',return_value=response):
                self.assertEqual(news.fetch(T)['status'],'UNKNOWN')

    def test_cache_and_dst_week_contract(self):
        snap=news.parse_week([event()],T)
        with patch.object(news.requests,'get') as get:
            self.assertIs(news.fetch(T+10,snap),snap);get.assert_not_called()
        ts=datetime(2026,11,2,tzinfo=timezone.utc).timestamp()
        snap=news.parse_week([event(ts)],ts)
        self.assertEqual(snap['coverage_end']-snap['coverage_start'],169*3600)

    def test_final_calendar_check_never_sends_unknown(self):
        import gold_pa_bot as pa
        from test_execution_rules import guarded
        with patch.object(pa.fx,'fetch_twelve_bars',return_value=([bar(t=T)],{'source':'spot'})), \
             patch.object(pa,'datetime') as clock,patch.object(pa,'send_signal') as send:
            clock.now.return_value=datetime.fromtimestamp(T+30,timezone.utc)
            result,record=pa.deliver_paper_order(guarded(),'Asia','spot',T,{'status':'UNKNOWN'})
        self.assertFalse(result['ok']);self.assertIsNone(record);send.assert_not_called()

    def test_issue_cutoff_and_pending_calendar_expiry(self):
        import gold_pa_bot as pa
        with patch.object(pa,'datetime') as clock,patch.object(pa,'send_signal',return_value={'ok':True}) as send:
            clock.now.return_value=datetime.fromtimestamp(T+30,timezone.utc)
            result,record=pa.deliver_paper_order(order(),'Asia','spot',T,study_ends_at=T+29)
            self.assertFalse(result['ok']);send.assert_not_called()
            result,record=pa.deliver_paper_order(order(entry_type='limit'),'Asia','spot',T,news.parse_week([event()],T),T+86400)
        self.assertTrue(result['ok']);self.assertEqual(record['expires_at'],T+news.TTL)
        bars=[bar(t=t,o=101,h=101.5,l=100.5,c=101) for t in range(T+60,T+960,60)]
        bars.append(bar(t=T+960,o=101,h=101.5,l=99.5,c=100))
        ledger.advance(record,bars,T+1020,'spot')
        self.assertEqual(record['outcome'],'NOFILL');self.assertNotIn('filled_ts',record)


class PortfolioTests(unittest.TestCase):
    def rec(self,**kw):
        return dict(ledger.new_record(order(),T,'spot'),risk_units=1,**kw)

    def test_pending_open_unknown_reserve_closed_and_nontrade_release(self):
        for status,outcome in [('PENDING',None),('OPEN',None),('DATA_GAP','UNKNOWN_DATA_GAP'),('AMBIGUOUS','AMBIGUOUS')]:
            r=self.rec();r.update(status=status,outcome=outcome)
            self.assertTrue(controls.reserved(r))
        for outcome in ledger.SCORED|ledger.NON_TRADES:
            r=self.rec();r.update(status='CLOSED',outcome=outcome)
            self.assertFalse(controls.reserved(r))

    def test_same_direction_gross_and_unknown_caps(self):
        r=self.rec();state={'signals':[r]}
        self.assertEqual(controls.admission(state,order()),'SAME_DIRECTION_EXPOSURE')
        self.assertIsNone(controls.admission(state,order(dir='SELL')))
        r['risk_units']=1.5
        self.assertEqual(controls.admission(state,order(dir='SELL')),'TOTAL_RISK_BUDGET')
        del r['risk_units']
        self.assertEqual(controls.admission(state,order(dir='SELL')),'UNKNOWN_RISK_RESERVATION')
        state['signals']=[self.rec(),self.rec()]
        self.assertEqual(controls.admission(state,order(dir='SELL')),'MAX_ACTIVE_ORDERS')

    def test_shadow_and_legacy_do_not_reserve(self):
        state={'signals':[dict(self.rec(),ledger_version=1)],'observations':[{'shadow':self.rec()}]}
        self.assertEqual(controls.portfolio(state)['active'],0)

    def test_fixed_cutoff_never_extends_and_never_auto_live(self):
        state={};sid=controls.study(state,T,'abc');end=T+60*86400
        r=self.rec();r.update(study_id=sid,status='CLOSED',outcome='TP1',net_r=1,closed_ts=end+10)
        state['signals']=[r];controls.study(state,end+100,'abc')
        review=state['forward_review']
        self.assertEqual(review['cutoff'],end);self.assertEqual(review['metrics']['n'],0)
        self.assertEqual(review['pending_at_cutoff'],1)
        self.assertEqual(review['readiness'],'REVIEW_DUE');self.assertFalse(review['live_allowed'])
        self.assertEqual(controls.admission(state,order()),'STUDY_REVIEW_DUE')
        self.assertNotEqual(controls.study(state,end+100,'changed'),sid)
        self.assertEqual(len(state['forward_studies']),2)


class ObservationTests(unittest.TestCase):
    def test_first_snapshot_immutable_namespace_separate_and_sensitive_fields_removed(self):
        state={};p=order(signal_bar_t=T-3600,level=99,rec_lot=10,risk_pct=2)
        key=obs.observe(state,p,'NEWS',T,'spot',{'study_id':'a'})
        original=copy.deepcopy(state['observations'][0]['order'])
        p['entry']=101
        self.assertEqual(obs.observe(state,p,'ACCEPTED',T+60,'spot',{'study_id':'a'}),key)
        self.assertEqual(state['observations'][0]['order'],original)
        self.assertNotIn('rec_lot',original);self.assertNotIn('risk_pct',original)
        obs.observe(state,p,'NEWS',T+60,'spot',{'study_id':'b'})
        self.assertEqual(len(state['observations']),2)

    def test_never_replay_pre_detection_and_invalid_geometry_unscored(self):
        state={};obs.observe(state,order(),'NEWS',T,'spot',{})
        obs.resolve(state,[bar(t=T-60,h=105,l=95)],T,'spot')
        self.assertFalse(state['observations'][0]['shadow'].get('outcome'))
        obs.observe(state,order(setup='breakout',sl=101),'RR',T,'spot',{})
        self.assertEqual(state['observations'][1]['evaluation'],'NOT_EVALUABLE')

    def test_capacity_latches(self):
        state={}
        with patch.object(obs,'MAX_OBSERVATIONS',0):
            self.assertIsNone(obs.observe(state,order(),'NEWS',T,'spot',{}))
        self.assertTrue(state['audit_capacity_reached'])


if __name__=='__main__':unittest.main()
