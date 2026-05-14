#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Forex Signal Notifier v3
Thuat toan nang cao:
  - Hurst Exponent: phat hien regime (trend / mean-reversion / random walk)
  - Fourier Cycle Analysis: vi tri song gia (dinh/day chu ky)
  - OLS Dynamic Weights: trong so dong dua tren kha nang du bao thuc te
  - Intermarket: DXY (dollar index) + Oil anh huong chinh xac theo tung cap
  - Xac nhan 1 moc: +1h (khop voi kieu giu lenh 1 gio)
"""
import json, os, time
import numpy as np
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf

# ── Cau hinh ──────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT',  '')
COOLDOWN_HOURS  = 4
STATE_FILE      = 'last_signals.json'
CHECKPOINTS_H   = [1]    # Xac nhan tai +1h (khop voi kieu giu lenh 1 gio)
MIN_CONFIDENCE  = 65     # Chi gui tin hieu khi do tin cay >= 65%
VN_TZ          = timezone(timedelta(hours=7))   # Gio Viet Nam (UTC+7)

# Trong so rieng tung nhom cap tien te (tu backtest 180 ngay)
# w = [rsi, ema, macd, bb, mom]  |  trend_mult: he so Hurst TREND
PAIR_PROFILES = {
    # JPY cross: Momentum manh nhat, TREND lam viec tot (EUR/JPY 78.6% trong TREND)
    'EUR/JPY': {'w': np.array([0.05, 0.25, 0.30, 0.03, 0.37]), 'trend_mult': 1.05},
    'GBP/JPY': {'w': np.array([0.05, 0.25, 0.35, 0.03, 0.32]), 'trend_mult': 0.88},
    'USD/JPY': {'w': np.array([0.05, 0.20, 0.35, 0.03, 0.37]), 'trend_mult': 0.83},
    # Vang/Bac: Bollinger & RSI quan trong hon (bien dong lon, dao chieu ro)
    'XAU/USD': {'w': np.array([0.14, 0.20, 0.25, 0.14, 0.27]), 'trend_mult': 1.05},
    'XAG/USD': {'w': np.array([0.14, 0.20, 0.25, 0.14, 0.27]), 'trend_mult': 1.05},
    # Dau: Intermarket (oil) la tin hieu chinh, chi bao ky thuat phu
    'USOIL/USD': {'w': np.array([0.05, 0.20, 0.30, 0.05, 0.40]), 'trend_mult': 0.83},
    'UKOIL/USD': {'w': np.array([0.05, 0.20, 0.30, 0.05, 0.40]), 'trend_mult': 0.83},
}
# Mac dinh cho cac cap con lai (EUR/USD, GBP/USD, AUD/USD, USD/CAD, NZD/USD...)
_DEFAULT_PROFILE = {'w': np.array([0.08, 0.30, 0.35, 0.03, 0.24]), 'trend_mult': 0.82}

SYMBOLS = {
    'EUR/USD': 'EURUSD=X', 'GBP/USD': 'GBPUSD=X', 'USD/JPY': 'USDJPY=X',
    'USD/CHF': 'USDCHF=X', 'AUD/USD': 'AUDUSD=X', 'USD/CAD': 'USDCAD=X',
    'NZD/USD': 'NZDUSD=X', 'EUR/GBP': 'EURGBP=X', 'EUR/JPY': 'EURJPY=X',
    'GBP/JPY': 'GBPJPY=X', 'XAU/USD': 'GC=F',     'XAG/USD': 'SI=F',
    'UKOIL/USD': 'BZ=F',   'USOIL/USD': 'CL=F',
}

_im_cache = {}   # Cache intermarket data (chi fetch 1 lan moi phien)

# ── Indicator co ban ─────────────────────────────────────────
def ema(values, period):
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v*k + e*(1-k)
    return e

def rsi(closes, period=14):
    """Wilder's Smoothed RSI - chinh xac hon simple average"""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    ag = sum(max(d,0) for d in deltas[:period]) / period
    al = sum(max(-d,0) for d in deltas[:period]) / period
    for d in deltas[period:]:
        ag = (ag*(period-1) + max(d,0)) / period
        al = (al*(period-1) + max(-d,0)) / period
    return 100.0 if al == 0 else 100 - 100/(1+ag/al)

def macd(closes):
    """MACD chuan: EMA(12,26) + Signal EMA(9) - khong subsampling"""
    if len(closes) < 35:
        return 0.0
    k12, k26, k9 = 2.0/13, 2.0/27, 2.0/10
    e12 = sum(closes[:12]) / 12
    for v in closes[12:26]:
        e12 = v*k12 + e12*(1-k12)
    e26 = sum(closes[:26]) / 26
    mv = [e12 - e26]
    for v in closes[26:]:
        e12 = v*k12 + e12*(1-k12)
        e26 = v*k26 + e26*(1-k26)
        mv.append(e12 - e26)
    if len(mv) < 9:
        return 0.0
    sig = sum(mv[:9]) / 9
    for v in mv[9:]:
        sig = v*k9 + sig*(1-k9)
    ref = max(abs(sig), abs(closes[-1])*0.0001, 1e-10)
    return float(np.clip((mv[-1]-sig)/ref, -1.0, 1.0))

def bollinger(closes, period=20):
    if len(closes) < period:
        return closes[-1], closes[-1], closes[-1]
    w = closes[-period:]
    mid = sum(w) / period
    std = (sum((x-mid)**2 for x in w) / period)**0.5
    return mid+2*std, mid, mid-2*std

def atr(highs, lows, closes, period=14):
    """Average True Range - do bien dong thuc te"""
    if len(closes) < period+1:
        return 0.0
    trs = [max(highs[i]-lows[i],
               abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    if len(trs) < period:
        return 0.0
    a = sum(trs[:period]) / period
    for v in trs[period:]:
        a = (a*(period-1)+v) / period
    return a

def momentum(closes, n=5):
    """Ti le nen tang trong n nen gan nhat: +1 (tat ca tang) .. -1 (tat ca giam)"""
    if len(closes) < n+1:
        return 0.0
    gains = sum(1 for i in range(-n, 0) if closes[i] > closes[i-1])
    return (gains - (n-gains)) / n

# ── Thuat toan nang cao ───────────────────────────────────────
def hurst_exponent(closes):
    """
    Hurst Exponent (R/S Analysis) - do tinh ben cua xu huong.
    H > 0.55: thi truong dang TREND (tin hieu EMA/MACD dang tin)
    H < 0.45: thi truong MEAN-REVERTING (tin hieu RSI/BB dang tin)
    H ~ 0.5:  RANDOM WALK (tin hieu yeu, can than hon)
    """
    if len(closes) < 50:
        return 0.5
    ts = np.array(closes[-50:], dtype=float)
    lags = list(range(2, 20))
    tau  = [np.std(ts[lag:] - ts[:-lag]) for lag in lags]
    tau  = np.array(tau)
    valid = tau > 1e-10
    if valid.sum() < 3:
        return 0.5
    poly = np.polyfit(np.log(np.array(lags)[valid]), np.log(tau[valid]), 1)
    return float(np.clip(poly[0], 0.0, 1.0))

def fourier_signal(closes):
    """
    Fourier Decomposition - tim vi tri trong chu ky song gia.
    Ung dung phan tich tan so (dung trong co hoc luong tu) vao gia.
    +1.0 = dang o day chu ky (tin hieu MUA)
    -1.0 = dang o dinh chu ky (tin hieu BAN)
    """
    if len(closes) < 32:
        return 0.0
    n  = min(128, len(closes))
    ts = np.array(closes[-n:], dtype=float)
    # Loai xu huong tuyen tinh de chi lay thanh phan chu ky
    trend     = np.linspace(ts[0], ts[-1], n)
    detrended = ts - trend
    # Cua so Hanning giam spectral leakage (nhieu bien bien)
    windowed  = detrended * np.hanning(n)
    # FFT tim cac thanh phan tan so chinh
    fft   = np.fft.rfft(windowed)
    power = np.abs(fft)**2
    # Giu 20% tan so co nang luong lon nhat (loc nhieu)
    threshold   = np.percentile(power, 80)
    fft_clean   = np.where(power >= threshold, fft, 0)
    cycle       = np.fft.irfft(fft_clean, n=n)
    std = np.std(cycle)
    if std < 1e-12:
        return 0.0
    # Gia o day chu ky → goc am → tin hieu MUA (+1)
    return float(np.clip(-cycle[-1] / (std*2), -1.0, 1.0))

def _raw_scores(closes):
    """Tinh nhanh 5 diem chi bao (khong can highs/lows) cho 1 period."""
    if len(closes) < 55:
        return None
    p = closes[-1]
    r = rsi(closes)
    rsi_s = (1.0 if r<=30 else 0.5 if r<=40 else -1.0 if r>=70 else -0.5 if r>=60 else 0.0)
    e20 = ema(closes, 20); e50 = ema(closes, 50)
    ema_s = (1.0 if p>e20>e50 else -1.0 if p<e20<e50 else
             0.4 if p>e20 else -0.4 if p<e20 else 0.0)
    mac_s = macd(closes)
    upper, _, lower = bollinger(closes)
    bb_s  = (1.0 if p<lower else -1.0 if p>upper else 0.0)
    mom_s = momentum(closes)
    return [rsi_s, ema_s, mac_s, bb_s, mom_s]

def dynamic_weights(closes, lookback=40):
    """
    OLS Rolling Regression: tinh trong so dong cho tung chi bao
    dua tren kha nang du bao return thuc te trong qua khu.
    - Indicator nao co tuong quan cao voi return thuc → trong so cao hon
    - Tu dong thich nghi theo dieu kien thi truong
    """
    # Trong so mac dinh tu backtest 666 tin hieu: MACD>EMA>MOM>>RSI>BB
    default = np.array([0.08, 0.30, 0.35, 0.03, 0.24])
    c = closes[-120:] if len(closes) > 120 else closes
    if len(c) < 70:
        return default

    scores_hist, fwd_hist = [], []
    for i in range(55, len(c)-1):
        sc = _raw_scores(c[:i+1])
        if sc is None:
            continue
        fwd = (c[i+1] - c[i]) / c[i]
        scores_hist.append(sc)
        fwd_hist.append(fwd)

    if len(scores_hist) < lookback:
        return default

    X = np.array(scores_hist[-lookback:])
    y = np.array(fwd_hist[-lookback:])

    # Pearson correlation cua tung chi bao voi return tuong lai
    corrs = np.zeros(5)
    for j in range(5):
        col = X[:, j]
        if np.std(col) > 1e-10 and np.std(y) > 1e-10:
            c_val = np.corrcoef(col, y)[0, 1]
            corrs[j] = 0.0 if np.isnan(c_val) else c_val

    abs_c = np.abs(corrs)
    total = abs_c.sum()
    if total < 1e-10:
        return default
    # Trong so toi thieu 5% moi chi bao (tranh loai hoan toan)
    w = np.maximum(abs_c / total, 0.05)
    return w / w.sum()

def fetch_intermarket():
    """Lay DXY (dollar index) va Oil 1 lan, cache cho toan bo phien."""
    global _im_cache
    if _im_cache:
        return _im_cache
    for ticker, key in [('UUP', 'dxy'), ('CL=F', 'oil')]:  # UUP = ETF dollar index thay DX=F da delisted
        try:
            df = yf.Ticker(ticker).history(period='5d', interval='1h')
            if df is not None and len(df) >= 20:
                cl   = list(df['Close'].dropna())
                e20v = ema(cl, 20)
                # Phan tram lech gia hien tai so voi EMA20, nhan 2 de normalize
                trend = (cl[-1] - e20v) / e20v * 100
                _im_cache[key] = float(np.clip(trend * 2, -1.0, 1.0))
            else:
                _im_cache[key] = 0.0
        except Exception:
            _im_cache[key] = 0.0
    return _im_cache

def intermarket_signal(sym):
    """
    Tin hieu lien thi truong (intermarket analysis):
    - DXY tang → USD manh → USD/* tang, */USD giam, Vang giam
    - Oil tang → CAD manh → USD/CAD giam
    - Oil tang → USOIL/UKOIL tang truc tiep
    """
    im  = fetch_intermarket()
    dxy = im.get('dxy', 0.0)
    oil = im.get('oil', 0.0)

    if sym in ('USOIL/USD', 'UKOIL/USD'):
        return oil
    if sym in ('XAU/USD', 'XAG/USD'):
        return -dxy   # Vang/Bac nguoc chieu USD
    if sym == 'USD/CAD':
        return float(np.clip(dxy*0.5 - oil*0.5, -1.0, 1.0))
    if sym.startswith('USD/'):
        return dxy
    if sym.endswith('/USD'):
        return -dxy
    return 0.0   # Cross pairs (EUR/GBP...) it bi DXY anh huong

# ── Phan tich tin hieu ────────────────────────────────────────
def analyze(sym, yf_sym):
    try:
        df = yf.Ticker(yf_sym).history(period='60d', interval='1h')
        if df is None or len(df) < 60:
            print(f'  [D] du lieu qua it: {0 if df is None else len(df)} dong')
            return None
        closes = list(df['Close'].dropna())
        highs  = list(df['High'].dropna())
        lows   = list(df['Low'].dropna())
        n = min(len(closes), len(highs), len(lows))
        closes, highs, lows = closes[:n], highs[:n], lows[:n]
        if n < 60:
            print(f'  [D] du lieu sau dropna: {n} dong')
            return None
        price = closes[-1]

        # [LOC 1] ATR filter: bo qua thi truong qua phang (nhieu > tin hieu)
        atr_val = atr(highs, lows, closes)
        if atr_val < price * 0.00015:
            print(f'  [D] ATR={atr_val:.6f} < {price*0.00015:.6f} (loc phang)')
            return None

        # [HURST] Phat hien regime thi truong
        H = hurst_exponent(closes)
        regime = 'TREND' if H > 0.55 else ('RANGE' if H < 0.45 else 'NEUTRAL')

        # [FOURIER] Vi tri trong chu ky song gia
        fft_s = fourier_signal(closes)

        # [INTERMARKET] Tin hieu lien thi truong
        im_s = intermarket_signal(sym)

        # [OLS + PROFILE] Blend trong so dong (OLS) voi trong so tung cap (backtest)
        profile   = PAIR_PROFILES.get(sym, _DEFAULT_PROFILE)
        w_profile = profile['w']
        w_ols     = dynamic_weights(closes)   # OLS tu du lieu gan day
        # 60% OLS (thich nghi thuc te) + 40% profile (neo tu backtest)
        w = 0.60 * w_ols + 0.40 * w_profile
        w = w / w.sum()                       # Chuan hoa tong = 1

        # Chi bao chinh
        p = price
        r = rsi(closes)
        rsi_s = (1.0 if r<=30 else 0.5 if r<=40 else -1.0 if r>=70 else -0.5 if r>=60 else 0.0)
        e20 = ema(closes, 20); e50 = ema(closes, 50)
        ema_s = (1.0 if p>e20>e50 else -1.0 if p<e20<e50 else
                 0.4 if p>e20 else -0.4 if p<e20 else 0.0)
        mac_s = macd(closes)
        upper, _, lower = bollinger(closes)
        bb_s  = (1.0 if p<lower else -1.0 if p>upper else 0.0)
        mom_s = momentum(closes)

        # [LOC 3 - FIX 4] RSI-EMA xung dot: backtest 27-43% chinh xac (tệ hơn tung xu)
        # Chi loc khi RSI o muc cuc doan (>=0.5) va trai chieu voi EMA
        if abs(rsi_s) >= 0.5 and ((rsi_s > 0) != (ema_s > 0)):
            print(f'  [D] RSI-EMA xung dot rsi={rsi_s} ema={ema_s:.2f}')
            return None

        # Composite score: 82% chi bao chinh (trong so dong) + 10% Fourier + 8% Intermarket
        base  = rsi_s*w[0] + ema_s*w[1] + mac_s*w[2] + bb_s*w[3] + mom_s*w[4]
        score = base*0.82 + fft_s*0.10 + im_s*0.08

        # [HURST - DA SUA] Backtest: TREND regime chinh xac 27-48% (tệ hơn ngẫu nhiên)
        # NEUTRAL regime: 43.1% toan the → bo qua hoan toan
        trend_mult = profile.get('trend_mult', 0.82)
        if regime == 'TREND':
            score *= trend_mult  # Phat nhe: xu huong da gia → sap dao chieu
        elif regime == 'RANGE':
            if (score>0 and rsi_s>0) or (score<0 and rsi_s<0):
                score *= 1.10   # Mean-reversion + RSI xac nhan → hop ly
            else:
                score *= 0.80
        elif regime == 'NEUTRAL':
            print(f'  [D] NEUTRAL H={H:.3f} score={score:+.3f}')
            return None

        score = float(np.clip(score, -2.0, 2.0))

        # [LOC 2] Nguong 0.50: backtest cho thay 58.9% chinh xac (vs 45.2% o 0.40)
        if   score >= 0.50: signal = 'BUY'
        elif score <=-0.50: signal = 'SELL'
        else:
            print(f'  [D] score={score:+.3f} yeu (|score|<0.50) H={H:.3f} regime={regime}')
            return None

        # Tinh Entry / SL / TP voi RR 1:2 (thua 1 thang 2)
        # SL = 1.5x ATR: du cho bien dong binh thuong, tranh bi stop sớm
        # TP = 3.0x ATR = 2x SL → RR chinh xac 1:2
        sl_dist = atr_val * 1.5
        tp_dist = atr_val * 3.0
        if signal == 'BUY':
            sl = price - sl_dist
            tp = price + tp_dist
        else:
            sl = price + sl_dist
            tp = price - tp_dist
        sl_pct = round(sl_dist / price * 100, 4)
        tp_pct = round(tp_dist / price * 100, 4)

        # So chi bao dong thuan (trong 6 chi bao)
        s_pos = signal == 'BUY'
        aligned = sum([
            (rsi_s > 0.3) == s_pos,
            (ema_s > 0.3) == s_pos,
            (mac_s > 0.1) == s_pos,
            (bb_s  > 0.5) == s_pos,
            (mom_s > 0.1) == s_pos,
            (fft_s > 0.1) == s_pos,
        ])
        consensus = (rsi_s>0 and ema_s>0) or (rsi_s<0 and ema_s<0)

        return {
            'sym': sym, 'signal': signal,
            'score': round(score, 3), 'price': price, 'rsi': round(r, 1),
            'entry': price, 'sl': sl, 'tp': tp, 'sl_pct': sl_pct, 'tp_pct': tp_pct,
            'hurst': round(H, 3), 'regime': regime, 'aligned': aligned,
            'indicators': {
                'rsi':  rsi_s, 'ema':   ema_s,      'macd': round(mac_s, 2),
                'bb':   bb_s,  'mom':   round(mom_s, 2),
                'fft':  round(fft_s, 2), 'inter': round(im_s, 2),
            },
            'weights': {
                'rsi':  round(float(w[0]),2), 'ema':  round(float(w[1]),2),
                'macd': round(float(w[2]),2), 'bb':   round(float(w[3]),2),
                'mom':  round(float(w[4]),2),
            },
            'consensus': consensus,
        }
    except Exception as e:
        print(f'  [{sym}] Loi: {e}')
        return None

# ── Lay gia hien tai ──────────────────────────────────────────
def fetch_current_price(yf_sym):
    try:
        df = yf.Ticker(yf_sym).history(period='1d', interval='5m')
        if df is None or len(df) == 0:
            return None
        return float(df['Close'].iloc[-1])
    except Exception:
        return None

# ── Format ────────────────────────────────────────────────────
def fmt_price(sym, price):
    if 'JPY' in sym:                                  return f'{price:.3f}'
    if sym in ('XAG/USD',):                           return f'{price:.3f}'
    if sym in ('XAU/USD','UKOIL/USD','USOIL/USD'):    return f'{price:.2f}'
    return f'{price:.5f}'

def _icon(v):
    if v > 0.1:  return '⬆'
    if v < -0.1: return '⬇'
    return '➡'

# ── State ─────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

# ── Telegram ──────────────────────────────────────────────────
def send_telegram(msg, reply_to=None):
    url     = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT, 'text': msg, 'parse_mode': 'HTML'}
    if reply_to:
        payload['reply_to_message_id'] = reply_to
    resp = requests.post(url, json=payload, timeout=10)
    return resp.json()

# ── Xac nhan 3 moc ───────────────────────────────────────────
def run_validations(state, now):
    pending   = state.get('pending_validations', [])
    remaining = []

    for v in pending:
        # Backward compat: migrate dinh dang cu (chi co validate_at)
        if 'validate_at' in v and 'checkpoints' not in v:
            v['checkpoints'] = [
                {'hours': 2,  'at': v.pop('validate_at'), 'done': False},
                {'hours': 4,  'at': v.get('sent_at',0)+4*3600,  'done': False},
                {'hours': 24, 'at': v.get('sent_at',0)+24*3600, 'done': False},
            ]

        any_undone = False
        for cp in v.get('checkpoints', []):
            if cp['done']:
                continue
            any_undone = True
            if now.timestamp() < cp['at']:
                continue

            sym    = v['sym']
            yf_sym = SYMBOLS.get(sym)
            if not yf_sym:
                cp['done'] = True
                continue

            print(f'[+{cp["hours"]}h] Xac nhan {v["signal"]} {sym}...', end=' ', flush=True)
            current = fetch_current_price(yf_sym)
            if current is None:
                print('Khong lay duoc gia, thu lai sau')
                continue   # giu any_undone=True, thu lan chay ke tiep

            entry   = v['entry_price']
            signal  = v['signal']
            diff    = current - entry if signal == 'BUY' else entry - current
            pct     = abs(diff/entry) * 100
            correct = diff > 0

            verdict_emoji = '✅' if correct else '❌'
            verdict       = 'DUNG HUONG' if correct else 'SAI HUONG'
            move_text     = (
                (f'Tang {pct:.3f}%' if signal=='BUY' else f'Giam {pct:.3f}%') if correct
                else (f'Giam {pct:.3f}%' if signal=='BUY' else f'Tang {pct:.3f}%')
            )

            inds    = v.get('indicators', {})
            regime  = v.get('regime', '?')
            H       = v.get('hurst', 0.5)
            aligned = v.get('aligned', '?')
            ind_str = (f"RSI{_icon(inds.get('rsi',0))} EMA{_icon(inds.get('ema',0))} "
                      f"MACD{_icon(inds.get('macd',0))} FFT{_icon(inds.get('fft',0))} "
                      f"IM{_icon(inds.get('inter',0))}")
            sent_dt    = datetime.fromtimestamp(v['sent_at'], tz=timezone.utc).astimezone(VN_TZ)
            now_vn_val = now.astimezone(VN_TZ)

            # Kiem tra TP/SL da bi cham chua (ap sat, vi check theo dinh ky 30 phut)
            sl_val = v.get('sl')
            tp_val = v.get('tp')
            if sl_val and tp_val:
                tp_hit = (current >= tp_val) if signal == 'BUY' else (current <= tp_val)
                sl_hit = (current <= sl_val) if signal == 'BUY' else (current >= sl_val)
                if tp_hit:
                    tp_sl_line = f'🎉 DA CHAM TP ({fmt_price(sym, tp_val)}) - CHOT LOI!'
                elif sl_hit:
                    tp_sl_line = f'💸 DA CHAM SL ({fmt_price(sym, sl_val)}) - DUNG LO!'
                else:
                    d_tp = abs(tp_val - current) / entry * 100
                    d_sl = abs(current - sl_val) / entry * 100
                    tp_sl_line = (f'TP {fmt_price(sym, tp_val)} (con {d_tp:.3f}%) | '
                                  f'SL {fmt_price(sym, sl_val)} (con {d_sl:.3f}%)')
            else:
                tp_sl_line = ''

            msg_lines = [
                f'{verdict_emoji} <b>Ket qua +{cp["hours"]}h — {verdict}</b>',
                '',
                f'📈 Cap: <b>{sym}</b>',
                f'📌 {signal} @ {fmt_price(sym, entry)} → {fmt_price(sym, current)}',
                f'📊 Bien dong: <b>{move_text}</b>',
            ]
            if tp_sl_line:
                msg_lines.append(f'🎯 {tp_sl_line}')
            msg_lines += [
                f'🌊 Regime khi dat: {regime} (Hurst={H:.2f})',
                f'🔍 {ind_str} | {aligned}/6 dong thuan',
                '',
                f'⏱ Dat lenh: {sent_dt.strftime("%d/%m %H:%M")} (Gio VN)',
                f'⏱ Ket qua:  {now_vn_val.strftime("%d/%m %H:%M")} (Gio VN)',
            ]
            msg = '\n'.join(msg_lines)

            result = send_telegram(msg, reply_to=v.get('message_id'))
            if result.get('ok'):
                cp['done'] = True
                print(f'{verdict} ✓')
            else:
                print(f'Loi Telegram: {result}')
            time.sleep(1)

        if any_undone:
            remaining.append(v)

    state['pending_validations'] = remaining

# ── Main ──────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print('TELEGRAM_TOKEN hoac TELEGRAM_CHAT chua duoc dat!')
        return

    now    = datetime.now(timezone.utc)
    now_vn = now.astimezone(VN_TZ)
    state  = load_state()
    sent   = 0

    # Buoc 1: fetch intermarket 1 lan cho ca phien
    print('=== Lay du lieu lien thi truong (DXY, Oil) ===')
    im = fetch_intermarket()
    print(f'  DXY trend: {im.get("dxy",0):+.3f} | Oil trend: {im.get("oil",0):+.3f}')

    # Buoc 2: xac nhan lenh cu
    print('\n=== Kiem tra xac nhan lenh cu ===')
    run_validations(state, now)

    # Buoc 3: quet tin hieu moi
    print(f'\n=== Forex Scan v3 — {now_vn.strftime("%Y-%m-%d %H:%M")} (Gio VN) ===')

    for sym, yf_sym in SYMBOLS.items():
        print(f'Phan tich {sym}...', end=' ', flush=True)
        r = analyze(sym, yf_sym)

        if not r:
            print('NEUTRAL / loc ATR / khong du lieu')
            time.sleep(0.5)
            continue

        inds = r['indicators']
        w    = r['weights']
        print(
            f'{r["signal"]} score={r["score"]:+.3f} | '
            f'regime={r["regime"]}(H={r["hurst"]:.2f}) | '
            f'{r["aligned"]}/6 dong thuan'
        )
        print(
            f'  Weights: rsi={w["rsi"]} ema={w["ema"]} macd={w["macd"]} '
            f'bb={w["bb"]} mom={w["mom"]}'
        )

        key     = f'{sym}|{r["signal"]}'
        elapsed = (now.timestamp() - state.get(key, 0)) / 3600
        if elapsed < COOLDOWN_HOURS:
            print(f'  -> Cooldown ({elapsed:.1f}h / {COOLDOWN_HOURS}h), bo qua')
            time.sleep(0.5)
            continue

        strength = int(abs(r['score']) * 100)
        conf     = int((0.45 + abs(r['score'])*0.35) * 100)

        if conf < MIN_CONFIDENCE:
            print(f'  -> Do tin cay {conf}% < {MIN_CONFIDENCE}%, bo qua')
            time.sleep(0.5)
            continue
        emoji    = '🟢' if r['signal'] == 'BUY' else '🔴'
        rsi_note = 'Qua ban' if r['rsi']<=35 else ('Qua mua' if r['rsi']>=65 else 'Trung tinh')
        regime_icon = '📈' if r['regime']=='TREND' else ('🔄' if r['regime']=='RANGE' else '〰')
        ind_str  = (f"RSI{_icon(inds['rsi'])} EMA{_icon(inds['ema'])} "
                   f"MACD{_icon(inds['macd'])} FFT{_icon(inds['fft'])} "
                   f"IM{_icon(inds['inter'])}")
        consensus_line = ('✔ RSI va EMA cung chieu' if r['consensus']
                         else '⚠ RSI va EMA trai chieu — can than')
        top_w = max(r['weights'], key=r['weights'].get)

        msg = '\n'.join([
            f'{emoji} <b>Tin hieu FOREX — {r["signal"]}</b>',
            '',
            f'📈 Cap: <b>{sym}</b>',
            f'🎯 Vao lenh: <b>{fmt_price(sym, r["price"])}</b>',
            f'🛑 SL (Dung lo): <b>{fmt_price(sym, r["sl"])}</b>  (-{r["sl_pct"]:.3f}%)',
            f'✅ TP (Chot loi): <b>{fmt_price(sym, r["tp"])}</b>  (+{r["tp_pct"]:.3f}%)',
            f'📐 RR: <b>1 : 2</b>  |  Thua 1 thang 2',
            f'📊 RSI: {r["rsi"]} ({rsi_note})',
            f'{regime_icon} Regime: <b>{r["regime"]}</b> (Hurst={r["hurst"]:.2f})',
            f'🔍 Chi bao: {ind_str}',
            f'📋 {r["aligned"]}/6 dong thuan | {consensus_line}',
            f'⚖ Trong so cao nhat: <b>{top_w.upper()}</b> ({r["weights"][top_w]*100:.0f}%)',
            f'💪 Suc manh: <b>{strength}%</b> | Do tin cay: ~<b>{conf}%</b>',
            f'🔔 Xac nhan ket qua sau: +1h',
            '',
            '⚠ Phan tich ky thuat, khong phai tu van tai chinh',
            f'⏱ {now_vn.strftime("%d/%m/%Y %H:%M")} (Gio VN)',
        ])

        result = send_telegram(msg)
        if result.get('ok'):
            msg_id = result.get('result', {}).get('message_id')
            state[key] = now.timestamp()
            if 'pending_validations' not in state:
                state['pending_validations'] = []
            state['pending_validations'].append({
                'sym':         sym,
                'signal':      r['signal'],
                'entry_price': r['price'],
                'sl':          r['sl'],
                'tp':          r['tp'],
                'sent_at':     now.timestamp(),
                'message_id':  msg_id,
                'checkpoints': [
                    {'hours': h, 'at': now.timestamp()+h*3600, 'done': False}
                    for h in CHECKPOINTS_H
                ],
                'indicators':  r['indicators'],
                'regime':      r['regime'],
                'hurst':       r['hurst'],
                'aligned':     r['aligned'],
                'consensus':   r['consensus'],
            })
            sent += 1
            print(f'  -> Telegram OK | Xac nhan: +1h | conf={conf}%')
        else:
            print(f'  -> Loi Telegram: {result}')

        time.sleep(1)

    save_state(state)
    print(f'\n=== Hoan thanh. Da gui {sent} tin hieu moi ===')

if __name__ == '__main__':
    main()
