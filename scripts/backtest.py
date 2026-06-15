#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Forex Intelligence Dashboard — Backtesting Engine v2
Chay: python backtest.py
      python backtest.py XAU/USD EUR/USD
Output: backtest_results.json (dashboard doc qua /api/backtest)

FIXES tu v1:
  [FIX #1] Du lieu H1 (2 nam) + logic bo phieu giong he thong live
           thay vi weighted score tren du lieu ngay (khong lien quan den live)
  [FIX #4] Sharpe annualize bang tan suat giao dich thuc te, khong dung sqrt(252) co dinh
  [FIX #5] 4 cap active trong live system; bo GBP/USD (27% WR) va XAG/USD (0% WR)
  [FIX #7] Tru spread cost vao entry price de phan anh chi phi thuc te
"""

import json, math, sys, os, time, datetime, urllib.request
import numpy as np

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(os.path.dirname(SCRIPT_DIR), 'data', 'backtest_results.json')

# [FIX #5] 4 cap active — khop voi SYMBOLS trong forex_notifier.py
DEFAULT_SYMBOLS = ['XAU/USD', 'EUR/USD', 'WTI/USD', 'USD/JPY']

YF_MAP = {
    'EUR/USD': 'EURUSD=X', 'USD/JPY': 'USDJPY=X',
    'WTI/USD': 'CL=F',     'XAU/USD': 'GC=F',
    # Legacy — khong chay mac dinh, van ho tro khi chi dinh tay
    'GBP/USD': 'GBPUSD=X', 'XAG/USD': 'SI=F',
    'USD/CHF': 'USDCHF=X', 'AUD/USD': 'AUDUSD=X',
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json,*/*',
}

# [FIX #1] Per-pair config khop voi PAIR_CONFIG trong forex_notifier.py
PAIR_CONFIG = {
    'EUR/USD': {'rsi_buy': 45, 'rsi_sell': 55, 'hurst_block': 0.47, 'min_votes': 3},
    'USD/JPY': {'rsi_buy': 38, 'rsi_sell': 62, 'hurst_block': 0.48, 'min_votes': 3},
    'WTI/USD': {'rsi_buy': 42, 'rsi_sell': 58, 'hurst_block': 0.40, 'min_votes': 3},
    'XAU/USD': {'rsi_buy': 40, 'rsi_sell': 60, 'hurst_block': 0.39, 'min_votes': 3},
}
_DEFAULT_CFG = {'rsi_buy': 45, 'rsi_sell': 55, 'hurst_block': 0.47, 'min_votes': 3}

# [FIX #7] Spread dien hinh theo broker (pip). XAU/WTI cao hon FX majors.
SPREAD_PIPS = {'EUR/USD': 1.5, 'USD/JPY': 1.5, 'WTI/USD': 8.0, 'XAU/USD': 40.0}
PIP_SIZE    = {'EUR/USD': 0.0001, 'USD/JPY': 0.01, 'WTI/USD': 0.10, 'XAU/USD': 0.10}

# Tham so chien luoc mac dinh — WFO se tim gia tri tot nhat
ATR_STOP   = 1.5   # SL = entry +/- ATR_STOP x ATR14
ATR_TARGET = 3.0   # TP = entry +/- ATR_TARGET x ATR14 -> R:R 2:1
MAX_HOLD   = 20    # Thoat toi da sau N nen H1 (~20 gio)
LOOKBACK   = 200   # Nen can de Hurst + EMA on dinh
HURST_STEP = 4     # Tinh lai Hurst moi 4 nen H1 (tiep kiem CPU)

W_DEFAULT  = {'atr_stop': ATR_STOP, 'atr_target': ATR_TARGET}


# ════════════════════════════════════════════════════════════════
# CHI BAO KY THUAT — PORT TU forex_notifier.py (array output)
# ════════════════════════════════════════════════════════════════

def _ema_arr(arr, p):
    """EMA array — cung do dai voi arr, NaN o dau khi chua du data."""
    r = [float('nan')] * len(arr)
    k = 2.0 / (p + 1)
    started = False
    for i in range(len(arr)):
        if not started:
            if i < p - 1:
                continue
            r[i] = sum(arr[i - p + 1:i + 1]) / p
            started = True
        else:
            r[i] = arr[i] * k + r[i - 1] * (1 - k)
    return r


def _rsi_arr(closes, p=14):
    """RSI Wilder array — khop forex_notifier.py."""
    r = [float('nan')] * len(closes)
    if len(closes) < p + 1:
        return r
    g = [0.0] + [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    l = [0.0] + [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag = sum(g[1:p + 1]) / p
    al = sum(l[1:p + 1]) / p
    r[p] = 100 - 100 / (1 + ag / (al or 1e-9))
    for i in range(p + 1, len(closes)):
        ag = (ag * (p - 1) + g[i]) / p
        al = (al * (p - 1) + l[i]) / p
        r[i] = 100 - 100 / (1 + ag / (al or 1e-9))
    return r


def _macd_norm_arr(closes):
    """
    Normalized MACD score [-1, +1] tai moi nen — khop logic trong forex_notifier.py.
    Dung: mac_s > 0.09 = vote BUY | mac_s < -0.09 = vote SELL
    """
    n = len(closes)
    result = [0.0] * n
    if n < 35:
        return result
    k12, k26, k9 = 2.0 / 13, 2.0 / 27, 2.0 / 10
    e12 = sum(closes[:12]) / 12
    for v in closes[12:26]:
        e12 = v * k12 + e12 * (1 - k12)
    e26 = sum(closes[:26]) / 26
    mv_buf = []
    sig_val = None
    for idx in range(26, n):
        v = closes[idx]
        e12 = v * k12 + e12 * (1 - k12)
        e26 = v * k26 + e26 * (1 - k26)
        mv_buf.append(e12 - e26)
        j = len(mv_buf) - 1
        if j < 8:
            continue
        if sig_val is None:
            sig_val = sum(mv_buf[:9]) / 9
        else:
            sig_val = mv_buf[j] * k9 + sig_val * (1 - k9)
        ref = max(abs(sig_val), abs(v) * 0.0001, 1e-10)
        result[idx] = max(-1.0, min(1.0, (mv_buf[j] - sig_val) / ref))
    return result


def _bb_arr(closes, p=20):
    """Bollinger Bands — tra ve (upper_arr, lower_arr)."""
    upper = [float('nan')] * len(closes)
    lower = [float('nan')] * len(closes)
    for i in range(p - 1, len(closes)):
        w = closes[i - p + 1:i + 1]
        mid = sum(w) / p
        std = math.sqrt(sum((x - mid) ** 2 for x in w) / p)
        upper[i] = mid + 2 * std
        lower[i] = mid - 2 * std
    return upper, lower


def _mom_arr(closes, n=5):
    """Momentum ratio [-1, +1] — khop momentum() trong forex_notifier.py."""
    result = [0.0] * len(closes)
    for i in range(n, len(closes)):
        gains = sum(1 for j in range(i - n + 1, i + 1) if closes[j] > closes[j - 1])
        result[i] = (gains - (n - gains)) / n
    return result


def _atr_arr(highs, lows, closes, p=14):
    """ATR Wilder array."""
    n = len(closes)
    result = [0.0] * n
    if n < 2:
        return result
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    a = sum(trs[:p]) / p
    if p - 1 < n:
        result[p - 1] = a
    for i in range(p, n):
        a = (a * (p - 1) + trs[i]) / p
        result[i] = a
    return result


def _hurst(closes_window):
    """
    Hurst Exponent tu toi da 200 nen — khop hurst_exponent() trong forex_notifier.py.
    H > 0.55: TREND | H < 0.45: RANGE | ~0.5: NEUTRAL
    """
    n = min(len(closes_window), 200)
    if n < 50:
        return 0.5
    ts = np.array(closes_window[-n:], dtype=float)
    max_lag = min(n // 4, 50)
    lags = list(range(2, max_lag))
    if len(lags) < 5:
        return 0.5
    tau = np.array([np.std(ts[lag:] - ts[:-lag]) for lag in lags])
    valid = tau > 1e-10
    if valid.sum() < 5:
        return 0.5
    poly = np.polyfit(np.log(np.array(lags)[valid]), np.log(tau[valid]), 1)
    return float(np.clip(poly[0], 0.0, 1.0))


def _adx_at(highs, lows, closes, period=14):
    """
    ADX tai diem cuoi cua window — khop adx_indicator() trong forex_notifier.py.
    Tra ve (adx, pdi, mdi).
    """
    if len(closes) < period * 2 + 1:
        return 0.0, 0.0, 0.0
    pdm, mdm, trs = [], [], []
    for i in range(1, len(closes)):
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        pdm.append(up   if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    def _wilder(arr, n):
        s = sum(arr[:n])
        out = [s]
        for v in arr[n:]:
            s = s - s / n + v
            out.append(s)
        return out

    sp = _wilder(pdm, period)
    sm = _wilder(mdm, period)
    st = _wilder(trs, period)
    pdi = [100 * p / t if t > 1e-10 else 0.0 for p, t in zip(sp, st)]
    mdi = [100 * m / t if t > 1e-10 else 0.0 for m, t in zip(sm, st)]
    dx  = [100 * abs(p - m) / (p + m) if (p + m) > 1e-10 else 0.0 for p, m in zip(pdi, mdi)]
    if len(dx) < period:
        return 0.0, pdi[-1] if pdi else 0.0, mdi[-1] if mdi else 0.0
    adx_v = sum(dx[:period]) / period
    for v in dx[period:]:
        adx_v = (adx_v * (period - 1) + v) / period
    return float(adx_v), float(pdi[-1]) if pdi else 0.0, float(mdi[-1]) if mdi else 0.0


# ════════════════════════════════════════════════════════════════
# H4 CONTEXT — resample H1 → H4 va tinh xu huong
# ════════════════════════════════════════════════════════════════

def _resample_h1_to_h4(candles):
    """Nhom 4 nen H1 thanh 1 nen H4 — khop resample_to_h4() trong forex_notifier.py."""
    n = len(candles)
    if n < 8:
        return []
    remainder = n % 4
    h4 = []
    for i in range(remainder, n, 4):
        grp = candles[i:i + 4]
        if len(grp) == 4:
            h4.append({
                't': grp[-1]['t'],
                'c': grp[-1]['c'],
                'h': max(b['h'] for b in grp),
                'l': min(b['l'] for b in grp),
            })
    return h4


def _h4_direction_arr(h4_candles):
    """
    Xu huong H4 tai moi H4 bar — khop h4_trend() trong forex_notifier.py.
    Tra ve list 'BULL'/'BEAR'/'NEUTRAL' cung do dai voi h4_candles.
    """
    n = len(h4_candles)
    dirs = ['NEUTRAL'] * n
    if n < 20:
        return dirs

    c = [b['c'] for b in h4_candles]
    h = [b['h'] for b in h4_candles]
    l = [b['l'] for b in h4_candles]
    e9  = _ema_arr(c, 9)
    e21 = _ema_arr(c, 21)

    for i in range(20, n):
        e9v  = e9[i]
        e21v = e21[i]
        if math.isnan(e9v) or math.isnan(e21v):
            continue
        price = c[i]

        ema_s = (1  if price > e9v > e21v
                 else -1 if price < e9v < e21v else 0)

        n2 = min(i + 1, 20)
        mid = n2 // 2
        struct_s = 0
        if n2 > mid > 0:
            ph = max(h[i - n2:i - mid])
            ch = max(h[i - mid:i + 1])
            pl = min(l[i - n2:i - mid])
            cl = min(l[i - mid:i + 1])
            struct_s = (1  if ch > ph and cl > pl
                        else -1 if ch < ph and cl < pl else 0)

        mom_cnt = sum(1 for j in range(i - 4, i) if c[j + 1] > c[j]) if i >= 5 else 2
        mom_s = 1 if mom_cnt >= 4 else (-1 if mom_cnt <= 1 else 0)

        score = ema_s * 0.40 + struct_s * 0.30 + mom_s * 0.20
        dirs[i] = 'BULL' if score > 0.20 else ('BEAR' if score < -0.20 else 'NEUTRAL')

    return dirs


# ════════════════════════════════════════════════════════════════
# PRECOMPUTE — tinh truoc tat ca chi bao cho 1 bo candles
# ════════════════════════════════════════════════════════════════

def precompute(candles, sym):
    """
    Tinh truoc moi chi bao can thiet cho backtesting.
    Hurst va ADX duoc tinh moi HURST_STEP nen (tiet kiem CPU).
    H4 direction duoc map nguoc lai thanh H1 index.
    """
    closes = [b['c'] for b in candles]
    highs  = [b['h'] for b in candles]
    lows   = [b['l'] for b in candles]
    n      = len(closes)

    rsi_arr  = _rsi_arr(closes)
    e20_arr  = _ema_arr(closes, 20)
    e50_arr  = _ema_arr(closes, 50)
    macd_arr = _macd_norm_arr(closes)
    bbu, bbl = _bb_arr(closes)
    mom_arr  = _mom_arr(closes)
    atr_arr  = _atr_arr(highs, lows, closes)

    # Hurst + ADX: tinh moi HURST_STEP nen, giu gia tri den lan tinh tiep theo
    hurst_arr = [0.5] * n
    adx_arr   = [0.0] * n
    for i in range(LOOKBACK, n, HURST_STEP):
        end   = i + 1
        h_val = _hurst(closes[:end])
        a_val, _, _ = _adx_at(highs[:end], lows[:end], closes[:end])
        for j in range(i, min(i + HURST_STEP, n)):
            hurst_arr[j] = h_val
            adx_arr[j]   = a_val

    # H4: resample → tinh direction → map ve H1
    # [FIX look-ahead] Direction cua nhom H4 k (tinh tu gia DONG nhom = nen H1 thu 4)
    # chi duoc dung cho cac nen H1 cua nhom k+1 tro di. Neu gan cho chinh nhom k thi
    # 3 nen H1 dau nhom se "biet truoc" gia tuong lai -> WR backtest dep gia tao.
    h4_candles = _resample_h1_to_h4(candles)
    h4_dirs    = _h4_direction_arr(h4_candles)
    remainder  = n % 4
    h4_dir_h1  = ['NEUTRAL'] * n
    for k, d in enumerate(h4_dirs):
        start = remainder + (k + 1) * 4   # tre 1 nhom: nhom k da dong moi duoc dung
        for j in range(start, min(start + 4, n)):
            h4_dir_h1[j] = d

    return dict(
        c=closes, h=highs, l=lows,
        rsi=rsi_arr, e20=e20_arr, e50=e50_arr,
        macd=macd_arr, bbu=bbu, bbl=bbl,
        mom=mom_arr, atr=atr_arr,
        hurst=hurst_arr, adx=adx_arr,
        h4_dir=h4_dir_h1,
    )


# ════════════════════════════════════════════════════════════════
# SIGNAL — he thong bo phieu khop forex_notifier.py
# ════════════════════════════════════════════════════════════════

def signal_at_live(ind, i, sym):
    """
    [FIX #1] Signal tai nen i theo he thong bo phieu, khop analyze() trong forex_notifier.py.
    Tra ve 'BUY'/'SELL' hoac None neu khong du dieu kien.

    Bo sung so voi v1:
      - Hurst filter (per-pair hurst_block)
      - ADX sideways double block
      - H4 context (mo rong nguong RSI, dieu chinh min_votes)
      - Counter-trend TREND+H4 block
      - RANGE regime: min_votes +1
    """
    cfg   = PAIR_CONFIG.get(sym, _DEFAULT_CFG)
    price = ind['c'][i]
    r_val = ind['rsi'][i]
    e20   = ind['e20'][i]
    e50   = ind['e50'][i]
    mac_s = ind['macd'][i]
    bbu_v = ind['bbu'][i]
    bbl_v = ind['bbl'][i]
    mom_s = ind['mom'][i]
    H     = ind['hurst'][i]
    adx_v = ind['adx'][i]
    h4_dir = ind['h4_dir'][i]

    if any(math.isnan(v) for v in [r_val, e20, e50]):
        return None

    # [LOC 1] Hurst filter
    if H < cfg['hurst_block']:
        return None

    # [LOC 2] ADX sideways double block
    if adx_v < 15 and H < 0.50:
        return None

    regime = 'TREND' if H > 0.55 else ('RANGE' if H < 0.45 else 'NEUTRAL')

    # Votes theo H4 context (giong analyze() trong forex_notifier.py)
    if h4_dir == 'BULL':
        rsi_buy_thr = min(cfg['rsi_buy'] + 10, 58)
        rsi_v = 1 if r_val <= rsi_buy_thr else (-1 if r_val >= cfg['rsi_sell'] else 0)
        ema_v = (1  if price > e20 > e50
                 else 0 if price > e50
                 else -1 if price < e20 < e50 else 0)
        mac_v = 1 if mac_s > 0.09 else (0 if mac_s >= -0.05 else -1)
    elif h4_dir == 'BEAR':
        rsi_sell_thr = max(cfg['rsi_sell'] - 10, 42)
        rsi_v = -1 if r_val >= rsi_sell_thr else (1 if r_val <= cfg['rsi_buy'] else 0)
        ema_v = (-1 if price < e20 < e50
                 else 0 if price < e50
                 else 1 if price > e20 > e50 else 0)
        mac_v = -1 if mac_s < -0.09 else (0 if mac_s <= 0.05 else 1)
    else:
        rsi_v = 1 if r_val <= cfg['rsi_buy'] else (-1 if r_val >= cfg['rsi_sell'] else 0)
        ema_v = (1 if price > e20 > e50 else -1 if price < e20 < e50 else 0)
        mac_v = 1 if mac_s > 0.09 else (-1 if mac_s < -0.09 else 0)

    bb_v  = (1  if not math.isnan(bbl_v) and price < bbl_v
             else -1 if not math.isnan(bbu_v) and price > bbu_v else 0)
    mom_v = 1 if mom_s >= 0.2 else (-1 if mom_s <= -0.2 else 0)

    votes    = [rsi_v, ema_v, mac_v, bb_v, mom_v]
    bull_cnt = sum(v for v in votes if v > 0)
    bear_cnt = sum(-v for v in votes if v < 0)

    if max(bull_cnt, bear_cnt) < 2:
        return None

    # H4 dieu chinh min_votes
    prov_dir   = 'BUY' if bull_cnt >= bear_cnt else 'SELL'
    h4_aligned = (prov_dir == 'BUY'  and h4_dir == 'BULL') or \
                 (prov_dir == 'SELL' and h4_dir == 'BEAR')
    h4_opposed = (prov_dir == 'BUY'  and h4_dir == 'BEAR') or \
                 (prov_dir == 'SELL' and h4_dir == 'BULL')

    min_v = cfg['min_votes']
    if h4_aligned:   min_v = max(2, min_v - 1)
    elif h4_opposed: min_v = min(5, min_v + 1)
    if regime == 'RANGE': min_v = min(5, min_v + 1)

    # Counter-trend trong TREND regime + H4 nguoc chieu — block cung
    if regime == 'TREND' and h4_opposed:
        return None

    if bull_cnt >= min_v: return 'BUY'
    if bear_cnt >= min_v: return 'SELL'
    return None


# ════════════════════════════════════════════════════════════════
# BACKTESTING ENGINE
# ════════════════════════════════════════════════════════════════

def run_backtest(candles, sym, params=None, ind=None):
    """
    Backtest tren H1 data voi logic bo phieu giong he thong live.
    params: {'atr_stop': float, 'atr_target': float}
    ind: pre-computed indicators (truyen vao de tranh tinh lai trong WFO).
    [FIX #7] Spread cost duoc tru vao entry price.
    """
    if params is None:
        params = W_DEFAULT
    atr_stop   = params.get('atr_stop',   ATR_STOP)
    atr_target = params.get('atr_target', ATR_TARGET)

    if len(candles) < LOOKBACK + 30:
        return []

    if ind is None:
        ind = precompute(candles, sym)

    # [FIX #7] Spread cost theo gia (khong phai pips)
    spread_cost = SPREAD_PIPS.get(sym, 0.0) * PIP_SIZE.get(sym, 0.0001)

    trades   = []
    in_trade = False
    entry    = None

    for i in range(LOOKBACK, len(candles) - 1):
        # ── Kiem tra thoat lenh ──
        if in_trade:
            bh        = ind['h'][i]
            bl        = ind['l'][i]
            bars_held = i - entry['bar']
            d         = entry['direction']
            ep        = entry['price']
            risk      = abs(ep - entry['stop']) or 1e-9

            exited = False
            if d == 'BUY':
                if bl <= entry['stop']:
                    trades.append({**entry, 'exit_bar': i, 'exit': entry['stop'],
                                   'pnl': -1.0, 'result': 'LOSS'})
                    exited = True
                elif bh >= entry['target']:
                    trades.append({**entry, 'exit_bar': i, 'exit': entry['target'],
                                   'pnl': atr_target / atr_stop, 'result': 'WIN'})
                    exited = True
            else:  # SELL
                if bh >= entry['stop']:
                    trades.append({**entry, 'exit_bar': i, 'exit': entry['stop'],
                                   'pnl': -1.0, 'result': 'LOSS'})
                    exited = True
                elif bl <= entry['target']:
                    trades.append({**entry, 'exit_bar': i, 'exit': entry['target'],
                                   'pnl': atr_target / atr_stop, 'result': 'WIN'})
                    exited = True

            if not exited and bars_held >= MAX_HOLD:
                ex  = ind['c'][i]
                pnl = (ex - ep) / risk if d == 'BUY' else (ep - ex) / risk
                trades.append({**entry, 'exit_bar': i, 'exit': ex,
                               'pnl': round(pnl, 4),
                               'result': 'WIN' if pnl > 0 else 'LOSS'})
                exited = True

            if exited:
                in_trade = False

        # ── Tim tin hieu vao lenh ──
        if not in_trade:
            sig = signal_at_live(ind, i, sym)
            if sig is None:
                continue
            av = ind['atr'][i]
            if math.isnan(av) or av <= 0:
                continue

            # [FIX #7] Spread: BUY → gia vao tang (xau hon), SELL → giam (xau hon)
            raw_price = ind['c'][i]
            ep = raw_price + spread_cost if sig == 'BUY' else raw_price - spread_cost
            t  = candles[i].get('t', 0)

            if sig == 'BUY':
                entry = dict(bar=i, price=ep, direction='BUY',
                             stop=ep - atr_stop * av,
                             target=ep + atr_target * av,
                             atr=round(av, 5), date=t)
            else:
                entry = dict(bar=i, price=ep, direction='SELL',
                             stop=ep + atr_stop * av,
                             target=ep - atr_target * av,
                             atr=round(av, 5), date=t)
            in_trade = True

    return trades


def calc_stats(trades):
    if not trades:
        return {'total': 0, 'win_rate': 0, 'sharpe': 0, 'profit_factor': 0,
                'max_drawdown_r': 0, 'total_return_r': 0, 'equity_curve': [0]}

    wins   = [t for t in trades if t['result'] == 'WIN']
    losses = [t for t in trades if t['result'] == 'LOSS']
    n      = len(trades)
    wr     = len(wins) / n

    avg_w = sum(t['pnl'] for t in wins)   / len(wins)   if wins   else 0
    avg_l = sum(t['pnl'] for t in losses) / len(losses) if losses else 0

    gross_win  = sum(t['pnl'] for t in wins)
    gross_loss = abs(sum(t['pnl'] for t in losses))
    pf         = gross_win / gross_loss if gross_loss > 0 else 99.0

    # Equity curve (don vi R-multiple)
    equity = [0.0]
    for t in trades:
        equity.append(equity[-1] + t['pnl'])

    # Max drawdown
    peak = 0.0
    mdd  = 0.0
    for e in equity:
        peak = max(peak, e)
        mdd  = max(mdd, peak - e)

    # [FIX #4] Sharpe: annualize bang tan suat giao dich thuc te (khong dung sqrt(252) co dinh)
    # sqrt(252) gia dinh 1 lenh/ngay — sai khi lenh keo dai trung binh 5-15 nen H1.
    rets = [t['pnl'] for t in trades]
    mu   = sum(rets) / len(rets)
    sig  = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets))

    if len(trades) >= 2:
        span_bars      = trades[-1]['exit_bar'] - trades[0]['bar']
        BARS_PER_YEAR  = 252 * 16   # H1: ~16 gio giao dich/ngay
        years          = span_bars / max(BARS_PER_YEAR, 1)
        trades_per_yr  = n / max(years, 0.1)
    else:
        trades_per_yr  = 50  # fallback bao thu

    # Guard: rets gan nhu giong het nhau (sig~0) -> Sharpe vo nghia (truoc day bi
    # san 1e-9 thoi len hang ty). Coi nhu 0. Clamp [-10,10] chong nhieu mau nho.
    if sig < 1e-6:
        sharpe = 0.0
    else:
        sharpe = max(-10.0, min(10.0, mu / sig * math.sqrt(trades_per_yr)))

    return dict(
        total=n, wins=len(wins), losses=len(losses),
        win_rate=round(wr, 4),
        avg_win_r=round(avg_w, 4),
        avg_loss_r=round(avg_l, 4),
        profit_factor=round(min(pf, 99.0), 4),
        max_drawdown_r=round(mdd, 4),
        total_return_r=round(equity[-1], 4),
        sharpe=round(sharpe, 4),
        equity_curve=[round(e, 3) for e in equity],
    )


# ════════════════════════════════════════════════════════════════
# WALK-FORWARD OPTIMIZATION
# ════════════════════════════════════════════════════════════════

def build_param_grid():
    """
    [FIX #1] WFO toi uu ATR_STOP va ATR_TARGET thay vi trong so chi bao.
    Ly do: he thong live dung voting (khong co trong so), chi SL/TP co the toi uu.
    16 to hop (stop x target) voi R:R >= 1.2.
    """
    grid = []
    for stop in [1.0, 1.5, 2.0, 2.5]:
        for target in [2.0, 2.5, 3.0, 3.5, 4.0]:
            if target >= stop * 1.2:
                grid.append({'atr_stop': stop, 'atr_target': target})
    return grid


def walk_forward_optimize(candles, sym, n_windows=4):
    """
    Anchored Walk-Forward Optimization tren H1 data.
    Moi vong: train tren 70% data tich luy, test tren 30% tiep theo.
    Chien thang: Sharpe tot nhat tren train + PF > 1.1 + WR > 40%.
    """
    n = len(candles)
    if n < LOOKBACK * 3:
        return {'error': 'Khong du du lieu', 'consensus_weights': W_DEFAULT, 'windows': []}

    grid = build_param_grid()
    step = n // (n_windows + 1)
    results    = []
    all_best   = []
    oos_trades = []   # Gom trade test (OOS) qua moi vong -> equity walk-forward that
    bar_offset = 0    # Doi bar index sang global de Sharpe tinh dung span

    for anchor in range(1, n_windows + 1):
        split    = anchor * step
        test_end = min(split + step, n)
        if test_end - split < 40:
            continue

        train = candles[:split]
        test  = candles[split:test_end]
        print(f'    Vong {anchor}/{n_windows}: '
              f'train={len(train)} nen H1, test={len(test)} nen, luoi={len(grid)} to hop')

        # Pre-compute 1 lan cho train va test — tranh tinh lai moi to hop
        if len(train) < LOOKBACK + 30:
            continue
        train_ind = precompute(train, sym)
        test_ind  = precompute(test,  sym)

        best_sharpe = -999.0
        best_p      = W_DEFAULT
        used_default = False

        for p in grid:
            t  = run_backtest(train, sym, p, ind=train_ind)
            if len(t) < 5:
                continue
            st = calc_stats(t)
            if (st['sharpe'] > best_sharpe
                    and st['profit_factor'] > 1.1
                    and st['win_rate'] > 0.40):
                best_sharpe = st['sharpe']
                best_p      = p

        # Neu khong co to hop nao qua nguong → dung default, danh dau ro
        if best_sharpe == -999.0:
            best_p       = W_DEFAULT
            best_sharpe  = 0.0
            used_default = True

        test_trades = run_backtest(test, sym, best_p, ind=test_ind)
        test_st     = calc_stats(test_trades)

        # Gom OOS: doi bar index sang global (cac test window lien tiep, khong chong)
        for tr in test_trades:
            g = dict(tr)
            g['bar']      = g['bar'] + bar_offset
            g['exit_bar'] = g.get('exit_bar', g['bar']) + bar_offset
            oos_trades.append(g)
        bar_offset += len(test)

        row = dict(
            window=anchor,
            train_bars=len(train), test_bars=len(test),
            best_params={k: round(v, 2) for k, v in best_p.items()},
            used_default=used_default,
            train_sharpe=round(best_sharpe, 4),
            test_sharpe=test_st.get('sharpe', 0),
            test_win_rate=test_st.get('win_rate', 0),
            test_trades=test_st.get('total', 0),
            test_return_r=test_st.get('total_return_r', 0),
            degradation=(
                round((best_sharpe - test_st.get('sharpe', 0)) / (abs(best_sharpe) or 1), 4)
                if not used_default else None
            ),
        )
        results.append(row)
        all_best.append(best_p)

        print(f'      Train Sharpe={best_sharpe:.3f}  '
              f'Test Sharpe={test_st.get("sharpe", 0):.3f}  '
              f'Test WR={test_st.get("win_rate", 0):.1%}  '
              f'Test trades={test_st.get("total", 0)}'
              f'{"  [default]" if used_default else ""}')

    # Consensus: trung binh cac vong khong phai fallback default
    valid_best = [p for p, r in zip(all_best, results) if not r.get('used_default')]
    consensus  = (
        {k: round(sum(p[k] for p in valid_best) / len(valid_best), 2) for k in W_DEFAULT}
        if valid_best else W_DEFAULT
    )

    # Degradation trung binh train->test (chi cac vong khong fallback default)
    degs    = [r['degradation'] for r in results if r.get('degradation') is not None]
    avg_deg = round(sum(degs) / len(degs), 4) if degs else None

    return dict(windows=results, consensus_weights=consensus,
                oos_trades=oos_trades, avg_degradation=avg_deg)


# ════════════════════════════════════════════════════════════════
# TAI DU LIEU — H1 (2 nam) thay vi 1D (5 nam)
# ════════════════════════════════════════════════════════════════

def fetch_candles_yf(sym, interval='1h', yf_range='2y', limit=3000):
    """
    [FIX #1] Dung H1 data (2 nam, ~3500 nen) — khop voi timeframe he thong live.
    Fallback: '1y' neu '2y' khong lay duoc.
    """
    yf_sym = YF_MAP.get(sym)
    if not yf_sym:
        print(f'  [{sym}] Khong co trong YF_MAP')
        return None

    for host in ('query1.finance.yahoo.com', 'query2.finance.yahoo.com'):
        url = (f'https://{host}/v8/finance/chart/{yf_sym}'
               f'?interval={interval}&range={yf_range}&includePrePost=false')
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode('utf-8', errors='replace'))
            res = data.get('chart', {}).get('result')
            if not res:
                continue
            res    = res[0]
            ts     = res.get('timestamp', [])
            q      = res.get('indicators', {}).get('quote', [{}])[0]
            opens  = q.get('open',   [])
            highs  = q.get('high',   [])
            lows   = q.get('low',    [])
            closes = q.get('close',  [])
            vols   = q.get('volume', [])
            candles = []
            for i, t in enumerate(ts):
                try:
                    o = opens[i]; h = highs[i]; l = lows[i]; c = closes[i]
                    v = vols[i] if i < len(vols) else 0
                    if o is None or c is None or c <= 0:
                        continue
                    if not (math.isfinite(o) and math.isfinite(c)):
                        continue
                    candles.append({
                        't': int(t), 'o': float(o),
                        'h': float(h), 'l': float(l),
                        'c': float(c), 'v': int(v) if v else 0,
                    })
                except Exception:
                    continue
            candles.sort(key=lambda x: x['t'])
            seen = set(); unique = []
            for cn in candles:
                if cn['t'] not in seen:
                    seen.add(cn['t']); unique.append(cn)
            return unique[-limit:]
        except Exception as ex:
            print(f'  [{sym}] {host} loi: {ex}')
    return None


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

def fmt_date(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')


def main():
    symbols = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_SYMBOLS
    all_results = {}

    print('=' * 60)
    print('  Forex Intelligence Dashboard — Backtesting Engine v2')
    print('  [FIX #1] Logic bo phieu giong live (Hurst+ADX+H4+5 votes)')
    print('  [FIX #4] Sharpe annualize dung    [FIX #5] 4 active pairs')
    print('  [FIX #7] Spread cost included      Data: H1 2 nam')
    print('=' * 60)
    print(f'  Symbols : {", ".join(symbols)}')
    print(f'  Mac dinh: Stop={ATR_STOP}xATR  Target={ATR_TARGET}xATR  MaxHold={MAX_HOLD}h')
    print()

    for sym in symbols:
        print(f'------ {sym} ------')
        spread_pips = SPREAD_PIPS.get(sym, 0)

        # Tai H1 data
        print(f'  Tai du lieu H1 (2 nam)...')
        candles = fetch_candles_yf(sym, '1h', '2y', 3000)
        if not candles:
            candles = fetch_candles_yf(sym, '1h', '1y', 1500)
        if not candles or len(candles) < LOOKBACK + 50:
            cnt = len(candles) if candles else 0
            print(f'  BO QUA: khong du du lieu ({cnt} nen)')
            continue
        print(f'  OK: {len(candles)} nen H1 '
              f'— {fmt_date(candles[0]["t"])} den {fmt_date(candles[-1]["t"])}'
              f'  (spread={spread_pips} pips included)')

        # 1. Backtest voi tham so mac dinh
        print(f'  [1/3] Backtest tham so mac dinh...')
        def_trades = run_backtest(candles, sym, W_DEFAULT)
        def_stats  = calc_stats(def_trades)
        print(f'        WR={def_stats["win_rate"]:.1%}  '
              f'PF={def_stats["profit_factor"]:.2f}  '
              f'Sharpe={def_stats["sharpe"]:.2f}  '
              f'Trades={def_stats["total"]}')

        # 2. Walk-Forward Optimization
        n_grid = len(build_param_grid())
        print(f'  [2/3] Walk-Forward Optimization ({n_grid} to hop x 4 vong)...')
        t0  = time.time()
        wfo = walk_forward_optimize(candles, sym, n_windows=4)
        print(f'        Xong trong {time.time() - t0:.1f}s '
              f'— Tham so toi uu: {wfo["consensus_weights"]}')

        # 3. Headline = OOS (gop trade test cua moi vong WFO) — KHONG refit in-sample
        opt_p      = wfo.get('consensus_weights', W_DEFAULT)
        oos_trades = wfo.pop('oos_trades', [])          # bo khoi JSON (tranh bloat)
        oos_stats  = calc_stats(oos_trades)             # WR/Sharpe/PF/DD THUC ngoai mau
        avg_deg    = wfo.get('avg_degradation')

        # Giu lai so in-sample (refit toan bo data) — chi de tham chieu, danh dau ro la lac quan
        is_trades  = run_backtest(candles, sym, opt_p)
        is_stats   = calc_stats(is_trades)

        print(f'  [3/3] OOS (walk-forward, ngoai mau) — con so dang tin:')
        print(f'        WR={oos_stats["win_rate"]:.1%}  '
              f'PF={oos_stats["profit_factor"]:.2f}  '
              f'Sharpe={oos_stats["sharpe"]:.2f}  '
              f'Trades={oos_stats["total"]}'
              f'{f"  Degradation={avg_deg:+.1%}" if avg_deg is not None else ""}')
        print(f'        (in-sample tham chieu: WR={is_stats["win_rate"]:.1%}  '
              f'Sharpe={is_stats["sharpe"]:.2f} — lac quan, dung tin)')

        all_results[sym] = {
            'sym': sym,
            'bars': len(candles), 'interval': '1h',
            'date_from': fmt_date(candles[0]['t']),
            'date_to':   fmt_date(candles[-1]['t']),
            'spread_pips': spread_pips,
            'default_params':  W_DEFAULT,
            'default_stats':   {k: v for k, v in def_stats.items() if k != 'equity_curve'},
            'default_equity':  def_stats.get('equity_curve', []),
            'optimal_params':  opt_p,
            # 'optimal_*' = OOS walk-forward (headline that su) — dashboard doc key nay
            'optimal_stats':   {k: v for k, v in oos_stats.items() if k != 'equity_curve'},
            'optimal_equity':  oos_stats.get('equity_curve', []),
            'is_sample_basis': 'oos_walk_forward',
            'avg_degradation': avg_deg,
            # So in-sample (refit toan bo) — chi tham chieu, KHONG dung de danh gia
            'insample_stats':  {k: v for k, v in is_stats.items() if k != 'equity_curve'},
            'wfo': wfo,
            'run_at': datetime.datetime.utcnow().isoformat() + 'Z',
        }
        print()

    # Luu ket qua
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, separators=(',', ':'))

    print(f'Da luu: {OUTPUT_FILE}')
    print('  Mo dashboard de xem ket qua tai tab "Backtest".\n')


if __name__ == '__main__':
    main()
