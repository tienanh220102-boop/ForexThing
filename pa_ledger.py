"""Paper execution ledger, independent of transport and signal generation.

Market orders fill at the NEXT full M1 open, not at a retrospective H1 close.
Ambiguous intrabar ordering and missing data never become scored wins/losses.
Costs are an explicit round-trip price allowance, not broker fill claims.
"""
from datetime import datetime, timezone
import math

LEDGER_VERSION = 2
SCORED = {'TP1', 'TP2', 'SL', 'BE', 'TIMEOUT'}


def migrate_state(state):
    if state.get('ledger_version') == LEDGER_VERSION:
        return state
    legacy = {k: v for k, v in state.items() if k != 'legacy'}
    return {'ledger_version': LEDGER_VERSION, 'mode': 'paper', 'signals': [],
            'legacy': {'reason': 'unverified_time_order_v1', 'state': legacy},
            'cooldowns': {}, 'day_count': {}, 'pa_knowledge': {},
            'notice_pending': True}


def valid_geometry(rec, entry=None):
    e = rec['entry'] if entry is None else entry
    vals = [e, rec['sl'], rec['tp1'], rec['tp2']]
    if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in vals):
        return False
    return (rec['sl'] < e < rec['tp1'] <= rec['tp2'] if rec['dir'] == 'BUY'
            else rec['sl'] > e > rec['tp1'] >= rec['tp2'])


def new_record(order, ts, source, *, policy='tp1_full', cost_price=0.4):
    if policy not in ('tp1_full', 'partial_be', 'early_be'):
        raise ValueError('unknown exit policy')
    if cost_price < 0 or not math.isfinite(cost_price):
        raise ValueError('invalid cost assumption')
    rec = {k: order.get(k) for k in
           ('setup', 'dir', 'entry', 'sl', 'tp1', 'tp2', 'stars', 'probe',
            'align', 'sl_dist_atr', 'session', 'regime', 'signal_bar_t')}
    rec.update({'ts': ts, 'date': datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d'),
                'ledger_version': LEDGER_VERSION, 'mode': 'paper', 'source': source,
                'entry_type': order.get('entry_type', 'market'), 'entry_ref': order['entry'],
                'status': 'PENDING', 'policy': policy, 'cost_price': cost_price,
                'not_before': (int(ts) // 60 + 1) * 60, 'expires_at': ts + 6 * 3600,
                'remaining': 1.0, 'realized_r': 0.0, 'mfe_r': 0.0, 'mae_r': 0.0,
                'events': []})
    if rec['dir'] not in ('BUY', 'SELL') or not valid_geometry(rec):
        raise ValueError('invalid entry/SL/TP geometry')
    return rec


def _unscored(rec, reason, ts):
    rec.update(status=reason, outcome=reason, correct=None, closed_ts=ts)
    rec['events'].append({'type': reason, 'ts': ts})


def _exit(rec, price, ts, outcome, fraction):
    direction = 1 if rec['dir'] == 'BUY' else -1
    rec['realized_r'] += fraction * (price - rec['entry']) * direction / rec['initial_risk']
    rec['remaining'] = round(rec['remaining'] - fraction, 8)
    rec['events'].append({'type': outcome, 'ts': ts, 'price': price, 'fraction': fraction})
    if rec['remaining'] == 0:
        net = rec['realized_r'] - rec['cost_price'] / rec['initial_risk']
        rec.update(status='CLOSED', outcome=outcome, closed_ts=ts, net_r=net,
                   correct=True if net > 1e-9 else False if net < -1e-9 else None,
                   pips=round(net * rec['initial_risk'] * 10, 4))


def _weekend_gap(start, end):
    """Only accept a gap lying entirely inside conservative FX weekend hours."""
    if end <= start:
        return False
    t = start
    while t < end:
        dt = datetime.fromtimestamp(t, timezone.utc)
        if not (dt.weekday() == 5 or dt.weekday() == 6 and dt.hour < 21
                or dt.weekday() == 4 and dt.hour >= 22):
            return False
        t += 60
    return True


def advance(rec, bars, now_ts, source, *, interval=60):
    if rec.get('outcome') or rec.get('ledger_version') != LEDGER_VERSION:
        return
    if source != rec['source']:
        _unscored(rec, 'SOURCE_CHANGED', now_ts)
        return
    for b in bars:
        t = b['t']
        if t < rec['not_before'] or t <= rec.get('last_bar_t', -1) or t + interval > now_ts:
            continue
        expected = rec.get('last_bar_t', rec['not_before'] - interval) + interval
        if t > expected and not _weekend_gap(expected, t):
            _unscored(rec, 'DATA_GAP', t)
            return
        # Expiry precedes touch. Bar crossing expiry cannot prove on-time fill.
        if rec['status'] == 'PENDING' and t + interval > rec['expires_at']:
            touched = b['l'] <= rec['entry'] if rec['dir'] == 'BUY' else b['h'] >= rec['entry']
            _unscored(rec, 'UNKNOWN_EXPIRY' if t < rec['expires_at'] and touched else 'NOFILL', rec['expires_at'])
            return
        rec['last_bar_t'] = t
        buy = rec['dir'] == 'BUY'
        filled_here = False
        if rec['status'] == 'PENDING':
            limit = rec['entry_type'] == 'limit'
            touched = b['l'] <= rec['entry'] if buy else b['h'] >= rec['entry']
            if limit and not touched:
                continue
            fill = (min(rec['entry'], b['o']) if buy else max(rec['entry'], b['o'])) if limit else b['o']
            if not valid_geometry(rec, fill):
                _unscored(rec, 'INVALID_FILL', t)
                return
            rec.update(entry=fill, initial_risk=abs(fill - rec['sl']), active_sl=rec['sl'],
                       filled_ts=t, status='OPEN', timeout_at=t + 5 * 86400)
            rec['events'].append({'type': 'FILL', 'ts': t, 'price': fill})
            filled_here = limit and (b['o'] > fill if buy else b['o'] < fill)
        sl = rec['active_sl']
        tp = rec['tp2'] if rec.get('tp1_done') else rec['tp1']
        hit_sl = b['l'] <= sl if buy else b['h'] >= sl
        hit_tp = b['h'] >= tp if buy else b['l'] <= tp
        # Opening gaps give known order before intrabar extremes (unless limit filled inside bar).
        if not filled_here and (b['o'] <= sl if buy else b['o'] >= sl):
            _exit(rec, b['o'], t, 'BE' if rec.get('be_active') else 'SL', rec['remaining'])
            return
        if not filled_here and (b['o'] >= tp if buy else b['o'] <= tp):
            if rec['policy'] != 'partial_be' or rec.get('tp1_done'):
                _exit(rec, tp, t, 'TP2' if rec.get('tp1_done') else 'TP1', rec['remaining'])
                return
            _exit(rec, rec['tp1'], t, 'PARTIAL_TP1', 0.5)
            rec['tp1_done'] = True
            tp2_open = b['o'] >= rec['tp2'] if buy else b['o'] <= rec['tp2']
            tp2_hit = b['h'] >= rec['tp2'] if buy else b['l'] <= rec['tp2']
            if tp2_open:
                _exit(rec, rec['tp2'], t, 'TP2', rec['remaining'])
                return
            if hit_sl and tp2_hit:
                _unscored(rec, 'AMBIGUOUS', t)
                return
            if hit_sl:
                _exit(rec, sl, t + interval, 'SL', rec['remaining'])
                return
            if tp2_hit:
                _exit(rec, rec['tp2'], t + interval, 'TP2', rec['remaining'])
                return
            rec.update(active_sl=rec['entry'], be_active=True)
            if t + interval >= rec['timeout_at']:
                _exit(rec, b['c'], t + interval, 'TIMEOUT', rec['remaining'])
                return
            continue
        if hit_sl and hit_tp or filled_here and hit_tp:
            _unscored(rec, 'AMBIGUOUS', t)
            return
        if hit_sl:
            _exit(rec, sl, t + interval, 'BE' if rec.get('be_active') else 'SL', rec['remaining'])
            return
        risk = rec['initial_risk']
        favorable = (b['h'] - rec['entry']) if buy else (rec['entry'] - b['l'])
        adverse = (rec['entry'] - b['l']) if buy else (b['h'] - rec['entry'])
        rec['mfe_r'] = max(rec['mfe_r'], favorable / risk)
        rec['mae_r'] = max(rec['mae_r'], adverse / risk)
        if hit_tp:
            if rec['policy'] == 'partial_be' and not rec.get('tp1_done'):
                # TP1 and TP2 are ordered price barriers. The NEW BE is applied
                # only to the NEXT bar, never to earlier lows/highs of this bar.
                beyond_tp2 = b['h'] >= rec['tp2'] if buy else b['l'] <= rec['tp2']
                _exit(rec, rec['tp1'], t + interval, 'PARTIAL_TP1', 0.5)
                rec.update(tp1_done=True, active_sl=rec['entry'], be_active=True)
                if beyond_tp2:
                    _exit(rec, rec['tp2'], t + interval, 'TP2', rec['remaining'])
                    return
            else:
                _exit(rec, tp, t + interval, 'TP2' if rec.get('tp1_done') else 'TP1', rec['remaining'])
                return
        if rec['policy'] == 'early_be' and not rec.get('be_active'):
            # Hypothesis for research: only a CLOSED candle >= +0.5R moves SL;
            # next candle sees the new stop, never retroactively this candle.
            r_close = (b['c'] - rec['entry']) * (1 if buy else -1) / risk
            if r_close >= 0.5:
                rec.update(active_sl=rec['entry'], be_active=True)
                rec['events'].append({'type': 'BE_ARMED', 'ts': t + interval})
        if t + interval >= rec['timeout_at']:
            _exit(rec, b['c'], t + interval, 'TIMEOUT', rec['remaining'])
            return
    if rec['status'] == 'PENDING' and now_ts >= rec['expires_at']:
        # No bars covering the expiry window is missing evidence, not NOFILL.
        if rec.get('last_bar_t', 0) + interval >= rec['expires_at']:
            _unscored(rec, 'NOFILL', rec['expires_at'])
        else:
            _unscored(rec, 'DATA_GAP', now_ts)


def scored(state):
    return [r for r in state.get('signals', []) if r.get('ledger_version') == LEDGER_VERSION
            and r.get('status') == 'CLOSED' and r.get('outcome') in SCORED
            and isinstance(r.get('net_r'), (int, float))]


def monitor(state):
    """Evaluate every resolution, persist a latch; never auto-enable real money."""
    rows = sorted(scored(state), key=lambda r: (r['closed_ts'], r['ts']))
    rs = [r['net_r'] for r in rows]
    equity = peak = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    reasons = []
    if len(rs) >= 5 and all(r < 0 for r in rs[-5:]):
        reasons.append('five_consecutive_losses')
    if len(rs) >= 10 and sum(rs[-10:]) / 10 <= -0.3:
        reasons.append('last_10_expectancy_below_minus_0.3R')
    if dd >= 5:
        reasons.append('drawdown_at_least_5R')
    state['monitor'] = {'n': len(rs), 'total_r': sum(rs), 'max_dd_r': dd,
                        'reasons': reasons, 'review_required': bool(reasons) or
                        state.get('monitor', {}).get('review_required', False)}
    return state['monitor']
