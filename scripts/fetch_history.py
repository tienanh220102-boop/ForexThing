#!/usr/bin/env python3
"""Tai/cap nhat lich su XAU/USD tu Twelve Data (CUNG nguon voi bot live).

Luu <HISTORY_DIR>/<tf>/XAUUSD_<tf>_<nam>.csv.gz (t,o,h,l,c; t = gio MO nen, UTC).
Workflow luu vao branch rieng 'history-data' (khong lam nang main / GitHub Pages).
- Lan dau: lui ve qua khu theo trang 5000 nen den START hoac den khi nguon het du lieu.
- Lan sau: tai them phan moi, roi lui tiep neu chua cham START.
Chi giu nen DA DONG; moi trang qua market_data.parse_twelve (bat buoc UTC, hinh hoc OHLC).
BAO VE BOT LIVE: truoc moi request hoi /api_usage; dung khi credit ngay con <= RESERVE
(bot can ~400-500/ngay). Free plan 8 credit/phut -> nghi PAUSE giay giua cac request.

Chay: TWELVE_DATA_KEY=... HISTORY_DIR=... python scripts/fetch_history.py 1h 15min 5min 1min
"""
import csv, gzip, io, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from market_data import parse_twelve, DataQualityError  # noqa: E402

SYM = 'XAU/USD'
START = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
SECONDS = {'1h': 3600, '15min': 900, '5min': 300, '1min': 60}
OUT = Path(os.environ.get('HISTORY_DIR') or ROOT / 'data' / 'history')
PAUSE = 9
MAX_CALLS = int(os.environ.get('HISTORY_MAX_CALLS', '150'))
RESERVE = int(os.environ.get('HISTORY_CREDIT_RESERVE', '450'))
KEY = os.environ.get('TWELVE_DATA_KEY', '')
API = 'https://api.twelvedata.com'
calls = 0


class Stop(Exception):
    pass


def year_of(t):
    return datetime.fromtimestamp(t, timezone.utc).year


def files(tf):
    return sorted((OUT / tf).glob(f'XAUUSD_{tf}_*.csv.gz'))


def load(tf):
    bars = {}
    for p in files(tf):
        with gzip.open(p, 'rt', newline='') as f:
            for r in csv.DictReader(f):
                bars[int(r['t'])] = {k: float(r[k]) for k in 'ohlc'}
    return bars


def save(tf, bars):
    """Ghi lai theo nam; mtime=0 -> cung du lieu = cung byte (git khong thay doi gia)."""
    (OUT / tf).mkdir(parents=True, exist_ok=True)
    by_year = {}
    for t in sorted(bars):
        by_year.setdefault(year_of(t), []).append(t)
    for y, ts in by_year.items():
        p = OUT / tf / f'XAUUSD_{tf}_{y}.csv.gz'
        body = 't,o,h,l,c\n' + ''.join(
            f"{t},{bars[t]['o']},{bars[t]['h']},{bars[t]['l']},{bars[t]['c']}\n" for t in ts)
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode='wb', mtime=0) as gz:
            gz.write(body.encode())
        if not p.exists() or p.read_bytes() != buf.getvalue():
            tmp = p.with_suffix('.tmp')
            tmp.write_bytes(buf.getvalue())
            os.replace(tmp, p)


def credits_left():
    try:
        u = requests.get(f'{API}/api_usage', params={'apikey': KEY}, timeout=30).json()
        return int(u['plan_daily_limit']) - int(u['daily_usage'])
    except Exception:
        return None                     # khong biet -> coi nhu het, an toan cho bot


def page(tf, *, end_ts=None, start_ts=None):
    """Mot trang <=5000 nen DA DONG. Tra [] khi nguon khong con du lieu."""
    global calls
    params = {'symbol': SYM, 'interval': tf, 'outputsize': 5000, 'apikey': KEY,
              'timezone': 'UTC', 'order': 'asc'}
    fmt = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    if end_ts is not None:
        params['end_date'] = fmt(end_ts)
    if start_ts is not None:
        params['start_date'] = fmt(start_ts)
    for attempt in range(4):
        if calls >= MAX_CALLS:
            raise Stop(f'MAX_CALLS={MAX_CALLS}')
        if calls:
            time.sleep(PAUSE)
        left = credits_left()
        if left is None or left <= RESERVE:
            raise Stop(f'credit ngay con {left} <= du tru {RESERVE} cho bot')
        calls += 1
        try:
            data = requests.get(f'{API}/time_series', params=params, timeout=60).json()
        except (requests.RequestException, ValueError):
            data = {'code': 'transport'}          # khong in URL: chua API key
        code = data.get('code')
        if code == 429:
            time.sleep(61)
            continue
        if 'values' not in data:
            if code == 400 or 'no data' in str(data.get('message', '')).lower():
                return []
            if attempt < 3:
                continue
            raise DataQualityError(f'provider returned no candles (code={code})')
        bars = parse_twelve(data, time.time(), interval=SECONDS[tf], requested_timezone='UTC')
        return [b for b in bars if b['closed']]
    raise DataQualityError('rate limit not recovered')


def update(tf, bars):
    """Cap nhat bars tai cho (de bi Stop giua chung van giu phan da tai)."""
    step = SECONDS[tf]
    put = lambda rows: bars.update({b['t']: {k: b[k] for k in 'ohlc'} for b in rows})
    if not bars:
        put(page(tf))
        save(tf, bars)
    while True:                                   # phan moi
        new = [b for b in page(tf, start_ts=max(bars) + step) if b['t'] > max(bars)]
        put(new)
        if len(new) < 4900:
            break
    save(tf, bars)
    while min(bars) > START:                      # lui ve qua khu
        old = [b for b in page(tf, end_ts=min(bars) - step) if b['t'] < min(bars)]
        if not old:
            return True                           # nguon het du lieu truoc START
        put(old)
        save(tf, bars)
        print(f'  {tf}: lui toi {datetime.fromtimestamp(min(bars), timezone.utc):%Y-%m-%d} ({len(bars)} nen)')
    return False


def report(tf, bars, exhausted, done):
    ts = sorted(bars)
    step = SECONDS[tf]
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d %H:%M')
    # nghi cuoi tuan (T6 toi -> CN toi) va nghi ngay ~1h la binh thuong
    big = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > 6 * 3600 and not (
        datetime.fromtimestamp(a, timezone.utc).weekday() in (4, 5) and b - a < 80 * 3600)]
    return {'tf': tf, 'bars': len(ts), 'first': fmt(ts[0]), 'last': fmt(ts[-1]),
            'complete_to_start': done and not exhausted and ts[0] <= START + 7 * 86400,
            'source_exhausted_before_start': exhausted,
            'gaps_over_6h_not_weekend': len(big),
            'largest_gaps': [[fmt(a), fmt(b), round((b - a) / 3600, 1)]
                             for a, b in sorted(big, key=lambda x: x[0] - x[1])[:5]]}


def main():
    if not KEY:
        sys.exit('TWELVE_DATA_KEY chua dat')
    tfs = sys.argv[1:] or ['1h', '15min', '5min', '1min']
    old = {}
    sp = OUT / 'summary.json'
    if sp.exists():
        old = {s['tf']: s for s in json.loads(sp.read_text()).get('series', [])}
    series, stop = [], None
    for tf in tfs:
        bars = load(tf)
        before, exhausted, done = len(bars), False, False
        if stop is None:
            try:
                exhausted = update(tf, bars)
                done = True
            except Stop as e:
                stop = str(e)
                save(tf, bars) if bars else None
        if not bars:
            continue
        info = report(tf, bars, exhausted or old.get(tf, {}).get('source_exhausted_before_start', False), done)
        info['added_this_run'] = len(bars) - before
        series.append(info)
        print(json.dumps(info, ensure_ascii=False))
    for tf, s in old.items():                     # giu muc cua khung khong chay lan nay
        if tf not in {x['tf'] for x in series}:
            series.append(s)
    OUT.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({'source': f'twelvedata:{SYM}', 'timezone': 'UTC', 't': 'bar open time',
                              'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                              'calls_this_run': calls, 'stopped': stop, 'series': series},
                             indent=1, ensure_ascii=False) + '\n')
    if stop:
        print(f'DUNG SOM: {stop} - lan chay sau tai tiep tu cho dang do')


if __name__ == '__main__':
    main()
