"""scripts/fetch_history.py: luu theo nam, lat trang, bao ve credit cho bot live."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fetch_history', ROOT / 'scripts' / 'fetch_history.py')
fh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fh)

Y2026 = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def bars(t0, n, step=60):
    return [{'t': int(t0 + k * step), 'o': 2000.5, 'h': 2001.25, 'l': 1999.75, 'c': 2000.0,
             'closed': True} for k in range(n)]


class FetchHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch.object(fh, 'OUT', Path(self.tmp.name))
        self.p.start()

    def tearDown(self):
        self.p.stop(); self.tmp.cleanup()

    def test_save_load_split_by_year_and_byte_stable(self):
        data = {b['t']: {k: b[k] for k in 'ohlc'} for b in bars(Y2026 - 120, 4)}
        fh.save('1min', data)
        names = sorted(p.name for p in fh.files('1min'))
        self.assertEqual(names, ['XAUUSD_1min_2025.csv.gz', 'XAUUSD_1min_2026.csv.gz'])
        before = [p.read_bytes() for p in fh.files('1min')]
        fh.save('1min', fh.load('1min'))
        self.assertEqual(fh.load('1min'), data)
        self.assertEqual([p.read_bytes() for p in fh.files('1min')], before)

    def test_backfill_pages_until_source_exhausted(self):
        now0 = Y2026
        pages = {'latest': bars(now0, 5), 'back1': bars(now0 - 300, 5), 'back2': []}
        def page(tf, end_ts=None, start_ts=None):
            if start_ts is not None:
                return []
            if end_ts is None:
                return pages['latest']
            return pages['back1'] if end_ts >= now0 - 60 else pages['back2']
        data = {}
        with patch.object(fh, 'page', side_effect=page):
            exhausted = fh.update('1min', data)
        self.assertTrue(exhausted)
        self.assertEqual(len(data), 10)
        self.assertEqual(min(data), now0 - 300)

    def test_stops_before_eating_bot_credits(self):
        with patch.object(fh, 'credits_left', return_value=fh.RESERVE), \
                patch.object(fh.requests, 'get') as get:
            with self.assertRaises(fh.Stop):
                fh.page('1min')
            get.assert_not_called()
        with patch.object(fh, 'credits_left', return_value=None):
            with self.assertRaises(fh.Stop):
                fh.page('1min')


if __name__ == '__main__':
    unittest.main()
