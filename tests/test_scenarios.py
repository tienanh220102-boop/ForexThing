"""pa_scenarios: gio dang bang, kich hoat/vo hieu/cham dich tren M15, thong ke, tin nhan."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pa_scenarios as scn  # noqa: E402

DAY = datetime(2026, 10, 14, tzinfo=timezone.utc).timestamp()   # thu Tu


def m15(t, o, h, l, c):
    return {'t': int(t), 'o': o, 'h': h, 'l': l, 'c': c, 'closed': True}


def board_with(sc, atr=10.0):
    return {'name': 'EU', 'day': DAY, 'created_at': DAY + 6.5 * 3600, 'expire': DAY + 12.5 * 3600,
            'atr': atr, 'price': 4100.0, 'range': {'hi': 4110, 'lo': 4090, 'mid': 4100, 'label': 'range phiên Á'},
            'ctx': {'regime': 'RANGE', 'h1': 'RANGE', 'h4': 'UP', 'h1_ok': False, 'h4_ok': True, 'news': [], 'news_ts': None},
            'scenarios': sc, 'status': 'OPEN', 'pick': 'BUY'}


def sweep_buy():
    return {'type': 'quet_dao', 'dir': 'BUY', 'trigger': 'sweep_low', 'level': 4090.0, 'target': 4110.0,
            'stop': 4085.0, 'status': 'WAITING', 'score': 2, 'why': [], 'text': 't', 'id': 'a'}


class DueTests(unittest.TestCase):
    def test_eu_then_us_once_per_day_and_not_weekend(self):
        self.assertIsNone(scn.due_board({}, DAY + 6 * 3600))
        self.assertEqual(scn.due_board({}, DAY + 6.6 * 3600), 'EU')
        st = {'boards': [{'name': 'EU', 'day': DAY}]}
        self.assertIsNone(scn.due_board(st, DAY + 6.7 * 3600))
        self.assertEqual(scn.due_board(st, DAY + 12.6 * 3600), 'US')
        sat = datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp()
        self.assertIsNone(scn.due_board({}, sat + 6.6 * 3600))


class ResolveTests(unittest.TestCase):
    def test_sweep_then_reclaim_triggers_then_hits_target(self):
        b = board_with([sweep_buy()])
        t = b['created_at']
        bars = [m15(t, 4095, 4096, 4087, 4092),     # quet xuong 4087 < 4090-0.5, dong lai tren 4090
                m15(t + 900, 4092, 4104, 4091, 4103),
                m15(t + 1800, 4103, 4111, 4100, 4108)]
        scn.resolve(b, bars, t + 2700)
        self.assertEqual(b['scenarios'][0]['status'], 'HIT')
        self.assertEqual(b['scenarios'][0]['entry'], 4092)

    def test_two_closes_beyond_stop_invalidate_before_trigger(self):
        b = board_with([sweep_buy()])
        t = b['created_at']
        bars = [m15(t, 4089, 4090, 4083, 4084), m15(t + 900, 4084, 4085, 4080, 4082)]
        scn.resolve(b, bars, t + 1800)
        self.assertEqual(b['scenarios'][0]['status'], 'INVALIDATED')

    def test_same_bar_target_and_stop_is_ambiguous(self):
        x = dict(sweep_buy(), status='TRIGGERED', entry=4095)
        b = board_with([x])
        scn.resolve(b, [m15(b['created_at'], 4095, 4112, 4084, 4100)], b['created_at'] + 900)
        self.assertEqual(x['status'], 'AMBIGUOUS')

    def test_expiry_closes_board_and_scores_pick(self):
        b = board_with([sweep_buy()])
        t = b['created_at']
        bars = [m15(t + k * 900, 4100, 4101, 4099, 4100 + k) for k in range(24)]
        self.assertTrue(scn.resolve(b, bars, b['expire'] + 60))
        self.assertEqual(b['status'], 'CLOSED')
        self.assertEqual(b['scenarios'][0]['status'], 'NOT_TRIGGERED')
        self.assertEqual(b['actual_dir'], 'BUY')                  # +23 > 0.25 ATR
        st = scn.stats({'boards': [b]})
        self.assertEqual(st['pick'], {'n': 1, 'right': 1, 'flat': 0})
        self.assertIn('✅ đúng', scn.result_message(b, st))
        json.dumps(b, allow_nan=False)


class MessageTests(unittest.TestCase):
    def test_board_message_lists_scenarios_and_disclaimer(self):
        b = board_with([sweep_buy()])
        msg = scn.board_message(b)
        self.assertIn('BẢNG KỊCH BẢN PHIÊN ÂU', msg)
        self.assertIn('Quét đầu range rồi đảo chiều', msg)
        self.assertIn('không phải lệnh', msg)


if __name__ == '__main__':
    unittest.main()
