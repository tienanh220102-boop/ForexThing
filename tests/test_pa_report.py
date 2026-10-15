from datetime import datetime, timezone
import unittest
from unittest.mock import patch
from test_recovery import T, bar, order
import pa_ledger as ledger
import pa_report as report


def closed(o, bars, ts=T+15, msg=None):
    r = ledger.new_record(o, ts, 'spot', cost_price=0.4)
    ledger.advance(r, bars, bars[-1]['t']+60, 'spot')
    r['message_id'] = msg
    return r


def tp_rec(ts=T+15, msg=11):
    b0 = (int(ts)//60+1)*60
    return closed(order(), [bar(t=b0), bar(t=b0+60, h=102.5)], ts, msg)


def sl_rec(ts=T+15, msg=12):
    b0 = (int(ts)//60+1)*60
    return closed(order(), [bar(t=b0), bar(t=b0+60, l=97.5)], ts, msg)


class MessageTests(unittest.TestCase):
    def test_tp_and_sl_are_scored_with_running_month(self):
        tp, sl = tp_rec(), sl_rec(T+3600)
        self.assertEqual((tp['outcome'], sl['outcome']), ('TP1', 'SL'))
        state = {'signals': [tp, sl]}
        msg = report.result_message(sl, state)
        self.assertIn('DÍNH SL', msg); self.assertIn('-1.20R', msg)
        self.assertIn('Luỹ kế tháng 06/2026: 1W/1L', msg)
        self.assertIn('CHẠM TP1', report.result_message(tp, state))

    def test_nofill_is_not_win_or_loss(self):
        r = ledger.new_record(order(entry_type='limit', entry=99, limit_ttl=120), T+15, 'spot')
        ledger.advance(r, [bar(t=T+60), bar(t=T+120)], T+400, 'spot')
        self.assertEqual(r['outcome'], 'NOFILL')
        msg = report.result_message(r, {'signals': [r]})
        self.assertIn('KHÔNG KHỚP', msg); self.assertIn('0W/0L', msg)


class WeeklyTests(unittest.TestCase):
    def test_saturday_schedule_once_per_week(self):
        thu = datetime(2026, 10, 15, 4, tzinfo=timezone.utc).timestamp()
        sat = datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp()
        last = datetime(2026, 10, 13, 4, 45, tzinfo=timezone.utc).timestamp()
        self.assertIsNone(report.weekly_due(last, thu))
        self.assertEqual(report.weekly_due(last, sat + 900), sat)
        self.assertIsNone(report.weekly_due(sat + 900, sat + 7200))

    def test_week_and_month_windows(self):
        state = {'signals': [tp_rec(), sl_rec(T+86400), tp_rec(T-86400)]}  # last one: 31/05
        end = report.week_end(T+86400*5)                                     # Sat 06/06
        w = report.period_stats(state, end - report.WEEK, end)
        m = report.period_stats(state, report.month_start(end-1), end)
        self.assertEqual((w['n'], m['n']), (3, 2))   # week 30/05-05/06 spans May
        msg = report.weekly_message(state, end)
        self.assertIn('Tháng 05/2026 (chốt cả tháng)', msg)
        self.assertIn('Thắng 1/2 (50%)', msg)


class TierTests(unittest.TestCase):
    def test_precedence_matches_sizing(self):
        self.assertEqual(report.tier_of({'probe': True, 'high_conviction': True}), 3)
        self.assertEqual(report.tier_of({'danger': True}), 3)
        self.assertEqual(report.tier_of({'high_conviction': True}), 1)
        self.assertEqual(report.tier_of({}), 2)

    def test_tier_saved_shown_and_split(self):
        b0 = (int(T+15)//60+1)*60
        t1 = closed(order(tier=1), [bar(t=b0), bar(t=b0+60, h=102.5)])
        t3 = closed(order(tier=3), [bar(t=b0), bar(t=b0+60, l=97.5)])
        old = tp_rec()                                   # recorded before tiers existed
        self.assertEqual((t1['tier'], t3['tier']), (1, 3))
        state = {'signals': [t1, t3, old]}
        self.assertIn('Cấp 1 (mạnh)', report.result_message(t1, state))
        msg = report.weekly_message(state, report.week_end(T+86400*5))
        self.assertIn('• Cấp 1 (mạnh): phát 1 · khớp 1 · 1W/0L', msg)
        self.assertIn('• Cấp 3 (dò đường): phát 1 · khớp 1 · 0W/1L', msg)
        self.assertIn('• Chưa phân cấp: phát 1', msg)
        self.assertNotIn('Cấp 2', msg)

    def test_signal_message_has_tier(self):
        import gold_pa_bot as pa
        with patch.object(pa.fx, 'send_telegram', return_value={'ok': True}) as send:
            pa.send_signal(dict(order(tier=3), stars=3), 'Asia', datetime.fromtimestamp(T, timezone.utc))
        self.assertIn('<b>Cấp 3 (dò đường)</b> - trend chưa', send.call_args.args[0])


class NotifyTests(unittest.TestCase):
    def test_reply_fallback_and_no_resend(self):
        import gold_pa_bot as pa
        state = {'signals': [tp_rec(msg=77)]}
        replies = [{'ok': False}, {'ok': True, 'result': {'message_id': 90}}]
        with patch.object(pa.fx, 'send_telegram', side_effect=replies) as send:
            self.assertEqual(pa.notify_results(state), 1)
        self.assertEqual(send.call_args_list[0].kwargs['reply_to'], 77)
        self.assertEqual(state['signals'][0]['result_message_id'], 90)
        with patch.object(pa.fx, 'send_telegram') as send:
            self.assertEqual(pa.notify_results(state), 0); send.assert_not_called()

    def test_failure_retries_next_run(self):
        import gold_pa_bot as pa
        state = {'signals': [tp_rec()]}
        with patch.object(pa.fx, 'send_telegram', return_value={'ok': False}):
            pa.notify_results(state)
        self.assertNotIn('result_sent_at', state['signals'][0])


if __name__ == '__main__':
    unittest.main()
