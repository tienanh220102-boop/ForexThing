"""Test cac nang cap BOT: hash nghien cuu, gia han 60 ngay, canh bao mu du lieu, SPRT + snapshot."""
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_recovery import T, bar, order
from test_pa_report import tp_rec, sl_rec
import pa_controls as controls
import pa_ledger as ledger
import pa_notify
import pa_report as report

DAY = 86400
OPEN_TS = T + 10 * 3600            # thu Hai 10:00 UTC - thi truong mo
SAT_TS = T + 5 * DAY + 3600        # thu Bay - dong cua


def ok(mid=1):
    return {'ok': True, 'result': {'message_id': mid}}


class HashTests(unittest.TestCase):
    def test_hash_covers_trading_logic_not_report_text(self):
        names = set(controls.HASHED_FILES)
        for f in ('gold_pa_bot.py', 'pa_ledger.py', 'pa_controls.py', 'execution_rules.py',
                  'market_data.py', 'news_calendar.py', 'pa_observations.py', 'forex_notifier.py'):
            self.assertIn(f, names)
        self.assertNotIn('pa_report.py', names)
        self.assertNotIn('pa_notify.py', names)

    def test_report_text_change_keeps_hash_logic_change_resets(self):
        content = {}
        read = []

        def fake(self):
            read.append(self.name)
            return content.get(self.name, self.name.encode())
        with patch.object(controls.Path, 'read_bytes', autospec=True, side_effect=fake):
            base = controls.configuration_hash('root')
            content['pa_report.py'] = b'chu moi'
            content['pa_notify.py'] = b'chu moi'
            self.assertEqual(controls.configuration_hash('root'), base)
            content['gold_pa_bot.py'] = b'logic moi'
            self.assertNotEqual(controls.configuration_hash('root'), base)
        self.assertNotIn('pa_report.py', read)
        self.assertNotIn('pa_notify.py', read)


class StudyExtensionTests(unittest.TestCase):
    def rec(self, sid, outcome='TP1', net_r=1.0, closed=T + DAY, setup='sweep_reclaim'):
        r = dict(ledger.new_record(order(setup=setup), T + 60, 'spot'), risk_units=1)
        r.update(study_id=sid, status='CLOSED', outcome=outcome, net_r=net_r, closed_ts=closed)
        return r

    def test_sample_only_extends_and_does_not_block(self):
        state = {}
        sid = controls.study(state, T, 'abc')
        end = T + 60 * DAY
        state['signals'] = [self.rec(sid)]
        controls.study(state, end + 100, 'abc')
        cur, review = state['forward_studies'][-1], state['forward_review']
        self.assertEqual(cur['ends_at'], end + 30 * DAY)
        self.assertEqual(review['readiness'], 'COLLECTING_EXTENDED')
        self.assertEqual(review['ends_at'], end + 30 * DAY)
        self.assertTrue(all(i.startswith('INSUFFICIENT_') for i in cur['extensions'][0]['issues']))
        ext = cur['extensions'][0]
        self.assertEqual((ext['old_ends_at'], ext['new_ends_at'], ext['resolved']), (end, end + 30 * DAY, 1))
        self.assertEqual(ext['per_setup'], {'sweep_reclaim': 1, 'breakout': 0})
        self.assertIsNone(controls.admission(state, order()))
        self.assertEqual(review['plan'], controls.PLAN)
        self.assertEqual(controls.PLAN['purpose'], 'review only; never automatic live approval')
        self.assertFalse(review['live_allowed'])
        # Cung mot ky: goi lai khong gia han them
        controls.study(state, end + 5000, 'abc')
        self.assertEqual(len(cur['extensions']), 1)

    def test_delivery_uses_new_ends_at(self):
        import gold_pa_bot as pa
        state = {}
        sid = controls.study(state, T, 'abc')
        state['signals'] = [self.rec(sid)]
        now = T + 60 * DAY + 100
        controls.study(state, now, 'abc')
        with patch.object(pa, 'datetime') as clock, \
                patch.object(pa, 'send_signal', return_value=ok(5)) as send:
            clock.now.return_value = datetime.fromtimestamp(now, timezone.utc)
            result, record = pa.deliver_paper_order(order(), 'Asia', 'spot', now,
                                                    study_ends_at=state['forward_studies'][-1]['ends_at'])
        self.assertTrue(result['ok'])
        send.assert_called_once()
        self.assertEqual(record['calendar_valid_until'], T + 90 * DAY)

    def test_main_extends_before_admission_and_delivery(self):
        import gold_pa_bot as pa
        src = inspect.getsource(pa.main)
        i_study = src.index('controls.study(')
        self.assertLess(i_study, src.index('controls.admission('))
        self.assertLess(i_study, src.index('deliver_paper_order('))
        # ends_at doc tu study hien tai luc goi, khong cache
        self.assertIn("state['forward_studies'][-1]['ends_at']", src)

    def test_performance_issue_goes_review_due(self):
        state = {}
        sid = controls.study(state, T, 'abc')
        state['signals'] = [self.rec(sid, 'SL', -1.2, T + DAY + i) for i in range(3)]
        controls.study(state, T + 60 * DAY + 100, 'abc')
        review = state['forward_review']
        self.assertEqual(review['readiness'], 'REVIEW_DUE')
        self.assertIn('MEAN_R_NOT_POSITIVE', review['review_blockers'])
        self.assertNotIn('extensions', state['forward_studies'][-1])
        self.assertEqual(controls.admission(state, order()), 'STUDY_REVIEW_DUE')

    def test_drawdown_issue_goes_review_due(self):
        state = {}
        sid = controls.study(state, T, 'abc')
        rows = [self.rec(sid, 'TP1', 10.0, T + DAY)] + [self.rec(sid, 'SL', -1.2, T + 2 * DAY + i) for i in range(5)]
        state['signals'] = rows
        controls.study(state, T + 60 * DAY + 100, 'abc')
        self.assertIn('DRAWDOWN_ABOVE_MAX', state['forward_review']['review_blockers'])
        self.assertEqual(state['forward_review']['readiness'], 'REVIEW_DUE')

    def test_cap_365_days_then_review_due(self):
        state = {}
        sid = controls.study(state, T, 'abc')
        state['signals'] = [self.rec(sid)]
        controls.study(state, T + 400 * DAY, 'abc')
        cur = state['forward_studies'][-1]
        self.assertEqual(cur['ends_at'], T + 365 * DAY)
        self.assertEqual(len(cur['extensions']), 11)       # 60 -> 90 ... 360 -> 365
        self.assertEqual(state['forward_review']['readiness'], 'REVIEW_DUE')
        self.assertEqual(controls.admission(state, order()), 'STUDY_REVIEW_DUE')
        controls.study(state, T + 401 * DAY, 'abc')
        self.assertEqual(len(cur['extensions']), 11)

    def test_extension_message_sent_once_and_retried(self):
        state = {}
        sid = controls.study(state, T, 'abc')
        state['signals'] = [self.rec(sid)]
        controls.study(state, T + 60 * DAY + 100, 'abc')
        fail = Mock(return_value={'ok': False})
        self.assertEqual(pa_notify.notify_extensions(state, fail), 0)
        send = Mock(return_value=ok())
        self.assertEqual(pa_notify.notify_extensions(state, send), 1)
        text = send.call_args.args[0]
        self.assertIn('GIA HẠN', text)
        self.assertIn('1/100 lệnh đã chấm', text)
        self.assertIn(datetime.fromtimestamp(T + 90 * DAY, report.VN_TZ).strftime('%d/%m/%Y'), text)
        self.assertEqual(pa_notify.notify_extensions(state, send), 0)
        self.assertEqual(send.call_count, 1)


class WatchdogTests(unittest.TestCase):
    def test_counts_open_market_alerts_once_on_fourth(self):
        state, send = {}, Mock(return_value=ok())
        for i in range(3):
            pa_notify.data_watchdog(state, OPEN_TS + i * 900, 'MARKET_STALE', send)
        send.assert_not_called()
        self.assertEqual(state['data_watchdog']['consecutive'], 3)
        self.assertEqual(state['data_watchdog']['since'], OPEN_TS)
        pa_notify.data_watchdog(state, OPEN_TS + 3 * 900, 'MARKET_STALE', send, 'nến cuối cách 2.0 giờ')
        self.assertEqual(send.call_count, 1)
        self.assertIn('mù', send.call_args.args[0])
        self.assertIn('4 lần chạy liên tiếp', send.call_args.args[0])
        self.assertTrue(state['data_watchdog']['alerted'])
        pa_notify.data_watchdog(state, OPEN_TS + 4 * 900, 'MARKET_STALE', send)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(state['data_watchdog']['last_reason'], 'MARKET_STALE')

    def test_weekend_not_counted(self):
        state, send = {}, Mock(return_value=ok())
        for i in range(10):
            pa_notify.data_watchdog(state, SAT_TS + i * 900, 'MARKET_STALE', send)
        send.assert_not_called()
        self.assertEqual(state['data_watchdog']['consecutive'], 0)
        fri = T + 4 * DAY
        self.assertFalse(pa_notify.market_closed(fri + 21 * 3600 + 59 * 60))
        self.assertTrue(pa_notify.market_closed(fri + 22 * 3600))
        self.assertTrue(pa_notify.market_closed(T - 3 * 3600 - 60))     # CN 20:59 UTC
        self.assertFalse(pa_notify.market_closed(T - 3 * 3600))         # CN 21:00 UTC

    def test_recovery_message_and_reset(self):
        state, send = {}, Mock(return_value=ok())
        for i in range(4):
            pa_notify.data_watchdog(state, OPEN_TS + i * 900, 'H1_FETCH_FAILED', send)
        pa_notify.data_watchdog(state, OPEN_TS + 8 * 900, None, send)
        self.assertEqual(send.call_count, 2)
        text = send.call_args.args[0]
        self.assertIn('đã có dữ liệu trở lại', text)
        self.assertIn('2 giờ', text)
        wd = state['data_watchdog']
        self.assertEqual((wd['consecutive'], wd['since'], wd['alerted']), (0, None, False))

    def test_good_run_without_alert_is_silent(self):
        state, send = {}, Mock(return_value=ok())
        pa_notify.data_watchdog(state, OPEN_TS, 'MARKET_STALE', send)
        pa_notify.data_watchdog(state, OPEN_TS + 900, None, send)
        send.assert_not_called()
        self.assertEqual(state['data_watchdog']['consecutive'], 0)

    def test_intermittent_rejections_alert_once_per_day(self):
        # Kieu su co 06-12/10: chi run :00 bi tu choi, xen ke run tot
        state, send = {}, Mock(return_value=ok())
        for h in range(12):
            base = OPEN_TS + h * 3600
            pa_notify.data_watchdog(state, base, 'MARKET_STALE', send)
            for k in (1, 2, 3):
                pa_notify.data_watchdog(state, base + k * 900, None, send)
        self.assertEqual(send.call_count, 1)
        self.assertIn('lặp lại', send.call_args.args[0])

    def test_rate_alert_then_outage_sends_no_duplicate(self):
        state, send = {}, Mock(return_value=ok())
        t = OPEN_TS
        for _ in range(5):                       # 5 lan tu choi xen ke
            pa_notify.data_watchdog(state, t, 'MARKET_STALE', send)
            pa_notify.data_watchdog(state, t + 900, None, send)
            t += 1800
        for i in range(5):                       # roi mat du lieu lien tuc
            pa_notify.data_watchdog(state, t + i * 900, 'MARKET_STALE', send)
        self.assertEqual(send.call_count, 1)
        self.assertIn('lặp lại', send.call_args.args[0])
        pa_notify.data_watchdog(state, t + 5 * 900, None, send)
        self.assertEqual(send.call_count, 2)
        self.assertIn('trở lại', send.call_args.args[0])

    def test_send_failure_retries_alert(self):
        state = {}
        send = Mock(side_effect=[{'ok': False}, ok()])
        for i in range(5):
            pa_notify.data_watchdog(state, OPEN_TS + i * 900, 'MARKET_STALE', send)
        self.assertEqual(send.call_count, 2)
        self.assertTrue(state['data_watchdog']['alerted'])

    def test_main_h1_failure_counts_and_saves(self):
        import gold_pa_bot as pa
        state, saved = {}, []
        send = Mock(return_value=ok())
        with patch.object(pa.fx, 'TELEGRAM_TOKEN', 'x'), patch.object(pa.fx, 'TELEGRAM_CHAT', 'y'), \
                patch.object(pa, 'load_state', return_value=state), \
                patch.object(pa, 'save_state', side_effect=lambda s: saved.append(dict(s['data_watchdog']))), \
                patch.object(pa.controls, 'configuration_hash', return_value='h'), \
                patch.object(pa.fx, 'load_price_history'), patch.object(pa.fx, '_price_history', {}), \
                patch.object(pa.fx, 'fetch_ohlcv', return_value=([], [], [], [])), \
                patch.object(pa.fx, 'send_telegram', send), patch.object(pa, 'datetime') as clock:
            for i in range(4):
                clock.now.return_value = datetime.fromtimestamp(OPEN_TS + i * 900, timezone.utc)
                pa.main()
        self.assertEqual([s['consecutive'] for s in saved], [1, 2, 3, 4])
        self.assertEqual(saved[-1]['last_reason'], 'H1_FETCH_FAILED')
        self.assertEqual(send.call_count, 1)
        self.assertIn('không tải được nến H1', send.call_args.args[0])


class FredTests(unittest.TestCase):
    def fetch(self, text=None, exc=None, status=200):
        import forex_notifier as fx
        resp = Mock(text=text, status_code=status)
        resp.raise_for_status = Mock(side_effect=None if status == 200 else Exception('HTTP %d' % status))
        with patch.object(fx.requests, 'get', side_effect=exc, return_value=resp):
            return fx.fetch_tips_real_yield()

    def test_parses_and_drops_holidays(self):
        csv = ('observation_date,DFII10\n2026-09-30,2.80\n2026-10-06,2.91\n2026-10-09,.\n'
               '2026-10-12,\n2026-10-13,2.88\n')
        s = self.fetch(csv)
        self.assertEqual([round(v, 2) for v in s.values], [2.91, 2.88])   # chi 7 ngay cuoi, bo '.' va rong

    def test_network_and_http_errors_are_fail_safe(self):
        import requests
        self.assertTrue(self.fetch(exc=requests.ConnectionError('down')).empty)
        self.assertTrue(self.fetch('x', status=500).empty)
        self.assertTrue(self.fetch('garbage-without-columns').empty)


STATS_KEYS = {'issued', 'filled', 'n', 'wins', 'losses', 'flat', 'tp', 'sl', 'no_trade', 'unscored',
              'total_r', 'avg_r', 'win_rate', 'max_dd_r', 'best_r', 'worst_r'}


class SnapshotTests(unittest.TestCase):
    SPRT = {'verdict': 'CHUA_DU', 'llr': 0.12, 'n': 2, 'upper': 2.9444, 'lower': -2.9444}

    def check_contract(self, snap, now):
        self.assertEqual(set(snap), {'generated_at', 'week', 'month', 'all', 'sprt'})
        self.assertEqual(snap['generated_at'], now)
        for key in ('week', 'month', 'all'):
            per = snap[key]
            self.assertEqual(set(per), {'label', 'start', 'end', 'stats', 'tiers'})
            self.assertEqual(per['end'], now)
            self.assertEqual(set(per['stats']), STATS_KEYS)
            self.assertEqual(set(per['tiers']), {'1', '2', '3', 'none'})
            for s in per['tiers'].values():
                self.assertEqual(set(s), STATS_KEYS)
        self.assertEqual(set(snap['sprt']), {'verdict', 'llr', 'n', 'upper', 'lower'})
        json.dumps(snap, allow_nan=False)

    def test_contract_keys_windows_and_json(self):
        state = {'signals': [tp_rec(), sl_rec(T + 3600), dict(tp_rec(T + 7200), tier=1)]}
        now = T + 2 * DAY
        snap = report.snapshot(state, now, self.SPRT)
        self.check_contract(snap, now)
        self.assertEqual(snap['week']['start'], report.week_end(now))
        self.assertEqual(snap['month']['start'], report.month_start(now))
        self.assertEqual(snap['all']['start'], 0)
        self.assertEqual(snap['all']['label'], 'Từ khi bắt đầu sổ lệnh v2')
        self.assertEqual(snap['month']['label'], 'Tháng 06/2026')
        self.assertEqual(snap['week']['label'], 'Tuần 30/05 - 03/06/2026')
        self.assertEqual(snap['all']['stats']['n'], 3)
        self.assertEqual(snap['all']['tiers']['1']['n'], 1)
        self.assertEqual(snap['all']['tiers']['none']['n'], 2)

    def test_empty_state_serializes(self):
        snap = report.snapshot({}, T, self.SPRT)
        self.check_contract(snap, T)
        self.assertIsNone(snap['all']['stats']['win_rate'])
        self.assertIsNone(snap['all']['stats']['best_r'])

    def test_sprt_summary_and_weekly_line(self):
        import gold_pa_bot as pa
        self.assertEqual(pa.sprt_summary({})['verdict'], 'CHUA_DU')
        state = {'signals': [tp_rec(T + i * 3600) for i in range(3)] + [sl_rec(T + 4 * 3600 + i) for i in range(3)]}
        s = pa.sprt_summary(state)
        rs = [r['net_r'] for r in sorted(ledger.scored(state), key=lambda r: (r['closed_ts'], r['ts']))]
        self.assertEqual(s['n'], 6)
        self.assertEqual(s['verdict'], pa._sprt_verdict(rs)[0])
        self.assertAlmostEqual(s['llr'], pa._sprt_verdict(rs)[1], places=3)
        json.dumps(s, allow_nan=False)
        end = report.week_end(T + 5 * DAY)
        self.assertNotIn('SPRT', report.weekly_message(state, end))
        msg = report.weekly_message(state, end, sprt=s)
        self.assertIn('Kiểm định SPRT', msg)
        self.assertIn('n=6', msg)
        self.assertIn('có bằng chứng hệ thống KHÔNG có lợi thế',
                      report.weekly_message(state, end, sprt=dict(s, verdict='REJECT_EDGE', llr=-3.1)))
        self.assertIn('mới 2 lệnh', report.sprt_line(dict(s, n=2)))

    def test_save_state_writes_snapshot(self):
        import gold_pa_bot as pa
        state = {'signals': [tp_rec()]}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            path = str(Path(d) / 'state.json')
            with patch.object(pa, 'STATE_FILE', path):
                pa.save_state(state)
            with open(path, encoding='utf-8') as f:
                saved = json.load(f)
        self.assertEqual(saved['pa_report']['all']['stats']['n'], 1)
        self.assertIn(saved['pa_report']['sprt']['verdict'], ('CHUA_DU', 'CONFIRM_EDGE', 'REJECT_EDGE'))


class MovedMessageTests(unittest.TestCase):
    def test_signal_message_identical_to_sent_text(self):
        import gold_pa_bot as pa
        p = dict(order(tier=1, entry_type='limit', limit_ttl=7200), stars=4)
        now = datetime.fromtimestamp(T, timezone.utc)
        with patch.object(pa.fx, 'send_telegram', return_value=ok()) as send:
            pa.send_signal(p, 'Asia', now)
        self.assertEqual(send.call_args.args[0], report.signal_message(p, 'Asia', now, pa.PAPER_COST_PRICE))
        self.assertIn('(sau 2 giờ)', send.call_args.args[0])

    def test_notify_results_direct_with_injected_send(self):
        state = {'signals': [tp_rec(msg=77)]}
        send = Mock(return_value=ok(90))
        self.assertEqual(pa_notify.notify_results(state, send, now_ts=T + 999), 1)
        self.assertEqual(send.call_args.kwargs['reply_to'], 77)
        self.assertEqual(state['signals'][0]['result_sent_at'], T + 999)
        self.assertEqual(pa_notify.notify_results(state, send), 0)


if __name__ == '__main__':
    unittest.main()
