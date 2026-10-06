"""Read-only historical audit. Never overwrites original state or claims broker P&L.

Fetch explicit-UTC spot H1 history, refine entry/fill/ambiguous hours with M1.
Missing coverage or unknown intrabar ordering remains UNKNOWN, never a forced SL.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forex_notifier import fetch_twelve_bars
from market_data import DataQualityError


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def review_trade(rec, bars, now_ts, *, interval=60):
    """Original quoted entry, first SL/TP1, limit expiry before fill.

    Entry minute is only usable if its full extremes exclude both exits;
    otherwise we cannot know whether a touch preceded the issued signal.
    """
    e, sl, tp = rec['entry'], rec['sl'], rec['tp1']
    buy = rec['dir'] == 'BUY'
    risk = abs(e - sl)
    if not (sl < e < tp if buy else sl > e > tp):
        return {'status': 'INVALID_GEOMETRY', 'reason': 'SL/entry/TP order invalid'}
    start = rec['ts']; expiry = start + 6 * 3600; timeout = start + 5 * 86400
    filled = rec.get('entry_type', 'market') != 'limit'
    expected = int(start) // interval * interval
    mfe = 0.0
    from pa_ledger import _weekend_gap
    for b in bars:
        t = b['t']
        if t + interval <= start or t + interval > now_ts:
            continue
        if t > expected and not _weekend_gap(expected, t):
            return {'status': 'MISSING_HISTORY', 'reason': 'gap before first touch', 'at': expected}
        expected = t + interval
        if not filled and t + interval > expiry:
            return {'status': 'UNKNOWN_EXPIRY_BAR', 'at': t} if t < expiry else {'status': 'NOFILL'}
        touch_sl = b['l'] <= sl if buy else b['h'] >= sl
        touch_tp = b['h'] >= tp if buy else b['l'] <= tp
        fill_here = False
        if not filled:
            touch_entry = b['l'] <= e if buy else b['h'] >= e
            if not touch_entry:
                continue
            if t < start:
                return {'status': 'UNKNOWN_ENTRY_BAR', 'at': t}
            filled = True
            fill_here = (b['o'] > e if buy else b['o'] < e)
            if b['o'] <= sl if buy else b['o'] >= sl:
                return {'status': 'UNKNOWN_GAP_FILL', 'at': t}
        if t < start and (touch_sl or touch_tp):
            return {'status': 'UNKNOWN_ENTRY_BAR', 'at': t}
        if (touch_sl and touch_tp) or (fill_here and touch_tp):
            return {'status': 'UNKNOWN_INTRABAR_ORDER', 'at': t}
        if touch_sl or touch_tp:
            price = sl if touch_sl else tp
            gap = not fill_here and ((b['o'] < sl if buy else b['o'] > sl) if touch_sl else False)
            if gap:
                # Stop market can slip; price-open model is explicit, not exact broker fill.
                price = b['o']
            return {'status': 'SL' if touch_sl else 'TP1', 'at': t,
                    'gross_r': (price - e) * (1 if buy else -1) / risk,
                    'mfe_before_exit_r': mfe, 'gap_price_model': gap,
                    'resolution': f'{interval}s_OHLC', 'entry_price_assumption': 'original_signal'}
        favorable = b['h'] - e if buy else e - b['l']
        # Do not claim pre-entry / pre-fill excursions as post-entry profits.
        if t >= start and not fill_here:
            mfe = max(mfe, favorable / risk)
        if t + interval >= timeout:
            return {'status': 'TIMEOUT', 'at': t + interval,
                    'gross_r': (b['c'] - e) * (1 if buy else -1) / risk}
    if not filled and now_ts >= expiry and expected >= expiry:
        return {'status': 'NOFILL'}
    return {'status': 'MISSING_HISTORY' if expected < min(timeout, now_ts) - interval else 'OPEN',
            'reason': 'no complete path through exit/expiry', 'at': expected}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ref', default='2907543dd014c3b8ec8e1cd7c5d09d7488e5c9b5')
    ap.add_argument('--output', default='data/history_recovery')
    ap.add_argument('--offline', action='store_true')
    args = ap.parse_args()
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    original = subprocess.check_output(['git', 'show', args.ref + ':gold_pa_state.json'])
    (out / 'original_state.json').write_bytes(original)
    state = json.loads(original); records = state['signals']
    now_ts = time.time(); last_call = 0

    def fetch(start, end, interval):
        nonlocal last_call
        key = hashlib.sha256(f'{start}|{end}|{interval}'.encode()).hexdigest()[:20]
        dest = out / ('source_' + key + '.json')
        if dest.exists():
            value = json.loads(dest.read_text(encoding='utf-8'))
            if 'error' in value:
                raise DataQualityError(value['error'])
            return value['bars']
        if args.offline:
            raise DataQualityError('source not cached')
        # Reserve quota for the scheduled bot (8 credits/minute free tier).
        time.sleep(max(0, 12 - (time.monotonic() - last_call)))
        last_call = time.monotonic()
        try:
            bars, meta = fetch_twelve_bars('XAU/USD', interval=interval, outputsize=5000,
                                          start_date=iso(start), end_date=iso(end))
            dest.write_text(json.dumps({'meta': meta, 'bars': bars}), encoding='utf-8')
            return bars
        except DataQualityError as exc:
            dest.write_text(json.dumps({'error': str(exc)}), encoding='utf-8')
            raise

    start = min(r['ts'] for r in records) // 3600 * 3600
    try:
        h1 = fetch(start, now_ts, '1h')
    except DataQualityError:
        h1 = []
    results = []
    for idx, rec in enumerate(records):
        first = review_trade(rec, h1, now_ts, interval=3600)
        # A clean H1 path already establishes which barrier occurred first.
        # Any entry-boundary, fill, ordering, expiry or coverage uncertainty is refined.
        repaired = first
        if first['status'].startswith(('UNKNOWN', 'MISSING')):
            end = min(rec['ts'] + 5 * 86400, now_ts)
            if first.get('at') and first['status'].startswith('UNKNOWN'):
                end = min(end, first['at'] + 3600)
            minute_bars = []
            cursor = int(rec['ts']) // 60 * 60
            try:
                while cursor < end:
                    page_end = min(end, cursor + 4000 * 60)
                    page = fetch(cursor, page_end, '1min')
                    minute_bars.extend(b for b in page if b['t'] < page_end)
                    cursor = page_end
                repaired = review_trade(rec, minute_bars, now_ts)
            except DataQualityError as exc:
                repaired = {'status': 'MISSING_HISTORY', 'reason': str(exc), 'h1_candidate': first}
        row = {'signal_ts': rec['ts'], 'date': rec['date'], 'setup': rec['setup'],
               'dir': rec['dir'], 'entry': rec['entry'], 'sl': rec['sl'], 'tp1': rec['tp1'],
               'old_outcome': rec.get('outcome'), 'old_correct': rec.get('correct'),
               'repaired': repaired}
        results.append(row)
        print(f'{idx+1}/{len(records)} {rec["date"]} {rec["setup"]}: {rec.get("outcome")} -> {repaired["status"]}', flush=True)
        report = {'source_ref': args.ref, 'original_sha256': hashlib.sha256(original).hexdigest(),
                  'asof_utc': iso(now_ts), 'basis': 'signal-model first SL/TP1, not broker fills',
                  'costs': 'gross; broker spread, fees, slippage unavailable',
                  'counts': dict(Counter(r['repaired']['status'] for r in results)), 'trades': results}
        (out / 'recovered_history.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report['counts']), flush=True)


if __name__ == '__main__':
    main()
