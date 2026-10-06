"""Versioned PAPER entry rules. Thresholds are hypotheses, not fitted edge."""
import copy
import math

RULE_VERSION = '2026-10-06-entry-v3'
MAX_SIGNAL_AGE = 45 * 60
MAX_QUOTE_AGE = 90
MAX_CHASE_ATR = 0.75
MAX_DRIFT_ATR = 0.25
MIN_NET_RR = {'sweep_reclaim': 0.80, 'breakout': 1.20}
LIMIT_TTL = {'sweep_reclaim': 2 * 3600, 'breakout': 3600}


def net_reward_risk(order, entry, cost, stop_slippage=0):
    sign = 1 if order['dir'] == 'BUY' else -1
    risk = (entry - order['sl']) * sign
    reward = (order['tp1'] - entry) * sign
    return (reward - cost) / (risk + cost + stop_slippage) if risk > 0 and cost >= 0 else -math.inf


def prepare(order, atr, structures=(), *, cost=0.4):
    """Freeze rules/targets using only structures confirmed at analysis time."""
    p = copy.deepcopy(order)
    sign = 1 if p['dir'] == 'BUY' else -1
    if atr <= 0 or (p['entry'] - p['structure']) * sign <= 0:
        return None, 'STRUCTURE_ALREADY_BROKEN'
    walls = [s.get('swing_hi' if sign == 1 else 'swing_lo') for s in structures]
    walls = [w for w in walls if w is not None and
             (w-p['entry'])*sign > 0 and (p['tp1']-w)*sign > 0]
    if walls:
        wall = min(walls, key=lambda w: abs(w-p['entry']))
        p['tp1'] = wall - sign * 0.15 * atr
        p['target_wall'] = wall
    p.update(rule_version=RULE_VERSION, atr_at_signal=atr,
             min_net_rr=MIN_NET_RR[p['setup']], max_chase_atr=MAX_CHASE_ATR,
             max_drift_atr=MAX_DRIFT_ATR, max_signal_age=MAX_SIGNAL_AGE,
             limit_ttl=LIMIT_TTL[p['setup']], invalidation_price=p['structure'],
             cancel_if_target_passed=True, risk_cost_price=cost)
    p['net_reward_risk'] = net_reward_risk(p, p['entry'], cost)
    p['rr1'] = abs(p['tp1']-p['entry']) / abs(p['entry']-p['sl'])
    if p['net_reward_risk'] < p['min_net_rr']:
        return None, 'INSUFFICIENT_NET_REWARD'
    return p, None


def validate_quote(order, price, quote_t, now, *, source=None, expected_source=None):
    """Fresh M1 close is a source quote proxy, never claimed executable bid/ask."""
    p = copy.deepcopy(order)
    if not math.isfinite(price) or price <= 0:
        return None, 'INVALID_QUOTE'
    if source is not None and source != expected_source:
        return None, 'QUOTE_SOURCE_CHANGED'
    if not 0 <= now - quote_t < MAX_QUOTE_AGE:
        return None, 'STALE_QUOTE'
    if not 0 <= now - (p['signal_bar_t'] + 3600) <= p['max_signal_age']:
        return None, 'STALE_SETUP'
    sign = 1 if p['dir'] == 'BUY' else -1
    if (price - p['invalidation_price']) * sign <= 0:
        return None, 'STRUCTURE_ALREADY_BROKEN'
    if (price - p['level']) * sign <= 0:
        return None, 'LEVEL_NOT_RECLAIMED'
    if (p['tp1'] - price) * sign <= 0:
        return None, 'TARGET_ALREADY_PASSED'
    if p.get('entry_type', 'market') != 'limit':
        if (price - p['level']) * sign > p['max_chase_atr'] * p['atr_at_signal']:
            return None, 'ENTRY_TOO_FAR_FROM_LEVEL'
        if abs(price - p['entry']) > p['max_drift_atr'] * p['atr_at_signal']:
            return None, 'QUOTE_MOVED_TOO_FAR'
        p['analysis_entry'] = p['entry']
        p['entry'] = price
    elif (price-p['entry'])*sign <= 0:
        return None, 'LIMIT_ALREADY_MARKETABLE'
    p['net_reward_risk'] = net_reward_risk(p, p['entry'], p['risk_cost_price'])
    if p['net_reward_risk'] < p['min_net_rr']:
        return None, 'INSUFFICIENT_NET_REWARD'
    p.update(quote_t=quote_t, quote_checked_at=now, quote_price=price)
    return p, None


def fill_rejection(rec, fill, fill_ts=None):
    """Recheck at actual paper fill; old records keep their original rules."""
    if not rec.get('rule_version'):
        return None
    sign = 1 if rec['dir'] == 'BUY' else -1
    if rec['entry_type'] != 'limit':
        if fill_ts is not None and fill_ts > rec['signal_bar_t'] + 3600 + rec['max_signal_age']:
            return 'CANCELLED_STALE_SETUP'
        if (fill-rec['level'])*sign <= 0:
            return 'CANCELLED_LEVEL_LOST'
        if ((fill-rec['level'])*sign > rec['max_chase_atr']*rec['atr_at_signal'] or
                abs(fill-rec.get('analysis_entry', rec['entry_ref'])) > rec['max_drift_atr']*rec['atr_at_signal']):
            return 'CANCELLED_PRICE_MOVED'
    cost = rec.get('risk_cost_price', rec['cost_price'])
    stop_slippage = 0
    if rec.get('execution', {}).get('pricing') == 'synthetic_bid_ask':
        # Entry already includes spread/slip; TP is an exit-side quote.
        # Only fees and possible STOP slippage remain, not a second spread.
        cost = rec['cost_price']
        stop_slippage = rec['execution'].get('slippage', 0)
    if net_reward_risk(rec, fill, cost, stop_slippage) < rec['min_net_rr']:
        return 'CANCELLED_LOW_RR'
    return None
