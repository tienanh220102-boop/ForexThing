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
import re
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
    expected = start
    mfe = 0.0
    from pa_ledger import _weekend_gap
    for b in bars:
        span = b.get('_interval', interval)
        t = b['t']
        if t + span <= start or t + span > now_ts:
            continue
        if expected == start and t <= start:
            expected = t  # first eligible candle covers the issuance boundary
        if t > expected and not _weekend_gap(expected, t):
            return {'status': 'MISSING_HISTORY', 'reason': 'gap before first touch', 'at': expected}
        expected = t + span
        if not filled and t + span > expiry:
            touched = b['l'] <= e if buy else b['h'] >= e
            if t < expiry and touched:
                return {'status': 'UNKNOWN_EXPIRY_BAR', 'at': t, 'resolution_seconds': span}
            return {'status': 'NOFILL'}
        touch_sl = b['l'] <= sl if buy else b['h'] >= sl
        touch_tp = b['h'] >= tp if buy else b['l'] <= tp
        fill_here = False
        if not filled:
            touch_entry = b['l'] <= e if buy else b['h'] >= e
            if not touch_entry:
                continue
            if t < start:
                return {'status': 'UNKNOWN_ENTRY_BAR', 'at': t, 'resolution_seconds': span}
            filled = True
            fill_here = (b['o'] > e if buy else b['o'] < e)
            if (b['o'] <= sl if buy else b['o'] >= sl):
                return {'status': 'UNKNOWN_GAP_FILL', 'at': t, 'resolution_seconds': span}
        if t < start and (touch_sl or touch_tp):
            return {'status': 'UNKNOWN_ENTRY_BAR', 'at': t, 'resolution_seconds': span}
        if t >= start and not fill_here:
            # Position exists at the open. That observed price precedes the
            # rest of the candle, even if both barriers occur later in it.
            at_sl = b['o'] <= sl if buy else b['o'] >= sl
            at_tp = b['o'] >= tp if buy else b['o'] <= tp
            if at_sl or at_tp:
                price = b['o'] if at_sl else tp
                return {'status': 'SL' if at_sl else 'TP1', 'at': t,
                        'gross_r': (price-e)*(1 if buy else -1)/risk,
                        'mfe_before_exit_r': mfe, 'gap_price_model': at_sl,
                        'resolution': f'{span}s_open', 'entry_price_assumption': 'original_signal'}
        if (touch_sl and touch_tp) or (fill_here and touch_tp):
            return {'status': 'UNKNOWN_INTRABAR_ORDER', 'at': t, 'resolution_seconds': span}
        if t < timeout < t + span:
            return {'status': 'UNKNOWN_TIMEOUT_BAR', 'at': t, 'resolution_seconds': span}
        if touch_sl or touch_tp:
            price = sl if touch_sl else tp
            gap = not fill_here and ((b['o'] < sl if buy else b['o'] > sl) if touch_sl else False)
            if gap:
                # Stop market can slip; price-open model is explicit, not exact broker fill.
                price = b['o']
            return {'status': 'SL' if touch_sl else 'TP1', 'at': t,
                    'gross_r': (price - e) * (1 if buy else -1) / risk,
                    'mfe_before_exit_r': mfe, 'gap_price_model': gap,
                    'resolution': f'{span}s_OHLC', 'entry_price_assumption': 'original_signal'}
        favorable = b['h'] - e if buy else e - b['l']
        # Do not claim pre-entry / pre-fill excursions as post-entry profits.
        if t >= start and not fill_here:
            mfe = max(mfe, favorable / risk)
        if t + span >= timeout:
            return {'status': 'TIMEOUT', 'at': t + span,
                    'gross_r': (b['c'] - e) * (1 if buy else -1) / risk}
    if not filled and now_ts >= expiry and expected >= expiry:
        return {'status': 'NOFILL'}
    return {'status': 'MISSING_HISTORY' if expected < min(timeout, now_ts) - interval else 'OPEN',
            'reason': 'no complete path through exit/expiry', 'at': expected}


def recover_one(rec, h1, now_ts, fetch):
    path = [{**b, '_interval': 3600} for b in h1]
    for _ in range(12):
        result = review_trade(rec, path, now_ts, interval=3600)
        if result['status'].startswith('UNKNOWN') and result.get('resolution_seconds') == 3600:
            hour = result['at']
            start = max(hour, int(rec['ts']) // 60 * 60)
            try:
                minutes = fetch(start, hour + 3600, '1min')
            except DataQualityError as exc:
                return {'status': 'MISSING_HISTORY', 'reason': str(exc), 'h1_candidate': result}
            # Replay the WHOLE remaining path, replacing only the ambiguous
            # hour. Do not accidentally truncate the trade at this hour.
            minutes = [{**b, '_interval': 60} for b in minutes if start <= b['t'] < hour + 3600]
            if not minutes:
                return {'status': 'MISSING_HISTORY', 'reason': 'empty minute refinement', 'at': hour}
            path = sorted([b for b in path if not hour <= b['t'] < hour+3600] + minutes,
                          key=lambda b: b['t'])
            continue
        if result['status'] == 'MISSING_HISTORY':
            # Missing H1 history cannot be imputed. Try a complete paginated M1
            # path once, then keep any remaining missing coverage explicit.
            start = int(rec['ts'])//60*60; end = min(rec['ts']+5*86400, now_ts)
            minutes = []
            try:
                while start < end:
                    page_end = min(end, start + 4000*60)
                    minutes.extend(b for b in fetch(start, page_end, '1min') if b['t'] < page_end)
                    start = page_end
                return review_trade(rec, minutes, now_ts)
            except DataQualityError as exc:
                return {'status': 'MISSING_HISTORY', 'reason': str(exc)}
        return result
    return {'status': 'UNKNOWN', 'reason': 'refinement limit exceeded'}


def issuance_anchor(rec, ref):
    """Find SENT log written after Telegram ack, without contacting Telegram.

    Second-resolution logging gives a conservative end-of-second upper bound.
    Missing evidence is recorded, not silently replaced by run-start time.
    """
    cutoff = datetime.fromtimestamp(rec['ts'] + 600, timezone.utc).isoformat()
    commit = subprocess.check_output(['git', 'rev-list', '-1', '--before='+cutoff, ref], text=True).strip()
    try:
        lines = subprocess.check_output(['git', 'show', commit+':data/gold_pa.log'], stderr=subprocess.DEVNULL).decode('utf-8').splitlines()
    except subprocess.CalledProcessError:
        return None
    for line in lines:
        prefix = ('ADDON_SENT '+rec['dir']) if rec['setup']=='addon' else ('SENT '+rec['setup']+' '+rec['dir'])
        if ' UTC '+prefix not in line:
            continue
        price = re.search(r'entry=([0-9.]+)', line)
        if not price or abs(float(price.group(1))-rec['entry']) > 0.02:
            continue
        stamp = datetime.strptime(line[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
        if int(rec['ts']) <= stamp <= rec['ts']+600:
            return {'after_ack_upper_bound': stamp+1, 'sent_log_utc': iso(stamp), 'snapshot': commit}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ref', default='2907543dd014c3b8ec8e1cd7c5d09d7488e5c9b5')
    ap.add_argument('--output', default='data/history_recovery')
    ap.add_argument('--offline', action='store_true')
    ap.add_argument('--asof', help='Fixed UTC replay cutoff, e.g. 2026-10-06T04:00:00Z')
    args = ap.parse_args()
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    original = subprocess.check_output(['git', 'show', args.ref + ':gold_pa_state.json'])
    (out / 'original_state.json').write_bytes(original)
    state = json.loads(original); records = state['signals']
    now_ts = time.time()
    if args.asof:
        cutoff = datetime.fromisoformat(args.asof.replace('Z', '+00:00'))
        if cutoff.tzinfo is None:
            ap.error('--asof must include a timezone')
        now_ts = cutoff.timestamp()
    last_call = 0

    def fetch(start, end, interval):
        nonlocal last_call
        key = hashlib.sha256(f'{start}|{end}|{interval}'.encode()).hexdigest()[:20]
        dest = out / ('source_' + key + '.json')
        if dest.exists():
            value = json.loads(dest.read_text(encoding='utf-8'))
            if 'error' not in value:
                return value['bars']
            if args.offline:
                raise DataQualityError(value['error'])
        # Reuse a larger cached request for a narrower refinement. Its full
        # path will still be checked for gaps by review_trade.
        seconds = {'1h': 3600, '1min': 60}[interval]
        for candidate in sorted(out.glob('source_*.json')):
            cached = json.loads(candidate.read_text(encoding='utf-8'))
            bs = cached.get('bars', [])
            if cached.get('meta', {}).get('interval_seconds') == seconds and bs and \
                    bs[0]['t'] <= start and bs[-1]['t'] + seconds >= end:
                return [b for b in bs if start <= b['t'] <= end]
        if args.offline:
            raise DataQualityError('source not cached')
        # Reserve quota for the scheduled bot (8 credits/minute free tier).
        time.sleep(max(0, 20 - (time.monotonic() - last_call)))
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
        anchor = issuance_anchor(rec, args.ref)
        replay_rec = {**rec, 'ts': anchor['after_ack_upper_bound']} if anchor else rec
        repaired = recover_one(replay_rec, h1, now_ts, fetch)
        if not anchor and repaired['status'] in ('SL', 'TP1', 'TIMEOUT', 'NOFILL'):
            repaired = {'status': 'UNVERIFIED_TIMING', 'recorded_time_model': repaired}
        row = {'signal_ts': rec['ts'], 'date': rec['date'], 'setup': rec['setup'],
               'dir': rec['dir'], 'entry': rec['entry'], 'sl': rec['sl'], 'tp1': rec['tp1'],
               'old_outcome': rec.get('outcome'), 'old_correct': rec.get('correct'),
               'issuance_evidence': anchor, 'repaired': repaired}
        results.append(row)
        print(f'{idx+1}/{len(records)} {rec["date"]} {rec["setup"]}: {rec.get("outcome")} -> {repaired["status"]}', flush=True)
        report = {'source_ref': args.ref, 'original_sha256': hashlib.sha256(original).hexdigest(),
                  'asof_utc': iso(now_ts), 'basis': 'post-SENT-log source-price first SL/TP1 model; not broker fills',
                  'costs': 'gross; broker spread, fees, slippage unavailable',
                  'counts': dict(Counter(r['repaired']['status'] for r in results)), 'trades': results}
        (out / 'recovered_history.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report['counts']), flush=True)


if __name__ == '__main__':
    main()
