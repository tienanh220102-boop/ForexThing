import copy
from datetime import datetime, timezone
from unittest.mock import patch
import unittest
from test_recovery import T, bar, order
import execution_rules as rules
import pa_ledger as ledger


def guarded(**kw):
    p = order(structure=98.2, level=99, signal_bar_t=T-3600, **kw)
    p, reason = rules.prepare(p, 2, cost=.1)
    assert reason is None, reason
    return p


class EntryRulesTests(unittest.TestCase):
    def test_net_rr_is_not_net_r(self):
        self.assertAlmostEqual(rules.net_reward_risk(order(), 100, .4), 1.6/2.4)

    def test_target_stops_before_nearest_structure_and_rejects_poor_rr(self):
        p = order(structure=98, level=99, tp1=104, tp2=106, setup='breakout')
        got, reason = rules.prepare(p, 1, ({'swing_hi': 103.5}, {'swing_hi': 103}), cost=.1)
        self.assertIsNone(reason); self.assertAlmostEqual(got['tp1'], 102.85)
        got, reason = rules.prepare(p, 1, ({'swing_hi': 100.5},), cost=.1)
        self.assertIsNone(got); self.assertEqual(reason, 'INSUFFICIENT_NET_REWARD')

    def test_no_geometry_repair_after_structure_crossed(self):
        p = order(structure=101)
        self.assertEqual(rules.prepare(p, 1)[1], 'STRUCTURE_ALREADY_BROKEN')

    def test_quote_rejects_lost_level_chase_stale_source_age_and_low_rr(self):
        p = guarded()
        cases = [(99, T, T+30, 'LEVEL_NOT_RECLAIMED'),
                 (100.6, T, T+30, 'ENTRY_TOO_FAR_FROM_LEVEL'),
                 (100, T, T+91, 'STALE_QUOTE'),
                 (100, T+2800, T+2800, 'STALE_SETUP')]
        for price, qt, now, expected in cases:
            self.assertEqual(rules.validate_quote(p, price, qt, now)[1], expected)
        self.assertEqual(rules.validate_quote(p, 100, T, T+30, source='future', expected_source='spot')[1], 'QUOTE_SOURCE_CHANGED')
        self.assertEqual(rules.validate_quote(p, 100.4, T, T+30)[1], 'INSUFFICIENT_NET_REWARD')

    def test_valid_quote_reprices_without_moving_stops_or_anchors(self):
        p = guarded()
        got, reason = rules.validate_quote(p, 99.9, T, T+30)
        self.assertIsNone(reason); self.assertEqual(got['entry'], 99.9)
        self.assertEqual(got['analysis_entry'], 100); self.assertEqual(got['sl'], p['sl'])
        self.assertEqual(p['entry'], 100)

    def test_fresh_quote_called_before_transport_and_rejection_does_not_send(self):
        import gold_pa_bot as pa
        with patch.object(pa.fx, 'fetch_twelve_bars', return_value=([bar(t=T,c=98.9,l=98)], {'source':'spot'})), \
             patch.object(pa, 'datetime') as clock, patch.object(pa, 'send_signal') as send:
            clock.now.return_value = datetime.fromtimestamp(T+30, timezone.utc)
            result, rec = pa.deliver_paper_order(guarded(), 'Asia', 'spot', T)
        self.assertFalse(result['ok']); self.assertIsNone(rec); send.assert_not_called()

    def test_buy_sell_round_wall_symmetry(self):
        import gold_pa_bot as pa
        buy=pa.build_order(order(structure=98,level=99),1,[{'price':103,'strength':4},{'price':101.5,'strength':4}])
        sell=pa.build_order(order(dir='SELL',structure=102,level=101),1,[{'price':97,'strength':4},{'price':98.5,'strength':4}])
        self.assertAlmostEqual(buy['tp1'],200-sell['tp1'])


class ExecutionLifecycleTests(unittest.TestCase):
    def rec(self, **kw):
        return ledger.new_record(guarded(**kw), T+15, 'spot', cost_price=.1)

    def step(self,r,bars,**kw):
        ledger.advance(r,bars,bars[-1]['t']+60,'spot',**kw)

    def test_late_fill_rechecks_rr(self):
        r=self.rec(); self.step(r,[bar(o=100.4,h=100.5,l=100.2,c=100.3)])
        self.assertEqual(r['outcome'],'CANCELLED_LOW_RR'); self.assertNotIn('filled_ts',r)

    def test_pending_open_invalidation_before_touch(self):
        r=self.rec(entry_type='limit'); self.step(r,[bar(o=97,h=101,l=96,c=100)])
        self.assertEqual(r['outcome'],'CANCELLED_STRUCTURE')
        self.assertNotIn('filled_ts',r)

    def test_fill_then_stop_cannot_be_cancelled_from_later_low(self):
        r=self.rec(entry_type='limit'); self.step(r,[bar(o=101,h=101.5,l=97,c=98)])
        self.assertEqual(r['outcome'],'SL')

    def test_context_change_at_next_bar_never_cancels_prior_fill(self):
        r=self.rec(entry_type='limit')
        bars=[bar(o=100,h=101,l=99.9,c=100.5),bar(t=T+120,o=100.5,h=101,l=100,c=100.5)]
        self.step(r,bars,cancellation_events=[{'ts':T+120}])
        self.assertEqual(r['status'],'OPEN')
        r=self.rec(entry_type='limit')
        self.step(r,[bar(o=101,h=101.5,l=100.5,c=101),bars[-1]],cancellation_events=[{'ts':T+120}])
        self.assertEqual(r['outcome'],'CANCELLED_CONTEXT')

    def test_target_passed_without_fill_cancels_before_future_retest(self):
        r=self.rec(entry_type='limit')
        self.step(r,[bar(o=101,h=102.5,l=100.5,c=101.5),bar(t=T+120,l=97)])
        self.assertEqual(r['outcome'],'CANCELLED_TARGET'); self.assertNotIn('filled_ts',r)

    def test_target_and_fill_same_bar_remains_unknown(self):
        r=self.rec(entry_type='limit'); self.step(r,[bar(o=101,h=103,l=99,c=102)])
        self.assertEqual(r['outcome'],'AMBIGUOUS_CANCEL_FILL')
        self.assertNotIn('filled_ts',r)

    def test_fill_drift_keeps_original_analysis_anchor(self):
        p=guarded(tp1=104,tp2=105)
        p['analysis_entry']=100; p['entry']=100.3
        r=ledger.new_record(p,T+15,'spot',cost_price=.1)
        self.step(r,[bar(o=100.6,h=101,l=100.5,c=100.7)])
        self.assertEqual(r['outcome'],'CANCELLED_PRICE_MOVED')

    def test_setup_expiry_rechecked_after_slow_delivery(self):
        r=ledger.new_record(guarded(),T+2701,'spot',cost_price=.1)
        self.step(r,[bar(t=T+2760)])
        self.assertEqual(r['outcome'],'CANCELLED_STALE_SETUP')

    def test_spread_cancellation_barrier_distinct_from_exit_bid(self):
        r=ledger.new_record(guarded(entry_type='limit'),T+15,'spot',cost_price=0,execution={'spread':.4})
        self.step(r,[bar(o=101,h=102.1,l=99.7,c=100.5)])
        self.assertEqual(r['outcome'],'AMBIGUOUS_CANCEL_FILL')

    def test_rule_snapshot_and_setup_ttl(self):
        p=guarded(entry_type='limit'); r=ledger.new_record(p,T,'spot')
        p['min_net_rr']=999
        self.assertEqual(r['min_net_rr'],.8); self.assertEqual(r['expires_at'],T+7200)
        old=ledger.new_record(order(),T,'spot')
        self.assertNotIn('rule_version',old); self.assertEqual(old['expires_at'],T+21600)

    def test_explicit_spread_and_slip_not_charged_twice(self):
        r=ledger.new_record(order(tp1=110,tp2=111),T+15,'spot',cost_price=0,
                            execution={'spread':.4,'slippage':.1})
        self.step(r,[bar(o=100,h=101,l=99,c=100)])
        self.assertAlmostEqual(r['entry'],100.3)
        self.step(r,[bar(t=T+120,o=98,h=99,l=97,c=98)])
        self.assertAlmostEqual(r['events'][-1]['price'],97.7)
        self.assertAlmostEqual(r['net_r'],(97.7-100.3)/2.3)

    def test_fill_rr_does_not_subtract_aggregate_spread_again(self):
        p=guarded(); p['risk_cost_price']=.4
        r=ledger.new_record(p,T+15,'spot',cost_price=0,execution={'spread':.2})
        self.step(r,[bar(o=100,h=101,l=99.8,c=100.5)])
        # (102-100.1)/(100.1-98)=.9048 passes .8; charging .4 again fails.
        self.assertEqual(r['status'],'OPEN')

    def test_limit_touch_uses_ask_for_buy(self):
        r=ledger.new_record(order(entry_type='limit'),T+15,'spot',cost_price=0,execution={'spread':.4})
        self.step(r,[bar(o=101,h=101.5,l=99.9,c=101)])
        self.assertEqual(r['status'],'PENDING')

    def test_delay_skips_prices_unavailable_before_entry(self):
        r=ledger.new_record(order(),T+15,'spot',execution={'delay_seconds':120})
        self.step(r,[bar(t=T+60,h=105,l=95),bar(t=T+120,h=105,l=95),bar(t=T+180)])
        self.assertEqual(r['filled_ts'],T+180); self.assertEqual(r['status'],'OPEN')

    def test_time_stop_uses_complete_h1_and_same_result_in_chunks(self):
        r=ledger.new_record(order(),T-1,'spot',policy='time_stop',cost_price=0)
        bars=[bar(t=T+i*3600) for i in range(6)]
        other=copy.deepcopy(r)
        ledger.advance(r,bars,T+21600,'spot',interval=3600)
        for b in bars: ledger.advance(other,[b],b['t']+3600,'spot',interval=3600)
        self.assertEqual(r,other); self.assertEqual(r['outcome'],'TIME_STOP')

    def test_trailing_only_ratchets_after_closed_hour(self):
        r=ledger.new_record(order(tp1=102,tp2=110),T-1,'spot',policy='structure_trail',cost_price=0)
        bars=[bar(t=T,o=100,h=101,l=99,c=100.5),bar(t=T+3600,o=100.5,h=102,l=100,c=101),
              bar(t=T+7200,o=101,h=103,l=100.5,c=102.5)]
        ledger.advance(r,bars,T+10800,'spot',interval=3600)
        self.assertEqual(r['status'],'OPEN'); self.assertTrue(r['trailing_active'])
        self.assertAlmostEqual(r['active_sl'],98.8)
        ledger.advance(r,[bar(t=T+10800,o=102,h=103,l=98,c=99)],T+14400,'spot',interval=3600)
        self.assertEqual(r['outcome'],'TRAIL')


if __name__ == '__main__':
    unittest.main()
