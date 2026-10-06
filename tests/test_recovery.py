import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import market_data as md
import pa_ledger as ledger

T = 1780272000  # Monday 2026-06-01 00:00 UTC


def bar(t=T+60, o=100, h=100.5, l=99.5, c=100):
    return {'t': t, 'o': o, 'h': h, 'l': l, 'c': c}


def order(**kw):
    return dict({'setup': 'sweep_reclaim', 'dir': 'BUY', 'entry': 100, 'sl': 98,
                 'tp1': 102, 'tp2': 103, 'entry_type': 'market'}, **kw)


class DataTests(unittest.TestCase):
    def test_future_rejected_not_clipped(self):
        with self.assertRaises(md.DataQualityError):
            md.validate_bars([bar(t=T+36000)], T)

    def test_missing_or_non_utc_metadata_rejected(self):
        values = [{'datetime': '2026-06-01 00:00:00', 'open': '100', 'high': '101', 'low': '99', 'close': '100'}]
        for meta in ({}, {'exchange_timezone': 'Australia/Sydney'}):
            with self.assertRaises(md.DataQualityError):
                md.parse_twelve({'meta': meta, 'values': values}, T+3600)

    def test_utc_metadata(self):
        data = {'meta': {'exchange_timezone': 'UTC'}, 'values': [
            {'datetime': '2026-06-01 00:00:00', 'open': '100', 'high': '101', 'low': '99', 'close': '100'}]}
        got = md.parse_twelve(data, T+3600)
        self.assertEqual(got[0]['t'], T)
        self.assertTrue(got[0]['closed'])
        without_meta = {**data, 'meta': {}}
        self.assertEqual(md.parse_twelve(without_meta, T+3600, requested_timezone='UTC')[0]['t'], T)

    def test_bad_geometry_and_duplicate(self):
        for bars in ([bar(h=99)], [bar(), bar()], [bar(c=float('nan'))]):
            with self.assertRaises(md.DataQualityError):
                md.validate_bars(bars, T+3600)

    def test_legacy_cache_not_trusted(self):
        self.assertFalse(md.usable_cache({'bars': [bar()]}, T+3600))

    def test_h4_excludes_partial_and_gaps(self):
        bars = [bar(t=T+i*3600) for i in range(5)]
        self.assertEqual(len(md.closed_h4(bars, T+5*3600)), 1)
        self.assertEqual(md.closed_h4(bars[1:], T+5*3600), [])


class LedgerTests(unittest.TestCase):
    def rec(self, policy='tp1_full', **kw):
        return ledger.new_record(order(**kw), T+15, 'spot', policy=policy, cost_price=0.4)

    def step(self, r, bars, now=None):
        ledger.advance(r, bars, now or bars[-1]['t']+60, 'spot')

    def test_preexisting_and_future_candles_cannot_score(self):
        r = self.rec()
        self.step(r, [bar(t=T, h=104, l=95), bar(t=T+36000, h=104, l=95)], T+30)
        self.assertEqual(r['status'], 'PENDING')
        self.assertNotIn('outcome', r)

    def test_market_next_minute_open_and_net_cost(self):
        r = self.rec()
        self.step(r, [bar(o=100.2, h=102.5, l=100.1, c=102)])
        self.assertEqual(r['entry'], 100.2)
        self.assertEqual(r['outcome'], 'TP1')
        self.assertAlmostEqual(r['net_r'], (1.8-0.4)/2.2)

    def test_same_bar_sl_tp_unscored(self):
        r = self.rec()
        self.step(r, [bar(h=103, l=97)])
        self.assertEqual(r['outcome'], 'AMBIGUOUS')
        self.assertEqual(ledger.scored({'signals': [r]}), [])

    def test_limit_touch_cannot_fill_after_expiry(self):
        r = self.rec(entry_type='limit')
        r['last_bar_t'] = T+6*3600-60
        self.step(r, [bar(t=T+6*3600+60, h=104, l=97)])
        self.assertNotIn('filled_ts', r)
        self.assertNotIn('net_r', r)

    def test_limit_fill_bar_tp_order_unknown(self):
        r = self.rec(entry_type='limit')
        self.step(r, [bar(o=101, h=103, l=99, c=102)])
        self.assertEqual(r['outcome'], 'AMBIGUOUS')

    def test_limit_fill_bar_sl_can_resolve(self):
        r = self.rec(entry_type='limit')
        self.step(r, [bar(o=101, h=101.5, l=97, c=98)])
        self.assertEqual(r['outcome'], 'SL')

    def test_limit_gap_beyond_sl_invalid_not_win(self):
        r = self.rec(entry_type='limit')
        self.step(r, [bar(o=97, h=100, l=96, c=99)])
        self.assertEqual(r['outcome'], 'INVALID_FILL')

    def test_gap_stop_models_worse_open(self):
        r = self.rec(); self.step(r, [bar()])
        self.step(r, [bar(t=T+120, o=96, h=97, l=95, c=96)])
        self.assertAlmostEqual(r['net_r'], -2.2)

    def test_gap_tp_precedes_later_sl(self):
        r = self.rec(); self.step(r, [bar()])
        self.step(r, [bar(t=T+120, o=103, h=104, l=97, c=100)])
        self.assertEqual(r['outcome'], 'TP1')
        self.assertAlmostEqual(r['net_r'], 0.8)

    def test_expiry_missing_tail_is_not_nofill(self):
        r = self.rec(entry_type='limit'); r['last_bar_t'] = T+21540
        self.step(r, [], T+21630)
        self.assertEqual(r['outcome'], 'DATA_GAP')

    def test_expiry_straddle_touch_unscored(self):
        r = self.rec(entry_type='limit'); r['last_bar_t'] = T+21540
        self.step(r, [bar(t=T+21600, o=101, h=101, l=99, c=100)], T+21660)
        self.assertEqual(r['outcome'], 'UNKNOWN_EXPIRY')

    def test_partial_be_never_looks_backward_in_tp1_bar(self):
        r = self.rec('partial_be')
        self.step(r, [bar(o=100, h=102.5, l=99.5, c=102)])
        self.assertEqual(r['status'], 'OPEN')
        self.assertEqual(r['remaining'], 0.5)
        self.assertTrue(r['be_active'])

    def test_partial_at_open_still_honors_timeout(self):
        r = self.rec('partial_be'); self.step(r, [bar()]); r['timeout_at'] = T+180
        self.step(r, [bar(t=T+120, o=102.1, h=102.5, l=101, c=102.2)])
        self.assertEqual(r['outcome'], 'TIMEOUT')
        self.assertAlmostEqual(r['net_r'], 0.85)

    def test_new_be_not_retroactive(self):
        r = self.rec('early_be')
        self.step(r, [bar(h=101.8, l=99, c=101.2)])
        self.assertEqual(r['status'], 'OPEN')
        self.assertTrue(r['be_active'])
        self.step(r, [bar(t=T+120, o=101.2, h=101.5, l=99.8, c=100)])
        self.assertEqual(r['outcome'], 'BE')
        self.assertAlmostEqual(r['net_r'], -0.2)

    def test_partial_runner_preserved_and_cost_weighted_once(self):
        r = self.rec('partial_be'); self.step(r, [bar()])
        self.step(r, [bar(t=T+120, o=101, h=102.5, l=100.5, c=102)])
        self.assertEqual(r['remaining'], 0.5)
        self.assertNotIn('outcome', r)
        self.step(r, [bar(t=T+180, o=102, h=103.2, l=101, c=103)])
        self.assertEqual(r['outcome'], 'TP2')
        self.assertAlmostEqual(r['net_r'], 1.05)

    def test_wrong_side_sell_sl_rejected(self):
        with self.assertRaises(ValueError):
            self.rec(dir='SELL', sl=99, tp1=98, tp2=97)

    def test_data_gap_and_source_change_unscored(self):
        r = self.rec(); self.step(r, [bar(t=T+180, h=103)])
        self.assertEqual(r['outcome'], 'DATA_GAP')
        r = self.rec(); ledger.advance(r, [bar()], T+120, 'futures')
        self.assertEqual(r['outcome'], 'SOURCE_CHANGED')

    def test_migration_preserves_original_and_is_idempotent(self):
        old = {'signals': [{'outcome': 'SL', 'ts': T}], 'pa_knowledge': {'adjust': {'bad': 1}}}
        backup = copy.deepcopy(old); new = ledger.migrate_state(old)
        self.assertEqual(new['legacy']['state'], backup)
        self.assertEqual(old, backup)
        self.assertEqual(new['signals'], [])
        self.assertEqual(ledger.migrate_state(new), new)

    def test_incremental_replay_idempotent(self):
        r = self.rec(); bars = [bar(), bar(t=T+120, h=102.5, c=102)]
        self.step(r, bars); original = copy.deepcopy(r); self.step(r, bars)
        self.assertEqual(r, original)

    def test_monitor_checks_5_not_50_and_latches(self):
        state = {'signals': [dict(ledger_version=2, status='CLOSED', outcome='SL', net_r=-1,
                                 ts=T+i, closed_ts=T+60+i) for i in range(5)]}
        self.assertTrue(ledger.monitor(state)['review_required'])
        state['signals'] = []
        self.assertTrue(ledger.monitor(state)['review_required'])


class IntegrationTests(unittest.TestCase):
    def recovery(self):
        spec = importlib.util.spec_from_file_location('recovery', ROOT/'scripts/recover_pa_history.py')
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod

    def test_fetch_requests_utc_and_no_gold_futures_fallback(self):
        import forex_notifier as fx
        response = {'meta': {'exchange_timezone': 'UTC'}, 'values': [
            {'datetime': '2026-06-01 00:00:00', 'open': '100', 'high': '101', 'low': '99', 'close': '100'}]}
        with patch.object(fx, 'TWELVE_DATA_KEY', 'test'), patch.object(fx.requests, 'get') as get:
            get.return_value.status_code = 200; get.return_value.json.return_value = response
            fx.fetch_twelve_bars('XAU/USD')
            self.assertEqual(get.call_args.kwargs['params']['timezone'], 'UTC')
        with patch.object(fx, 'TWELVE_DATA_KEY', ''), patch.object(fx.yf, 'Ticker') as ticker:
            self.assertEqual(fx.fetch_ohlcv('XAU/USD', 'GC=F'), (None, None, None, None))
            ticker.assert_not_called()

    def test_history_does_not_force_intra_entry_minute_sl(self):
        mod = self.recovery()
        rec = {**order(), 'ts': T+15}
        self.assertEqual(mod.review_trade(rec, [bar(t=T, l=97)], T+120)['status'], 'UNKNOWN_ENTRY_BAR')
        self.assertEqual(mod.review_trade(rec, [bar(t=T), bar(h=102.5, c=102)], T+120)['status'], 'TP1')

    def test_history_refinement_continues_beyond_first_hour(self):
        mod = self.recovery(); rec = {**order(), 'ts': T+15}
        h1 = [bar(t=T, h=103), bar(t=T+3600, h=103, c=102)]
        # The old H1 high happened before entry; M1 excludes that pre-entry
        # hit and establishes an intact path through the following H1 win.
        def fetch(start, end, interval):
            return [bar(t=T+i*60) for i in range(60)]
        got = mod.recover_one(rec, h1, T+7200, fetch)
        self.assertEqual(got['status'], 'TP1')
        self.assertEqual(got['at'], T+3600)

    def test_history_known_open_precedes_intrabar(self):
        mod = self.recovery(); rec = {**order(), 'ts': T+15}
        for opening, expected in [(103, 'TP1'), (96, 'SL')]:
            got = mod.review_trade(rec, [bar(t=T), bar(o=opening, h=104, l=95)], T+120)
            self.assertEqual(got['status'], expected)

    def test_health_recovers_after_old_gap(self):
        import gold_pa_bot as pa
        state = {'signals': [], 'data_health': {'ok': False}}
        with patch.object(pa.fx, 'fetch_twelve_bars', return_value=([bar()],
                          {'source': 'spot', 'fetched_at': T+90})):
            pa.resolve_signals(state, datetime.fromtimestamp(T+90, timezone.utc))
        self.assertTrue(state['data_health']['ok'])

    def test_slow_delivery_clock_excludes_analysis_and_transport_bars(self):
        import gold_pa_bot as pa
        with patch.object(pa, 'datetime') as clock, patch.object(pa, 'send_signal') as send:
            clock.now.side_effect = [datetime.fromtimestamp(T+95, timezone.utc),
                                     datetime.fromtimestamp(T+125, timezone.utc)]
            send.return_value = {'ok': True, 'result': {'message_id': 7}}
            _, rec = pa.deliver_paper_order(order(), 'EU_US', 'spot', T+15)
        self.assertEqual(rec['ts'], T+125)
        self.assertEqual(rec['not_before'], T+180)
        ledger.advance(rec, [bar(t=T+60, l=97), bar(t=T+120, l=97)], T+180, 'spot')
        self.assertEqual(rec['status'], 'PENDING')

    def test_live_mode_cannot_emit(self):
        import gold_pa_bot as pa
        with patch.object(pa, 'PA_MODE', 'live'), patch.object(pa, 'send_signal') as send:
            pa.main()
        send.assert_not_called()

    def test_history_timeout_boundary_refines_instead_of_late_tp(self):
        mod = self.recovery()
        rec = {**order(), 'ts': T+15}
        bars = [bar(t=T+i*3600) for i in range(120)]
        bars.append(bar(t=T+5*86400, h=104, c=103))
        got = mod.review_trade(rec, bars, T+5*86400+3600, interval=3600)
        self.assertEqual(got['status'], 'UNKNOWN_TIMEOUT_BAR')


if __name__ == '__main__':
    unittest.main()
