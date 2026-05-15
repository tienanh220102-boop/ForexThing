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
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT    = os.environ.get('TELEGRAM_CHAT',  '')
TWELVE_DATA_KEY  = os.environ.get('TWELVE_DATA_KEY', '')  # Twelve Data API (free: 800 req/ngay)
COOLDOWN_HOURS  = 4
STATE_FILE      = 'last_signals.json'
CHECKPOINTS_H   = [1]    # Xac nhan tai +1h (khop voi kieu giu lenh 1 gio)
MIN_CONFIDENCE  = 60     # 3/5 phieu = 60% | 4/5 = 75% | 5/5 = 90%
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
_DEFAULT_PROFILE = {'w': np.array([0.08, 0.30, 0.35, 0.03, 0.24]), 'trend_mult': 0.92}

SYMBOLS = {
    'EUR/USD': 'EURUSD=X', 'GBP/USD': 'GBPUSD=X', 'USD/JPY': 'USDJPY=X',
    'USD/CHF': 'USDCHF=X', 'AUD/USD': 'AUDUSD=X', 'USD/CAD': 'USDCAD=X',
    'NZD/USD': 'NZDUSD=X', 'EUR/GBP': 'EURGBP=X', 'EUR/JPY': 'EURJPY=X',
    'GBP/JPY': 'GBPJPY=X', 'XAU/USD': 'GC=F',     'XAG/USD': 'SI=F',
    'UKOIL/USD': 'BZ=F',   'USOIL/USD': 'CL=F',
}

_im_cache = {}   # Cache intermarket data (chi fetch 1 lan moi phien)

# Symbol mapping cho Twelve Data API
TWELVE_DATA_SYMBOLS = {
    'EUR/USD': 'EUR/USD', 'GBP/USD': 'GBP/USD', 'USD/JPY': 'USD/JPY',
    'USD/CHF': 'USD/CHF', 'AUD/USD': 'AUD/USD', 'USD/CAD': 'USD/CAD',
    'NZD/USD': 'NZD/USD', 'EUR/GBP': 'EUR/GBP', 'EUR/JPY': 'EUR/JPY',
    'GBP/JPY': 'GBP/JPY', 'XAU/USD': 'XAU/USD', 'XAG/USD': 'XAG/USD',
    'UKOIL/USD': 'XBR/USD',   # Brent crude
    'USOIL/USD': 'XTI/USD',   # WTI crude
}

def fetch_ohlcv(sym, yf_sym, outputsize=500):
    """
    Lay OHLCV H1: uu tien Twelve Data (chat luong cao),
    fallback yfinance khi chua co API key.
    Twelve Data free: 800 req/ngay — 14 cap × 48 lan/ngay = 672 req, vua du.
    """
    if TWELVE_DATA_KEY:
        td_sym = TWELVE_DATA_SYMBOLS.get(sym)
        if td_sym:
            try:
                r = requests.get(
                    'https://api.twelvedata.com/time_series',
                    params={
                        'symbol': td_sym, 'interval': '1h',
                        'outputsize': outputsize, 'apikey': TWELVE_DATA_KEY,
                    },
                    timeout=15,
                )
                data = r.json()
                if data.get('status') != 'error' and 'values' in data:
                    vals = list(reversed(data['values']))  # Newest-first → chronological
                    if len(vals) >= 60:
                        closes = [float(v['close']) for v in vals]
                        highs  = [float(v['high'])  for v in vals]
                        lows   = [float(v['low'])   for v in vals]
                        return closes, highs, lows
                else:
                    print(f'  Twelve Data: {data.get("message", "unknown error")} ({sym})')
            except Exception as e:
                print(f'  Twelve Data loi {sym}: {e}')
    # Fallback: yfinance
    try:
        df = yf.Ticker(yf_sym).history(period='60d', interval='1h')
        if df is None or len(df) < 60:
            return None, None, None
        closes = list(df['Close'].dropna())
        highs  = list(df['High'].dropna())
        lows   = list(df['Low'].dropna())
        return closes, highs, lows
    except Exception as e:
        print(f'  yfinance loi {sym}: {e}')
        return None, None, None

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

# ── Cong cu nang cap ─────────────────────────────────────────
def analyze_m15(sym, yf_sym):
    """Phan tich nhanh khung M15 de xac nhan confluence voi H1."""
    try:
        df = yf.Ticker(yf_sym).history(period='5d', interval='15m')
        if df is None or len(df) < 40:
            return None
        closes = list(df['Close'].dropna())
        if len(closes) < 40:
            return None
        p     = closes[-1]
        r_val = rsi(closes)
        e20   = ema(closes, 20)
        mac_s = macd(closes)
        rsi_s = (1.0 if r_val<=30 else 0.5 if r_val<=40 else
                 -1.0 if r_val>=70 else -0.5 if r_val>=60 else 0.0)
        ema_s = 1.0 if p > e20 else -1.0
        score = rsi_s*0.30 + ema_s*0.40 + mac_s*0.30
        if score >= 0.25:   return 'BUY'
        if score <= -0.25:  return 'SELL'
        return None
    except Exception:
        return None

def wyckoff_phase(regime, signal, rsi_val):
    """Xac dinh pha Wyckoff tu regime va tin hieu."""
    if regime == 'TREND':
        if signal == 'BUY':
            return 'Transition' if rsi_val >= 65 else 'Markup'
        else:
            return 'Transition' if rsi_val <= 35 else 'Markdown'
    if regime == 'RANGE':
        return 'Accumulation' if signal == 'BUY' else 'Distribution'
    return 'Neutral'

def confidence_bar(n):
    """Tao thanh █░ bieu thi confidence (n tu 1-10)."""
    n = max(1, min(10, n))
    return '█' * n + '░' * (10 - n)

def build_reason(signal, regime, vote_lbls, indicators):
    """Tao cau ly do ngan gon tu cac chi bao dong thuan."""
    parts = [f'{lbl} xác nhận' for lbl in vote_lbls]
    if regime == 'TREND':
        parts.append('xu hướng rõ ràng')
    elif regime == 'RANGE':
        parts.append('thị trường dao động')
    im = indicators.get('inter', 0)
    if abs(im) > 0.15:
        parts.append('Intermarket hỗ trợ' if (im > 0) == (signal == 'BUY') else 'Intermarket ngược chiều')
    return ', '.join(parts) if parts else 'Tín hiệu kỹ thuật tổng hợp'

def build_pa_vol(signal, indicators, rsi_val, h1_phase, m15_signal, m15_phase):
    """Tao danh sach bang chung PA/Vol."""
    lines = []
    rsi_v = indicators.get('rsi', 0)
    if rsi_v < 0:
        lines.append(f'- H1 RSI={rsi_val:.0f} (vùng mua quá bán)')
    elif rsi_v > 0:
        lines.append(f'- H1 RSI={rsi_val:.0f} (vùng bán quá mua)')
    mac = indicators.get('macd', 0)
    if mac < -0.12:
        lines.append('- H1 MACD âm (đã xác nhận)')
    elif mac > 0.12:
        lines.append('- H1 MACD dương (đã xác nhận)')
    bb_v = indicators.get('bb', 0)
    if bb_v != 0:
        lines.append('- H1 giá chạm Bollinger Band (tín hiệu mạnh)')
    lines.append(f'- H1 Wyckoff: {h1_phase}')
    if m15_signal and m15_signal == signal:
        lines.append(f'- M15 confluence: {m15_phase}')
    return '\n'.join(lines)

# ── Phan tich tin hieu (Vote System v4) ──────────────────────
def analyze(sym, yf_sym):
    """
    He thong bieu quyet: moi chi bao bau +1 (MUA) / -1 (BAN) / 0 (trung tinh).
    Can it nhat 3/5 phieu cung chieu de phat tin hieu.
    Thay the Composite Score + OLS (qua phuc tap, can du lieu lon).
    """
    try:
        closes, highs, lows = fetch_ohlcv(sym, yf_sym)
        if closes is None or len(closes) < 60:
            print(f'  [D] du lieu qua it hoac loi fetch')
            return None
        n = min(len(closes), len(highs), len(lows))
        closes, highs, lows = closes[:n], highs[:n], lows[:n]
        if n < 60:
            return None
        price = closes[-1]

        # [LOC 1] ATR filter: bo qua thi truong qua phang
        atr_val = atr(highs, lows, closes)
        if atr_val < price * 0.00015:
            print(f'  [D] ATR={atr_val:.6f} loc phang')
            return None

        # [HURST] Phat hien regime (giu lai de context, khong dung lam he so nhan)
        H      = hurst_exponent(closes)
        regime = 'TREND' if H > 0.55 else ('RANGE' if H < 0.45 else 'NEUTRAL')

        # [INTERMARKET] Tin hieu lien thi truong
        im_s = intermarket_signal(sym)

        # --- Chi bao ky thuat ---
        r_val = rsi(closes)
        e20   = ema(closes, 20)
        e50   = ema(closes, 50)
        mac_s = macd(closes)
        upper, _, lower = bollinger(closes)
        mom_s = momentum(closes)

        # --- He thong bieu quyet ---
        # Moi chi bao bau: +1 (MUA), -1 (BAN), 0 (trung tinh)
        rsi_v = (1 if r_val <= 45 else -1 if r_val >= 55 else 0)
        ema_v = (1 if price > e20 > e50 else -1 if price < e20 < e50 else 0)
        mac_v = (1 if mac_s > 0.12 else -1 if mac_s < -0.12 else 0)
        bb_v  = (1 if price < lower else -1 if price > upper else 0)
        mom_v = (1 if mom_s > 0.2 else -1 if mom_s < -0.2 else 0)

        votes     = [rsi_v, ema_v, mac_v, bb_v, mom_v]
        vote_lbls = ['RSI', 'EMA', 'MACD', 'BB', 'Mom']
        bull_cnt  = sum(v for v in votes if v > 0)
        bear_cnt  = sum(-v for v in votes if v < 0)

        if bull_cnt >= 3:
            signal     = 'BUY'
            vote_count = bull_cnt
        elif bear_cnt >= 3:
            signal     = 'SELL'
            vote_count = bear_cnt
        else:
            print(f'  [D] BUY={bull_cnt} BEAR={bear_cnt} — chua du 3/5 phieu')
            return None

        # Ten cac chi bao dong thuan
        aligned_lbls = [vote_lbls[i] for i, v in enumerate(votes)
                        if (v > 0 and signal == 'BUY') or (v < 0 and signal == 'SELL')]

        # --- Do tin cay ---
        # Nen tang: 3/5=60%, 4/5=75%, 5/5=90%
        base_conf = {3: 60, 4: 75, 5: 90}.get(vote_count, 60)
        # Intermarket cung chieu: +5%; TREND: +5%; RANGE (mean-rev ro hon): +3%
        im_aligned  = (im_s > 0.15 and signal == 'BUY') or (im_s < -0.15 and signal == 'SELL')
        im_bonus    = 5 if im_aligned else 0
        regime_bonus = 5 if regime == 'TREND' else (3 if regime == 'RANGE' else 0)
        conf        = min(95, base_conf + im_bonus + regime_bonus)

        # Wyckoff phase
        phase_name = wyckoff_phase(regime, signal, r_val)

        # SL = 1.5×ATR | TP1 = 3×ATR (RR 1:2) | TP2 = 4.5×ATR (RR 1:3)
        sl_dist  = atr_val * 1.5
        tp_dist  = atr_val * 3.0
        tp2_dist = atr_val * 4.5
        if signal == 'BUY':
            sl = price - sl_dist; tp = price + tp_dist; tp2 = price + tp2_dist
        else:
            sl = price + sl_dist; tp = price - tp_dist; tp2 = price - tp2_dist
        sl_pct  = round(sl_dist  / price * 100, 4)
        tp_pct  = round(tp_dist  / price * 100, 4)
        tp2_pct = round(tp2_dist / price * 100, 4)
        rr1     = round(tp_dist  / sl_dist, 1)
        rr2     = round(tp2_dist / sl_dist, 1)

        entry_low  = price - atr_val * 0.2
        entry_high = price + atr_val * 0.2

        return {
            'sym': sym, 'signal': signal, 'price': price, 'rsi': round(r_val, 1),
            'vote_count': vote_count, 'vote_lbls': aligned_lbls,
            'conf': conf,
            'sl': sl, 'tp': tp, 'tp2': tp2,
            'sl_pct': sl_pct, 'tp_pct': tp_pct, 'tp2_pct': tp2_pct,
            'rr1': rr1, 'rr2': rr2,
            'entry_low': entry_low, 'entry_high': entry_high,
            'phase': phase_name, 'hurst': round(H, 3), 'regime': regime,
            'aligned': vote_count,
            'indicators': {
                'rsi':  rsi_v, 'ema': ema_v, 'macd': round(mac_s, 2),
                'bb':   bb_v,  'mom': round(mom_s, 2), 'inter': round(im_s, 2),
            },
            'consensus': True,   # Luon True khi da qua nguong 3/5
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
    if 'JPY' in sym:                                  return f'{price:,.3f}'
    if sym in ('XAG/USD',):                           return f'{price:,.3f}'
    if sym in ('XAU/USD','UKOIL/USD','USOIL/USD'):    return f'{price:,.2f}'
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
        # Bo qua signal format cu (khong co checkpoints)
        if 'checkpoints' not in v:
            continue

        any_undone = False
        for cp in v.get('checkpoints', []):
            if cp['done']:
                continue
            # Bo qua checkpoint khong con trong CHECKPOINTS_H (don dep gia cu)
            if cp['hours'] not in CHECKPOINTS_H:
                cp['done'] = True
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
            verdict       = 'ĐÚNG HƯỚNG' if correct else 'SAI HƯỚNG'
            move_text     = (
                (f'Tăng {pct:.3f}%' if signal=='BUY' else f'Giảm {pct:.3f}%') if correct
                else (f'Giảm {pct:.3f}%' if signal=='BUY' else f'Tăng {pct:.3f}%')
            )

            inds    = v.get('indicators', {})
            regime  = v.get('regime', '?')
            H       = v.get('hurst', 0.5)
            aligned = v.get('aligned', '?')
            ind_str = (f"RSI{_icon(inds.get('rsi',0))} EMA{_icon(inds.get('ema',0))} "
                      f"MACD{_icon(inds.get('macd',0))} BB{_icon(inds.get('bb',0))} "
                      f"Mom{_icon(inds.get('mom',0))} IM{_icon(inds.get('inter',0))}")
            sent_dt    = datetime.fromtimestamp(v['sent_at'], tz=timezone.utc).astimezone(VN_TZ)
            now_vn_val = now.astimezone(VN_TZ)

            # Kiem tra TP/SL da bi cham chua (ap sat, vi check theo dinh ky 30 phut)
            sl_val = v.get('sl')
            tp_val = v.get('tp')
            if sl_val and tp_val:
                tp_hit = (current >= tp_val) if signal == 'BUY' else (current <= tp_val)
                sl_hit = (current <= sl_val) if signal == 'BUY' else (current >= sl_val)
                if tp_hit:
                    tp_sl_line = f'🎉 ĐÃ CHẠM TP ({fmt_price(sym, tp_val)}) — CHỐT LỜI!'
                elif sl_hit:
                    tp_sl_line = f'💸 ĐÃ CHẠM SL ({fmt_price(sym, sl_val)}) — DỪNG LỖ!'
                else:
                    d_tp = abs(tp_val - current) / entry * 100
                    d_sl = abs(current - sl_val) / entry * 100
                    tp_sl_line = (f'TP {fmt_price(sym, tp_val)} (còn {d_tp:.3f}%) | '
                                  f'SL {fmt_price(sym, sl_val)} (còn {d_sl:.3f}%)')
            else:
                tp_sl_line = ''

            msg_lines = [
                f'{verdict_emoji} <b>Kết quả +{cp["hours"]}h — {verdict}</b>',
                '',
                f'📈 Cặp: <b>{sym}</b>',
                f'📌 {signal} @ {fmt_price(sym, entry)} → {fmt_price(sym, current)}',
                f'📊 Biến động: <b>{move_text}</b>',
            ]
            if tp_sl_line:
                msg_lines.append(f'🎯 {tp_sl_line}')
            msg_lines += [
                f'🌊 Regime khi đặt: {regime} (Hurst={H:.2f})',
                f'🔍 {ind_str} | {aligned}/5 đồng thuận',
                '',
                f'⏱ Đặt lệnh: {sent_dt.strftime("%d/%m %H:%M")} (Giờ VN)',
                f'⏱ Kết quả:  {now_vn_val.strftime("%d/%m %H:%M")} (Giờ VN)',
            ]
            msg = '\n'.join(msg_lines)

            result = send_telegram(msg, reply_to=v.get('message_id'))
            if result.get('ok'):
                cp['done'] = True
                print(f'{verdict} ✓')
                # Ghi ket qua theo doi win rate
                state.setdefault('results', []).append({
                    'sym': sym, 'signal': signal, 'correct': correct,
                    'date': sent_dt.strftime('%Y-%m-%d'), 'regime': v.get('regime', '?'),
                })
                if len(state['results']) > 100:
                    state['results'] = state['results'][-100:]
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
        print(
            f'{r["signal"]} {r["vote_count"]}/5 phieu | '
            f'conf={r["conf"]}% | '
            f'regime={r["regime"]}(H={r["hurst"]:.2f}) | '
            f'dong thuan: {", ".join(r["vote_lbls"])}'
        )

        key     = f'{sym}|{r["signal"]}'
        elapsed = (now.timestamp() - state.get(key, 0)) / 3600
        if elapsed < COOLDOWN_HOURS:
            print(f'  -> Cooldown ({elapsed:.1f}h / {COOLDOWN_HOURS}h), bo qua')
            time.sleep(0.5)
            continue

        conf = r['conf']
        if conf < MIN_CONFIDENCE:
            print(f'  -> Do tin cay {conf}% < {MIN_CONFIDENCE}%, bo qua')
            time.sleep(0.5)
            continue

        # M15 confluence
        m15_dir   = analyze_m15(sym, yf_sym)
        m15_phase = wyckoff_phase(
            'RANGE' if m15_dir else 'NEUTRAL', m15_dir or r['signal'], r['rsi']
        )
        m15_match     = (m15_dir == r['signal'])
        timeframe_lbl = 'M15 + H1 confluence' if m15_match else 'H1'

        # Confidence bar 1-10
        conf_10 = max(1, min(10, round(conf / 10)))
        if m15_match:
            conf_10 = min(10, conf_10 + 1)
        bar = confidence_bar(conf_10)

        # Win rate (Cap 3) — hien thi khi co >= 5 ket qua
        results_all = state.get('results', [])
        wr_line = ''
        if len(results_all) >= 5:
            recent_r = results_all[-20:]
            wr = sum(1 for x in recent_r if x['correct']) / len(recent_r) * 100
            wr_line = f'📈 Win rate ({len(recent_r)} lệnh gần nhất): {wr:.0f}%'

        emoji     = '🟢' if r['signal'] == 'BUY' else '🔴'
        direction = 'MUA' if r['signal'] == 'BUY' else 'BÁN'

        # Entry zone & invalidation
        entry_zone = f'{fmt_price(sym, r["entry_low"])} — {fmt_price(sym, r["entry_high"])}'
        inval_text = (f'Giá lên trên {fmt_price(sym, r["sl"])}'
                      if r['signal'] == 'SELL'
                      else f'Giá xuống dưới {fmt_price(sym, r["sl"])}')

        reason = build_reason(r['signal'], r['regime'], r['vote_lbls'], inds)
        pa_vol = build_pa_vol(r['signal'], inds, r['rsi'],
                              r['phase'], m15_dir, m15_phase)

        vote_bar = '|'.join(r['vote_lbls']) + f'  ({r["vote_count"]}/5 đồng thuận)'
        msg_parts = [
            f'{emoji} <b>{sym} — {direction}</b> | {conf}% tin cậy',
            f'<code>{bar}</code>  {conf_10}/10',
            '',
            f'🗳 {vote_bar}',
            f'📊 Context: {r["phase"]} | {r["regime"]} (H={r["hurst"]:.2f})',
            '',
            f'📍 Entry: {entry_zone}',
            f'🔴 SL: {fmt_price(sym, r["sl"])}',
            f'🎯 TP1: {fmt_price(sym, r["tp"])} (R:R 1:{r["rr1"]})',
            f'🎯 TP2: {fmt_price(sym, r["tp2"])} (R:R 1:{r["rr2"]})',
            '',
            '💡 Lý do:',
            reason,
            '',
            '🔍 Bằng chứng PA/Vol:',
            pa_vol,
            '',
            '⚠️ Vô hiệu nếu:',
            inval_text,
            '',
        ]
        if wr_line:
            msg_parts.append(wr_line)
        msg_parts.append(f'⏰ {now_vn.strftime("%H:%M %d/%m/%Y")} | {timeframe_lbl}')
        msg = '\n'.join(msg_parts)

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
            print(f'  -> Telegram OK | +1h xac nhan | {r["vote_count"]}/5 phieu | conf={conf}%')
        else:
            print(f'  -> Loi Telegram: {result}')

        time.sleep(1)

    save_state(state)
    print(f'\n=== Hoan thanh. Da gui {sent} tin hieu moi ===')

if __name__ == '__main__':
    main()
