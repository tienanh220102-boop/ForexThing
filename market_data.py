"""Validated UTC OHLC. No credentials, network or file side effects."""
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

DATA_VERSION = 2


class DataQualityError(ValueError):
    pass


def validate_bars(bars, now_ts, *, interval=3600):
    if not bars:
        raise DataQualityError('empty OHLC response')
    result = []
    previous = -1
    for raw in bars:
        b = dict(raw)
        t = b.get('t')
        if not isinstance(t, (int, float)) or not math.isfinite(t) or t <= 0:
            raise DataQualityError('invalid candle timestamp')
        if t > now_ts:
            raise DataQualityError('future candle timestamp')
        if t <= previous:
            raise DataQualityError('duplicate or nonascending candle timestamp')
        previous = t
        for k in ('o', 'h', 'l', 'c'):
            if not isinstance(b.get(k), (int, float)) or not math.isfinite(b[k]) or b[k] <= 0:
                raise DataQualityError('invalid OHLC value')
        if not b['l'] <= min(b['o'], b['c']) <= max(b['o'], b['c']) <= b['h']:
            raise DataQualityError('inconsistent OHLC geometry')
        b['closed'] = t + interval <= now_ts
        result.append(b)
    return result


def parse_twelve(data, now_ts, *, interval=3600):
    """Request timezone=UTC; require returned timezone rather than guessing."""
    meta = data.get('meta', {})
    name = meta.get('exchange_timezone') or meta.get('timezone')
    if not name:
        raise DataQualityError('missing response timezone metadata')
    try:
        tz = timezone.utc if name in ('UTC', 'Etc/UTC', 'GMT') else ZoneInfo(name)
    except (ValueError, KeyError) as exc:
        raise DataQualityError('unknown response timezone') from exc
    bars = []
    try:
        for v in data['values']:
            dt = datetime.fromisoformat(v['datetime'])
            if dt.tzinfo is None:
                # Non-UTC wall time can be ambiguous/nonexistent around DST.
                # Fail closed; the caller explicitly requested UTC.
                if name not in ('UTC', 'Etc/UTC', 'GMT'):
                    raise DataQualityError('provider ignored requested UTC timezone')
                dt = dt.replace(tzinfo=tz)
            bars.append({'t': int(dt.timestamp()), **{
                k: float(v[field]) for k, field in
                [('o', 'open'), ('h', 'high'), ('l', 'low'), ('c', 'close')]}})
    except (KeyError, TypeError, ValueError) as exc:
        raise DataQualityError(str(exc)) from exc
    return validate_bars(sorted(bars, key=lambda b: b['t']), now_ts, interval=interval)


def usable_cache(item, now_ts):
    if item.get('data_version') != DATA_VERSION or not item.get('source'):
        return False
    try:
        validate_bars(item.get('bars', []), now_ts)
        return True
    except DataQualityError:
        return False


def closed_h4(bars, now_ts):
    """Timestamp-aligned H4, only four complete H1 candles, no sliding blocks."""
    groups = {}
    for b in bars:
        if b['t'] + 3600 <= now_ts:
            groups.setdefault(int(b['t']) // 14400 * 14400, []).append(b)
    result = []
    for t, group in sorted(groups.items()):
        if [b['t'] for b in group] != [t + i * 3600 for i in range(4)]:
            continue
        result.append({'t': t, 'o': group[0]['o'], 'c': group[-1]['c'],
                       'h': max(b['h'] for b in group), 'l': min(b['l'] for b in group)})
    return result
