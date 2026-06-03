#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Forex Signal Notifier v4 — Vote System
  - Vote System 3/5: moi chi bao bau phieu, can 3/5 cung chieu
  - Hurst Exponent: phat hien regime (TREND / RANGE / NEUTRAL)
  - Intermarket: DXY + Oil anh huong theo tung cap
  - Twelve Data API (fallback yfinance)
  - Xac nhan +1h: ket qua, TP/SL, win rate
"""
import json, os, time
import numpy as np
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf
import pandas as pd

# ── Cau hinh ──────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT    = os.environ.get('TELEGRAM_CHAT',  '')
TWELVE_DATA_KEY  = os.environ.get('TWELVE_DATA_KEY', '')  # Twelve Data API (free: 800 req/ngay)
COOLDOWN_HOURS  = 6
STATE_FILE      = 'last_signals.json'
CHECKPOINTS_H   = [1]    # Xac nhan tai +1h (khop voi kieu giu lenh 1 gio)
MIN_CONFIDENCE  = 65     # 3/5 phieu = 60 (truoc bonus) | can bonus H4/Fib/SR de dat 65
VN_TZ          = timezone(timedelta(hours=7))   # Gio Viet Nam (UTC+7)
# Ngay bat dau logic hien tai — chi dem ket qua tu ngay nay tro di de danh gia
# Cap nhat moi khi co thay doi lon ve thuat toan (ADX filter, session filter, swing SL)
LOGIC_VERSION   = '2026-06-03'
# Session per-pair duoc dinh nghia trong PAIR_CONFIG['trade_hours'] (UTC)
# Xem PAIR_CONFIG ben duoi de biet gio cu the tung cap

# Tham so phan tich rieng tung cap tien — thay the nguong chung trong analyze()
# rsi_buy  : RSI <= nguong nay → phieu MUA  (cang thap → cang chat, tranh false BUY trong trend)
# rsi_sell : RSI >= nguong nay → phieu BAN  (cang cao → cang chat)
# hurst_block : H < nguong nay → bo qua (RANGE sau)
#   Trailing pairs: ha block → cho qua thi truong co H thap hon
#   Range pairs:    nang block → loc chat hon
# min_votes: so phieu toi thieu (3 = chuan | 4 = yeu cau cao hon cho cap nhieu nhieu)
PAIR_CONFIG = {
    # === MAJORS ===
    # EUR/USD: cap tot nhat (83% WR) — giu nhu cu nhung nang hurst_block
    'EUR/USD':   {'rsi_buy': 45, 'rsi_sell': 55, 'hurst_block': 0.47, 'min_votes': 3,
                  'trade_hours': set(range(7, 21))},
    # GBP/USD: 27% WR — nhu cau tin hieu cuc chat, nang min_votes=5
    'GBP/USD':   {'rsi_buy': 42, 'rsi_sell': 58, 'hurst_block': 0.52, 'min_votes': 5,
                  'trade_hours': set(range(7, 21))},
    # USD/JPY: 33% WR — nang hurst_block, RSI chat hon
    'USD/JPY':   {'rsi_buy': 38, 'rsi_sell': 62, 'hurst_block': 0.48, 'min_votes': 3,
                  'trade_hours': set(range(0, 21))},
    # USD/CHF: 67% WR — hoat dong tot, nang nhe hurst_block
    'USD/CHF':   {'rsi_buy': 45, 'rsi_sell': 55, 'hurst_block': 0.47, 'min_votes': 3,
                  'trade_hours': set(range(7, 21))},
    # USD/CAD: 100% WR — pair tot, giu nguong tuong duong
    'USD/CAD':   {'rsi_buy': 45, 'rsi_sell': 55, 'hurst_block': 0.45, 'min_votes': 3,
                  'trade_hours': set(range(7, 21))},
    # AUD/USD: 0% WR (tat ca SELL deu sai) — nang hurst_block, chi ban khi RSI that su qua mua
    'AUD/USD':   {'rsi_buy': 42, 'rsi_sell': 62, 'hurst_block': 0.50, 'min_votes': 4,
                  'trade_hours': set(range(0, 17)) | {22, 23}},

    # === JPY CROSSES ===
    # EUR/JPY: 67% WR — OK, nang nhe hurst_block len tren nguong RANGE
    'EUR/JPY':   {'rsi_buy': 40, 'rsi_sell': 60, 'hurst_block': 0.46, 'min_votes': 3,
                  'trade_hours': set(range(0, 21))},
    # GBP/JPY: 33% WR — "The beast" qua nhieu nhieu, can TREND that su
    'GBP/JPY':   {'rsi_buy': 40, 'rsi_sell': 60, 'hurst_block': 0.52, 'min_votes': 4,
                  'trade_hours': set(range(0, 21))},
    # AUD/JPY: carry trade — nang hurst_block tren nguong RANGE
    'AUD/JPY':   {'rsi_buy': 40, 'rsi_sell': 60, 'hurst_block': 0.46, 'min_votes': 3,
                  'trade_hours': set(range(0, 17)) | {22, 23}},
    # CAD/JPY: carry trade + oil — nang hurst_block
    'CAD/JPY':   {'rsi_buy': 40, 'rsi_sell': 60, 'hurst_block': 0.46, 'min_votes': 3,
                  'trade_hours': set(range(0, 21))},

    # === KIM LOAI QUY ===
    # XAU/USD: 67% WR — pair tot nhat, nang nhe hurst_block
    'XAU/USD':   {'rsi_buy': 40, 'rsi_sell': 60, 'hurst_block': 0.42, 'min_votes': 3,
                  'trade_hours': set(range(6, 21))},
    # XAG/USD: da bi loai (0% WR, 7 lenh thua lien tiep — 2026-06-03)

    # === DAU MO ===
    # USOIL: 0% WR (1 lenh) — nang hurst_block
    'USOIL/USD': {'rsi_buy': 40, 'rsi_sell': 60, 'hurst_block': 0.47, 'min_votes': 3,
                  'trade_hours': set(range(7, 21))},
    # UKOIL: 33% WR — nang hurst_block
    'UKOIL/USD': {'rsi_buy': 40, 'rsi_sell': 60, 'hurst_block': 0.47, 'min_votes': 3,
                  'trade_hours': set(range(7, 21))},
}
# EUR/GBP da bi loai: 17% WR (1/6), pair phu thuoc chinh sach Brexit/UK-EU
# Khong the phan tich ky thuat chuan xac — loai hoan toan

_DEFAULT_CONFIG = {'rsi_buy': 45, 'rsi_sell': 55, 'hurst_block': 0.47, 'min_votes': 3,
                   'trade_hours': set(range(7, 21))}

SYMBOLS = {
    # Majors (6)
    'EUR/USD': 'EURUSD=X', 'GBP/USD': 'GBPUSD=X', 'USD/JPY': 'USDJPY=X',
    'USD/CHF': 'USDCHF=X', 'USD/CAD': 'USDCAD=X', 'AUD/USD': 'AUDUSD=X',
    # EUR crosses
    'EUR/JPY': 'EURJPY=X',
    # GBP crosses
    'GBP/JPY': 'GBPJPY=X',
    # JPY crosses (carry trade)
    'AUD/JPY': 'AUDJPY=X', 'CAD/JPY': 'CADJPY=X',
    # Commodities — XAU/USD la trong tam (80% win rate)
    'XAU/USD': 'GC=F',
    'UKOIL/USD': 'BZ=F', 'USOIL/USD': 'CL=F',
}

# Vung gia hop le tung symbol — loc du lieu sai tu yfinance/TwelveData
# Dat rong de chi loai gia co ban ro rang bi loi (0, nan, data nham contract)
PRICE_SANITY = {
    # Majors
    'EUR/USD':   (0.70, 1.80),
    'GBP/USD':   (0.80, 2.20),
    'USD/JPY':   (70,   220),
    'USD/CHF':   (0.60, 1.40),
    'AUD/USD':   (0.40, 1.20),
    'USD/CAD':   (0.80, 1.80),
    'NZD/USD':   (0.40, 1.00),
    # EUR crosses
    'EUR/JPY':   (80,   220),
    # GBP crosses
    'GBP/JPY':   (100,  280),
    # JPY crosses
    'AUD/JPY':   (50,   130),
    'CAD/JPY':   (70,   130),
    # Commodities
    'XAU/USD':   (1200, 8000),
    'USOIL/USD': (10,   300),
    'UKOIL/USD': (10,   300),
}

_im_cache          = {}   # Cache intermarket data (chi fetch 1 lan moi phien)
_gold_cache        = {}   # Cache macro data rieng cho XAU/USD (TNX, VIX)
_fundamental_cache = {}   # Cache Fundamental Intelligence Layer (Calendar/Sentiment/F&G)
_price_history     = {}   # Lich su gia tich luy — load tu file, save cuoi phien
_d1_cache          = {}   # D1 OHLCV cache per pair per session (180 ngay daily)

PRICE_HISTORY_FILE = 'price_history.json'
MAX_HISTORY_BARS   = 720  # 30 ngay H1 moi cap (720 nen × 1h = 720h = 30 ngay)

# Mapping dong tien → quoc gia (dung kiem tra lich kinh te)
_CURRENCY_COUNTRY = {
    'USD': 'United States', 'EUR': 'Euro Zone',  'GBP': 'United Kingdom',
    'JPY': 'Japan',         'CAD': 'Canada',      'AUD': 'Australia',
    'NZD': 'New Zealand',   'CHF': 'Switzerland',
    'XAU': 'United States',
    'USO': 'United States', 'UKO': 'United States',
}
# Keyword tim headline lien quan tung dong tien
_CURRENCY_KEYWORDS = {
    'USD': ['dollar', 'usd', 'fed ', 'federal reserve', 'powell', 'treasury'],
    'EUR': ['euro', 'eur ', 'ecb', 'eurozone', 'lagarde'],
    'GBP': ['pound', 'gbp', 'sterling', 'boe', 'bank of england'],
    'JPY': ['yen', 'jpy', 'boj', 'bank of japan'],
    'CAD': ['canadian dollar', 'cad ', 'bank of canada', 'loonie'],
    'AUD': ['aussie', 'aud ', 'rba', 'reserve bank of australia'],
    'NZD': ['kiwi', 'nzd ', 'rbnz'],
    'CHF': ['franc', 'chf ', 'snb', 'swiss national bank'],
    'XAU': ['gold', 'bullion', 'precious metal', 'safe haven'],

    'USO': ['crude oil', 'wti', 'opec'],
    'UKO': ['brent oil', 'crude', 'opec'],
}
_BULLISH_WORDS = [
    'rises', 'gains', 'rallies', 'climbs', 'surges', 'jumps', 'soars',
    'hawkish', 'rate hike', 'beats', 'stronger', 'optimism', 'recovery', 'upbeat',
]
_BEARISH_WORDS = [
    'falls', 'drops', 'declines', 'weakens', 'plunges', 'tumbles', 'slides',
    'dovish', 'rate cut', 'misses', 'weaker', 'concern', 'slowdown', 'recession',
]
# Phan loai cap: risk-on (tang khi thi truong lac quan) / risk-off (tang khi so hai)
_RISK_ON_BUYS  = {'EUR/USD','GBP/USD','AUD/USD','AUD/JPY','CAD/JPY',
                   'GBP/JPY','EUR/JPY','USOIL/USD','UKOIL/USD'}
_RISK_OFF_BUYS = {'USD/JPY', 'USD/CHF', 'XAU/USD'}

# Symbol mapping cho Twelve Data API (13 cap × 48 lan/ngay = 624 req — trong quota free 800)
TWELVE_DATA_SYMBOLS = {
    # Majors
    'EUR/USD': 'EUR/USD', 'GBP/USD': 'GBP/USD', 'USD/JPY': 'USD/JPY',
    'USD/CHF': 'USD/CHF', 'AUD/USD': 'AUD/USD', 'USD/CAD': 'USD/CAD',
    'AUD/USD': 'AUD/USD',
    # EUR crosses
    'EUR/JPY': 'EUR/JPY',
    # GBP crosses
    'GBP/JPY': 'GBP/JPY',
    # JPY crosses
    'AUD/JPY': 'AUD/JPY', 'CAD/JPY': 'CAD/JPY',
    # Commodities
    'XAU/USD': 'XAU/USD',
    'UKOIL/USD': 'XBR/USD',
    'USOIL/USD': 'XTI/USD',
}

def load_price_history():
    """Load lich su gia tu file khi bat dau phien."""
    global _price_history
    if os.path.exists(PRICE_HISTORY_FILE):
        try:
            with open(PRICE_HISTORY_FILE, encoding='utf-8') as f:
                _price_history = json.load(f)
            total = sum(len(v.get('bars', [])) for v in _price_history.values())
            print(f'  [History] {len(_price_history)} cap, {total} bars (toi da {MAX_HISTORY_BARS}/cap)')
        except Exception as e:
            print(f'  [History] Load loi: {e} — bat dau tu dau')
            _price_history = {}

def save_price_history():
    """Save lich su gia cuoi phien (compact JSON de giam kich thuoc file)."""
    try:
        with open(PRICE_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(_price_history, f, separators=(',', ':'))
        total = sum(len(v.get('bars', [])) for v in _price_history.values())
        print(f'  [History] Saved {len(_price_history)} cap, {total} bars')
    except Exception as e:
        print(f'  [History] Save loi: {e}')

def _merge_bars(old_bars, new_bars):
    """
    Merge hai danh sach bars theo timestamp Unix (key 't').
    Bar moi ghi de bar cu cung timestamp (cap nhat nen dang hinh).
    Giu MAX_HISTORY_BARS bars moi nhat.
    """
    by_ts = {b['t']: b for b in old_bars}
    for b in new_bars:
        by_ts[b['t']] = b
    return sorted(by_ts.values(), key=lambda x: x['t'])[-MAX_HISTORY_BARS:]

def fetch_ohlcv(sym, yf_sym, outputsize=500):
    """
    Lay OHLCV H1: uu tien Twelve Data (chat luong cao),
    fallback yfinance khi chua co API key hoac het quota.
    Tra ve: (closes, highs, lows, timestamps) — timestamps la list unix int (UTC).
    Twelve Data free: 800 req/ngay — 14 cap × 48 lan/ngay = 672 req (trong quota free).
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
                        closes     = [float(v['close'])    for v in vals]
                        highs      = [float(v['high'])     for v in vals]
                        lows       = [float(v['low'])      for v in vals]
                        timestamps = []
                        for v in vals:
                            try:
                                dt = datetime.strptime(v['datetime'], '%Y-%m-%d %H:%M:%S')
                                timestamps.append(int(dt.replace(tzinfo=timezone.utc).timestamp()))
                            except Exception:
                                timestamps.append(0)
                        return closes, highs, lows, timestamps
                else:
                    print(f'  Twelve Data: {data.get("message", "unknown error")} ({sym})')
            except Exception as e:
                print(f'  Twelve Data loi {sym}: {e}')
    # Fallback: yfinance
    try:
        df = yf.Ticker(yf_sym).history(period='60d', interval='1h')
        if df is None or len(df) < 60:
            return None, None, None, None
        idx    = df.index
        closes = list(df['Close'].dropna())
        highs  = list(df['High'].dropna())
        lows   = list(df['Low'].dropna())
        # Convert index to unix timestamps (UTC)
        if hasattr(idx, 'tz') and idx.tz is not None:
            timestamps = [int(ts.timestamp()) for ts in idx]
        else:
            timestamps = [int(pd.Timestamp(ts, tz='UTC').timestamp()) for ts in idx]
        n = min(len(closes), len(highs), len(lows), len(timestamps))
        return closes[:n], highs[:n], lows[:n], timestamps[:n]
    except Exception as e:
        print(f'  yfinance loi {sym}: {e}')
        return None, None, None, None

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

def adx_indicator(highs, lows, closes, period=14):
    """
    ADX (Average Directional Index) - do suc manh xu huong, khong do huong.
    ADX > 25 = trend manh  |  ADX < 20 = sideways / choppy
    Tra ve: (adx, +DI, -DI)  — +DI > -DI: xu huong tang | -DI > +DI: xu huong giam
    """
    if len(closes) < period * 2 + 1:
        return 0.0, 0.0, 0.0
    pdm, mdm, trs = [], [], []
    for i in range(1, len(closes)):
        up   = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        pdm.append(up   if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    def _wilder(arr, n):
        s = sum(arr[:n]); out = [s]
        for v in arr[n:]:
            s = s - s/n + v; out.append(s)
        return out
    sp = _wilder(pdm, period); sm = _wilder(mdm, period); st = _wilder(trs, period)
    pdi = [100*p/t if t > 1e-10 else 0.0 for p, t in zip(sp, st)]
    mdi = [100*m/t if t > 1e-10 else 0.0 for m, t in zip(sm, st)]
    dx  = [100*abs(p-m)/(p+m) if (p+m) > 1e-10 else 0.0 for p, m in zip(pdi, mdi)]
    if len(dx) < period:
        return 0.0, pdi[-1] if pdi else 0.0, mdi[-1] if mdi else 0.0
    adx_v = sum(dx[:period]) / period
    for v in dx[period:]:
        adx_v = (adx_v*(period-1) + v) / period
    return float(adx_v), float(pdi[-1]), float(mdi[-1])

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

    Dung 200 nen (thay vi 50) de ket qua on dinh ve mat thong ke.
    Hurst tren 50 nen: dao dong lon, khong dang tin.
    Hurst tren 200 nen: xap xi dung duoc (Peters 1994).
    """
    n = min(len(closes), 200)
    if n < 50:
        return 0.5
    ts   = np.array(closes[-n:], dtype=float)
    # Dung nhieu lag hon khi co du data — giam nhieu trong uoc luong
    max_lag = min(n // 4, 50)
    lags = list(range(2, max_lag))
    if len(lags) < 5:
        return 0.5
    tau  = [np.std(ts[lag:] - ts[:-lag]) for lag in lags]
    tau  = np.array(tau)
    valid = tau > 1e-10
    if valid.sum() < 5:
        return 0.5
    poly = np.polyfit(np.log(np.array(lags)[valid]), np.log(tau[valid]), 1)
    return float(np.clip(poly[0], 0.0, 1.0))

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
    Tin hieu lien thi truong (intermarket analysis).
    Du lieu: DXY (suc manh USD) va Oil (WTI — proxy hang hoa/risk sentiment).

    Nguyen tac:
    - DXY tang → USD manh → USD/* tang, */USD giam, Vang/Bac giam
    - Oil tang → CAD manh (xuat khau dau) → USD/CAD giam
    - Oil tang = risk-on → carry trade (AUD/JPY, CAD/JPY) tang
    - EUR/GBP, GBP/JPY: ca hai phia deu co safe-haven rieng → return 0 tranh nhieu
    """
    im  = fetch_intermarket()
    dxy = im.get('dxy', 0.0)
    oil = im.get('oil', 0.0)

    # Hang hoa truc tiep
    if sym in ('USOIL/USD', 'UKOIL/USD'):
        return oil
    if sym == 'XAU/USD':
        return -dxy   # Vang nguoc chieu USD

    # USD/CAD: DXY va Oil cung tac dong (CAD la dong tien dau mo)
    if sym == 'USD/CAD':
        return float(np.clip(dxy*0.5 - oil*0.5, -1.0, 1.0))

    # Carry trade: Oil tang = risk-on → AUD/CAD/JPY tang so voi JPY
    if sym == 'AUD/JPY':
        return float(np.clip(oil*0.4, -1.0, 1.0))
    if sym == 'CAD/JPY':
        return float(np.clip(oil*0.6, -1.0, 1.0))   # CAD nhay cam nhat voi oil

    # USD truc tiep
    if sym.startswith('USD/'):
        return dxy
    if sym.endswith('/USD'):
        return -dxy

    # CHF crosses + EUR/GBP + JPY vs non-commodity: ca hai phia di chuyen cung chieu
    # khi risk event xay ra → DXY anh huong qua nho → return 0 tranh nhieu
    return 0.0

# ── Phuong trinh macro XAU/USD ───────────────────────────────
def fetch_gold_macro():
    """
    Lay 4 nhan to macro anh huong den XAU/USD, cache 1 lan moi phien.
      DXY  (UUP)  — da co trong _im_cache, tai su dung
      10Y  (^TNX) — US Treasury Yield, nghich chieu Vang (co hoi)
      VIX  (^VIX) — Chi so so hai, cung chieu Vang (safe haven)
      Oil  (CL=F) — da co trong _im_cache, cung chieu nhe (lam phat)
    """
    global _gold_cache
    if _gold_cache:
        return _gold_cache
    im = fetch_intermarket()
    _gold_cache = {
        'dxy_raw': im.get('dxy', 0.0),
        'oil_raw': im.get('oil', 0.0),
        'tny_s':   0.0,
        'vix_s':   0.0,
    }
    # US 10-Year Treasury Yield — nghich chieu: yield tang → Vang giam (co hoi tai chinh)
    try:
        tny = yf.Ticker('^TNX').history(period='7d', interval='1d')['Close'].dropna()
        if len(tny) >= 3:
            delta = float(tny.iloc[-1] - tny.iloc[-3])   # thay doi trong 3 phien (% point)
            # +0.20 pp yield → Vang giam ~1% → score = -0.7
            _gold_cache['tny_s'] = float(np.clip(-delta * 3.5, -1.0, 1.0))
    except Exception:
        pass
    # VIX — safe haven: tang khi thi truong so, Vang huong loi
    try:
        vix = yf.Ticker('^VIX').history(period='7d', interval='1d')['Close'].dropna()
        if len(vix) >= 3:
            vix_now = float(vix.iloc[-1])
            vix_chg = (vix_now - float(vix.iloc[-3])) / max(float(vix.iloc[-3]), 1.0)
            # Level bonus: VIX>25 = panic (mua Vang manh), VIX<15 = risk-on (ban Vang)
            level = 0.30 if vix_now > 25 else (0.15 if vix_now > 20 else (-0.10 if vix_now < 15 else 0.0))
            _gold_cache['vix_s']   = float(np.clip(vix_chg * 2.5 + level, -1.0, 1.0))
            _gold_cache['vix_raw'] = vix_now   # luu gia tri goc de Fear&Greed fallback
    except Exception:
        pass
    return _gold_cache

def gold_macro_score():
    """
    Phuong trinh lien thi truong danh rieng XAU/USD.

    score = 0.40 * score_dxy + 0.35 * score_tny + 0.15 * score_vix + 0.10 * score_oil

    Nguyen tac:
      DXY  tang → USD manh → Vang GIAM  (nghich, w=0.40 — trong so lon nhat)
      10Y  tang → lai suat → Vang GIAM  (nghich, w=0.35 — co hoi tai chinh)
      VIX  tang → so hai   → Vang TANG  (thuan,  w=0.15 — safe haven)
      Oil  tang → lam phat → Vang TANG  (thuan nhe, w=0.10 — kenh gian tiep)

    Tra ve: (score[-1,+1], components_dict)
      +1.0 = macro ung ho manh MUA Vang
      -1.0 = macro ung ho manh BAN Vang
    """
    g = fetch_gold_macro()
    dxy_s = -g['dxy_raw']    # dao dau: DXY tang → score am (bearish Gold)
    tny_s =  g['tny_s']      # da dao dau trong fetch
    vix_s =  g['vix_s']      # cung chieu
    oil_s =  g['oil_raw']    # Oil tang → bullish Gold
    score = 0.40 * dxy_s + 0.35 * tny_s + 0.15 * vix_s + 0.10 * oil_s
    comps = {
        'dxy': round(dxy_s, 2),
        'tny': round(tny_s, 2),
        'vix': round(vix_s, 2),
        'oil': round(oil_s, 2),
    }
    return float(np.clip(score, -1.0, 1.0)), comps


# ── Phương trình macro JPY pairs ──────────────────────────────
def jpy_macro_score(sym):
    """
    Macro equation cho 5 cap JPY — tai su dung _gold_cache (zero API cost).
    score > 0: JPY suy yeu → BUY pair | score < 0: JPY manh → SELL pair

    3 sub-group theo driver chinh:
    - USD/JPY : yield differential (BoJ ~0% → TNX la proxy spread)
    - EUR/JPY, GBP/JPY : risk sentiment (JPY safe haven khi so hai)
    - AUD/JPY, CAD/JPY : carry trade (yield + oil/risk appetite)

    Sign convention (tai su dung tu gold_cache):
      raw_yield = -tny_s : duong khi US yield TANG (carry trade vao USD)
      risk_on   = -vix_s : duong khi VIX GIAM (risk-on, JPY yeu di)
      dxy_raw   = g['dxy_raw'] : duong khi DXY tang (USD manh)
      oil_raw   = g['oil_raw'] : duong khi Oil tang (risk appetite)
    """
    g = fetch_gold_macro()
    raw_yield = -g['tny_s']    # tny_s am khi yield tang → dao = duong khi carry thuan
    risk_on   = -g['vix_s']    # vix_s duong khi VIX cao → dao = duong khi risk-on
    dxy_raw   =  g['dxy_raw']
    oil_raw   =  g['oil_raw']

    if sym == 'USD/JPY':
        score = 0.50 * raw_yield + 0.30 * risk_on + 0.20 * dxy_raw
        comps = {'yield': round(raw_yield, 2), 'risk': round(risk_on, 2), 'dxy': round(dxy_raw, 2)}
    elif sym in ('EUR/JPY', 'GBP/JPY'):
        score = 0.55 * risk_on + 0.25 * raw_yield + 0.20 * oil_raw
        comps = {'risk': round(risk_on, 2), 'yield': round(raw_yield, 2), 'oil': round(oil_raw, 2)}
    elif sym in ('AUD/JPY', 'CAD/JPY'):
        score = 0.45 * risk_on + 0.35 * oil_raw + 0.20 * raw_yield
        comps = {'risk': round(risk_on, 2), 'oil': round(oil_raw, 2), 'yield': round(raw_yield, 2)}
    else:
        return 0.0, {}
    return float(np.clip(score, -1.0, 1.0)), comps


def oil_macro_score(sym):
    """
    Macro equation cho USOIL/USD va UKOIL/USD.
    Tai su dung Oil trend + VIX tu gold_cache, Oil news tu fundamental_cache.
    score > 0: macro ung ho Oil tang | score < 0: macro ung ho Oil giam
    """
    g = fetch_gold_macro()
    oil_raw  = g['oil_raw']   # Oil trend hien tai
    risk_on  = -g['vix_s']    # VIX thap = demand tot = bullish oil

    sent_key = 'USO' if sym == 'USOIL/USD' else 'UKO'
    oil_sent = _fundamental_cache.get('sentiment', {}).get(sent_key, 0.0) if _fundamental_cache else 0.0

    score = 0.50 * oil_raw + 0.30 * risk_on + 0.20 * oil_sent
    comps = {'trend': round(oil_raw, 2), 'risk': round(risk_on, 2), 'news': round(oil_sent, 2)}
    return float(np.clip(score, -1.0, 1.0)), comps


def macro_score(sym):
    """
    Router macro thong nhat — 100% cap deu co equation rieng.
    Moi cap tai su dung _gold_cache (DXY, TNX, VIX, Oil) — zero API cost.

    Tra ve (score[-1,+1], comps) hoac (None, {}) neu khong co macro.
    score > 0: macro ung ho BUY  |  score < 0: macro ung ho SELL

    Sign map (de nhat quan khi doc):
      dxy_inv = -dxy_raw  : duong khi USD yeu → bullish */USD
      yield_s =  tny_s    : duong khi yield giam → bullish gold/silver
      vix_s   =  vix_s    : duong khi VIX cao → bullish safe-haven (gold, CHF, JPY)
      oil_s   =  oil_raw  : duong khi oil tang → bullish dau, CAD, risk-on
    """
    if sym == 'XAU/USD':                          return gold_macro_score()
    if sym.endswith('/JPY'):                       return jpy_macro_score(sym)
    if sym in ('USOIL/USD', 'UKOIL/USD'):         return oil_macro_score(sym)

    g       = fetch_gold_macro()
    dxy_inv = -g['dxy_raw']
    yield_s =  g['tny_s']
    vix_s   =  g['vix_s']
    oil_s   =  g['oil_raw']

    if sym == 'USD/CHF':
        # Ca hai la safe haven; trong extreme fear CHF thang hon USD → USD/CHF giam
        dxy_raw = g['dxy_raw']
        s = 0.55*dxy_raw + 0.45*(-vix_s)
        c = {'dxy': round(dxy_raw,2), 'risk': round(-vix_s,2)}

    elif sym == 'USD/CAD':
        # CAD = dong tien dau mo: Oil tang → CAD manh → USD/CAD giam
        dxy_raw = g['dxy_raw']
        s = 0.45*dxy_raw + 0.40*(-oil_s) + 0.15*vix_s
        c = {'dxy': round(dxy_raw,2), 'oil': round(-oil_s,2), 'vix': round(vix_s,2)}

    elif sym == 'AUD/USD':
        # Risk-on commodity: DXY tang = bearish, VIX tang = bearish, Oil tang = bullish
        s = 0.45*dxy_inv + 0.35*(-vix_s) + 0.20*oil_s
        c = {'dxy': round(dxy_inv,2), 'risk': round(-vix_s,2), 'oil': round(oil_s,2)}

    elif sym == 'EUR/USD':
        # Risk-on vs USD: DXY tang = bearish; VIX spike = bearish (USD la safe haven)
        s = 0.60*dxy_inv + 0.40*(-vix_s)
        c = {'dxy': round(dxy_inv,2), 'risk': round(-vix_s,2)}

    elif sym == 'GBP/USD':
        # GBP nhay cam hon EUR voi risk-off (thanh khoan thap, BoE doc lap)
        # them GBP news sentiment (BoE, UK GDP, inflation)
        gbp_sent = _fundamental_cache.get('sentiment', {}).get('GBP', 0.0) if _fundamental_cache else 0.0
        s = 0.50*dxy_inv + 0.35*(-vix_s) + 0.15*gbp_sent
        c = {'dxy': round(dxy_inv,2), 'risk': round(-vix_s,2), 'gbp': round(gbp_sent,2)}

    else:
        return None, {}

    return float(np.clip(s, -1.0, 1.0)), c


# ── Multi-Timeframe Analysis ─────────────────────────────────

def fetch_d1_data(sym, yf_sym):
    """
    Lay du lieu D1 (daily) — 180 ngay, cache per session.
    Goi 1 lan duy nhat moi phien de tiet kiem API.
    """
    global _d1_cache
    if sym in _d1_cache:
        return _d1_cache[sym]
    try:
        df = yf.Ticker(yf_sym).history(period='180d', interval='1d')
        if df is None or len(df) < 20:
            _d1_cache[sym] = None
            return None
        closes = list(df['Close'].dropna())
        highs  = list(df['High'].dropna())
        lows   = list(df['Low'].dropna())
        n = min(len(closes), len(highs), len(lows))
        _d1_cache[sym] = {'closes': closes[:n], 'highs': highs[:n], 'lows': lows[:n]}
        return _d1_cache[sym]
    except Exception as e:
        print(f'  [D1] fetch loi {sym}: {e}')
        _d1_cache[sym] = None
        return None


def d1_trend(sym, yf_sym):
    """
    Xu huong Daily (D1) — context macro, khong phai bo loc cung.

    3 thanh phan, trong so theo do tre:
      fast_mom  (40%): dem so phien tang/giam trong 7 ngay — phan ung trong 1-2 ngay
      ema20_pos (35%): gia vs EMA20 daily — phan ung trong 5-10 ngay (EMA50 qua cham)
      structure (25%): HH/HL vs LH/LL trong 20 nen — xac nhan cau truc

    Returns: (direction: 'BULL'|'BEAR'|'NEUTRAL', score: float, details: dict)
    """
    d1 = fetch_d1_data(sym, yf_sym)
    if d1 is None:
        return 'NEUTRAL', 0.0, {}

    closes = d1['closes']
    highs  = d1['highs']
    lows   = d1['lows']
    price  = closes[-1]

    # [FAST] Momentum 7 phien: dem so phien tang/giam gan nhat
    # Phan ung trong 1-2 ngay — khac phuc do tre EMA
    window = closes[-8:] if len(closes) >= 8 else closes
    up_days = sum(1 for i in range(1, len(window)) if window[i] > window[i-1])
    total_d = len(window) - 1
    if total_d > 0:
        fast_s = 1 if up_days >= round(total_d * 0.70) else \
                (-1 if up_days <= round(total_d * 0.30) else 0)
    else:
        fast_s = 0

    # [MEDIUM] Vi tri gia vs EMA20 daily (it lag hon EMA50)
    e20   = ema(closes, 20)
    # Threshold nho de tranh flip lien tuc quanh EMA
    ema_s = 1 if price > e20 * 1.001 else (-1 if price < e20 * 0.999 else 0)

    # [SLOW] Market structure: HH+HL = uptrend | LH+LL = downtrend
    n = min(len(closes), 20)
    mid = n // 2
    if n > mid and mid > 0:
        ph = max(highs[-n:-mid]); ch = max(highs[-mid:])
        pl = min(lows[-n:-mid]);  cl = min(lows[-mid:])
        struct_s = 1 if (ch > ph and cl > pl) else (-1 if (ch < ph and cl < pl) else 0)
    else:
        struct_s = 0

    score = fast_s * 0.40 + ema_s * 0.35 + struct_s * 0.25
    direction = 'BULL' if score > 0.20 else ('BEAR' if score < -0.20 else 'NEUTRAL')
    details = {
        'fast': fast_s, 'ema': ema_s, 'struct': struct_s,
        'e20': round(e20, 5), 'up_days': up_days, 'total_days': total_d,
        'score': round(score, 2),
    }
    return direction, float(score), details


def d1_key_levels(sym, yf_sym, signal, price):
    """
    Tim muc khang cu / ho tro gan nhat tren D1 lam TP thuc te.
    Dieu nay quan trong hon ATR co dinh:
      - TP tai resistance thuc → co kha nang chot loi thuc su
      - TP tai ATR co dinh → co the bi block truoc khi chay

    BUY: tim khang cu gan nhat phia TREN gia (it nhat 0.5% tren)
    SELL: tim ho tro gan nhat phia DUOI gia (it nhat 0.5% duoi)
    Returns: float (muc key level) hoac None neu khong tim duoc.
    """
    d1 = fetch_d1_data(sym, yf_sym)
    if d1 is None:
        return None

    highs = d1['highs']
    lows  = d1['lows']

    if signal == 'BUY':
        # Khang cu = swing high D1 phia tren gia, cach it nhat 0.5%
        candidates = [h for h in highs[-90:] if h > price * 1.005]
        return min(candidates) if candidates else None
    else:
        # Ho tro = swing low D1 phia duoi gia, cach it nhat 0.5%
        candidates = [l for l in lows[-90:] if l < price * 0.995]
        return max(candidates) if candidates else None


def resample_to_h4(long_closes, long_highs, long_lows):
    """
    Resample H1 sang H4 bang cach nhom 4 nen lien tiep.
    720 H1 bars → 180 H4 bars (30 ngay H4).
    Bo nen H4 dang hinh (nhom cuoi chua du 4 nen).
    """
    n = len(long_closes)
    if n < 8:
        return [], [], []

    # Bo nhom dau neu chua du 4 nen
    remainder = n % 4
    h4_c, h4_h, h4_l = [], [], []

    for i in range(remainder, n, 4):
        grp_c = long_closes[i:i+4]
        grp_h = long_highs[i:i+4]
        grp_l = long_lows[i:i+4]
        if len(grp_c) == 4:
            h4_c.append(grp_c[-1])    # Close nen H1 cuoi = close H4
            h4_h.append(max(grp_h))   # High cao nhat trong 4 nen
            h4_l.append(min(grp_l))   # Low thap nhat trong 4 nen

    return h4_c, h4_h, h4_l


def h4_trend(long_closes, long_highs, long_lows):
    """
    Xu huong H4 (4-giờ) — xu huong trung gian xac nhan H1.
    Resample tu long_closes (720 H1 bars → 180 H4 bars).

    Returns: (direction: 'BULL'|'BEAR'|'NEUTRAL', score: float, details: dict)
    """
    h4_c, h4_h, h4_l = resample_to_h4(long_closes, long_highs, long_lows)
    if len(h4_c) < 20:
        return 'NEUTRAL', 0.0, {}

    price = h4_c[-1]
    # EMA 9/21 tren H4 (nhanh hon 20/50, phu hop voi H4)
    e9  = ema(h4_c, 9)
    e21 = ema(h4_c, 21)
    ema_s = 1 if (price > e9 > e21) else (-1 if (price < e9 < e21) else 0)

    # Market structure H4
    n = min(len(h4_c), 20)
    mid = n // 2
    if n > mid and mid > 0:
        ph = max(h4_h[-n:-mid])
        ch = max(h4_h[-mid:])
        pl = min(h4_l[-n:-mid])
        cl = min(h4_l[-mid:])
        struct_s = 1 if (ch > ph and cl > pl) else (-1 if (ch < ph and cl < pl) else 0)
    else:
        struct_s = 0

    # Momentum H4 (5 nen H4 = 20 nen H1 = xu huong 20 gio)
    mom_h4 = momentum(h4_c, n=5)
    mom_s  = 1 if mom_h4 >= 0.2 else (-1 if mom_h4 <= -0.2 else 0)

    # MACD H4
    mac_h4 = macd(h4_c)
    mac_s  = 1 if mac_h4 > 0.09 else (-1 if mac_h4 < -0.09 else 0)

    score = ema_s * 0.40 + struct_s * 0.30 + mom_s * 0.20 + mac_s * 0.10
    direction = 'BULL' if score > 0.20 else ('BEAR' if score < -0.20 else 'NEUTRAL')
    details = {
        'ema': ema_s, 'struct': struct_s, 'mom': round(mom_h4, 2),
        'macd': round(mac_h4, 2), 'score': round(score, 2),
        'bars': len(h4_c),
    }
    return direction, float(score), details


def find_sr_levels(highs, lows, closes, lookback=60, cluster_pct=0.003):
    """
    Phat hien cac muc ho tro (S) va khang cu (R) tu lich su gia.

    Thuat toan:
      1. Tim pivot high/low (2-bar confirmation moi phia)
      2. Cluster cac muc gia gan nhau (trong pham vi cluster_pct)
      3. Chi giu cluster co it nhat 2 lan cham (confirmed level)
      4. Phan loai S (duoi gia hien tai) / R (tren gia hien tai)

    Returns: list of {'price': float, 'type': 'S'|'R', 'touches': int}
             sap xep theo khoang cach gan nhat voi gia hien tai
    """
    n = min(len(highs), len(lows), len(closes), lookback + 4)
    if n < 10:
        return []

    h = highs[-n:]
    l = lows[-n:]
    price = closes[-1]
    raw_levels = []

    # Tim pivot high (2-bar confirmation moi phia: phai cao hon 2 nen trai/phai)
    for i in range(2, len(h) - 2):
        if h[i] >= h[i-1] and h[i] >= h[i-2] and h[i] > h[i+1] and h[i] > h[i+2]:
            raw_levels.append(h[i])

    # Tim pivot low (2-bar confirmation moi phia)
    for i in range(2, len(l) - 2):
        if l[i] <= l[i-1] and l[i] <= l[i-2] and l[i] < l[i+1] and l[i] < l[i+2]:
            raw_levels.append(l[i])

    if not raw_levels:
        return []

    # Cluster cac level gan nhau (trong cluster_pct %) — nhom thanh 1 zone
    raw_levels.sort()
    clusters = []
    current_cluster = [raw_levels[0]]
    for lv in raw_levels[1:]:
        if (lv - current_cluster[0]) / current_cluster[0] <= cluster_pct:
            current_cluster.append(lv)
        else:
            clusters.append(current_cluster)
            current_cluster = [lv]
    clusters.append(current_cluster)

    # Tao level cuoi: gia trung binh cluster, can it nhat 2 lan cham
    result = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        avg_price  = sum(cluster) / len(cluster)
        level_type = 'R' if avg_price > price else 'S'
        result.append({'price': avg_price, 'type': level_type, 'touches': len(cluster)})

    # Sap xep theo khoang cach gan nhat (level gan gia hien tai = quan trong nhat)
    result.sort(key=lambda x: abs(x['price'] - price))
    return result


def find_fib_levels(h4_c, h4_h, h4_l, signal, lookback=25):
    """
    Tinh Fibonacci Retracement va Extension tu swing H4 gan nhat.

    BUY pullback:  swing base = LL truoc top, swing top = HH gan nhat > price
      Retracement: top - ratio * range  (38.2 / 50 / 61.8%)
      Extension:   base + ratio * range (127.2 / 161.8% — lam TP target)

    SELL bounce:   swing top = HH truoc bottom, swing base = LL gan nhat < price
      Retracement: base + ratio * range (38.2 / 50 / 61.8%)
      Extension:   top  - ratio * range (127.2 / 161.8% — lam TP target)

    Golden Zone (38.2–61.8%): vung pullback ly tuong — entry xac nhan.
    Returns dict hoac {} neu khong xac dinh duoc swing ro rang.
    """
    n = min(len(h4_c), lookback)
    if n < 8:
        return {}

    price  = h4_c[-1]
    h_seg  = list(h4_h[-n:])
    l_seg  = list(h4_l[-n:])
    sz     = len(h_seg)

    if signal == 'BUY':
        # Tim swing HIGH gan nhat ma price da pullback XUONG DUOI
        top_idx = None
        for i in range(sz - 2, 1, -1):
            if h_seg[i] >= h_seg[i-1] and h_seg[i] > h_seg[min(i+1, sz-1)] and h_seg[i] > price:
                top_idx = i; break
        if top_idx is None:  # Fallback: HH cua toan segment
            hh = max(range(sz), key=lambda x: h_seg[x])
            if h_seg[hh] > price:
                top_idx = hh
        if top_idx is None:
            return {}

        top_val  = h_seg[top_idx]
        base_val = min(l_seg[:top_idx]) if top_idx > 0 else l_seg[0]

        swing_range = top_val - base_val
        if swing_range < price * 0.003 or price <= base_val:
            return {}

        retrace_pct = (top_val - price) / swing_range
        r382 = top_val - 0.382 * swing_range
        r500 = top_val - 0.500 * swing_range
        r618 = top_val - 0.618 * swing_range
        e1272 = base_val + 1.272 * swing_range
        e1618 = base_val + 1.618 * swing_range

    else:  # SELL
        # Tim swing LOW gan nhat ma price da bounce LEN TREN
        bot_idx = None
        for i in range(sz - 2, 1, -1):
            if l_seg[i] <= l_seg[i-1] and l_seg[i] < l_seg[min(i+1, sz-1)] and l_seg[i] < price:
                bot_idx = i; break
        if bot_idx is None:
            ll = min(range(sz), key=lambda x: l_seg[x])
            if l_seg[ll] < price:
                bot_idx = ll
        if bot_idx is None:
            return {}

        base_val = l_seg[bot_idx]
        top_val  = max(h_seg[:bot_idx]) if bot_idx > 0 else h_seg[0]

        swing_range = top_val - base_val
        if swing_range < price * 0.003 or price >= top_val:
            return {}

        retrace_pct = (price - base_val) / swing_range
        r382 = base_val + 0.382 * swing_range
        r500 = base_val + 0.500 * swing_range
        r618 = base_val + 0.618 * swing_range
        e1272 = top_val - 1.272 * swing_range
        e1618 = top_val - 1.618 * swing_range

    retrace_pct = float(np.clip(retrace_pct, 0.0, 1.0))

    if   0.382 <= retrace_pct <= 0.618: zone = 'golden'
    elif retrace_pct < 0.236:           zone = 'too_shallow'
    elif retrace_pct < 0.382:           zone = 'shallow'
    elif retrace_pct <= 0.786:          zone = 'deep'
    else:                               zone = 'extreme'

    return {
        'retrace_pct': round(retrace_pct, 3),
        'in_golden':   zone == 'golden',
        'zone':        zone,
        'r382':   round(r382,  5),
        'r500':   round(r500,  5),
        'r618':   round(r618,  5),
        'e1272':  round(e1272, 5),
        'e1618':  round(e1618, 5),
        'swing_base':  round(base_val,    5),
        'swing_top':   round(top_val,     5),
        'swing_range': round(swing_range, 5),
    }


# ── Fundamental Intelligence Layer ───────────────────────────
def _fetch_rss_headlines(url):
    """Lay headlines tu RSS feed (RSS 2.0 + Atom), tra ve list chuoi lowercase."""
    try:
        import xml.etree.ElementTree as ET
        headers = {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/124.0 Safari/537.36'),
            'Accept': 'application/rss+xml, application/xml, text/xml, */*',
        }
        r = requests.get(url, timeout=10, headers=headers)
        if r.status_code != 200 or not r.content:
            return []
        root = ET.fromstring(r.content)
        # RSS 2.0: <item><title>
        items = root.findall('.//item')
        if items:
            return [i.findtext('title', '').lower() for i in items][:40]
        # Atom: <entry><title>
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        entries = root.findall('.//atom:entry', ns) or root.findall('.//{http://www.w3.org/2005/Atom}entry')
        if entries:
            return [
                (e.findtext('{http://www.w3.org/2005/Atom}title') or '').lower()
                for e in entries
            ][:40]
        return []
    except Exception:
        return []

def fetch_fundamental(now):
    """
    Tai 3 nguon du lieu co ban, cache lai cho toan bo phien:
      1. Economic Calendar (Twelve Data) — su kien high/medium-impact hom nay
      2. News Sentiment (RSS Reuters + FXStreet) — xu huong tin tuc moi nhat
      3. Fear & Greed Index (CNN) — tram thai cam xuc thi truong (0-100)
    """
    global _fundamental_cache
    if _fundamental_cache:
        return _fundamental_cache

    print('  [Fundamental] Dang tai: Calendar / Sentiment / Fear&Greed ...')

    # --- 1. Economic Calendar (Twelve Data) ---
    calendar_events = []
    if TWELVE_DATA_KEY:
        try:
            date_str = now.strftime('%Y-%m-%d')
            r = requests.get(
                'https://api.twelvedata.com/economic_calendar',
                params={
                    'start_date': f'{date_str} 00:00:00',
                    'end_date':   f'{date_str} 23:59:59',
                    'importance': 'high,medium',
                    'apikey':     TWELVE_DATA_KEY,
                },
                timeout=10
            )
            # Twelve Data doi khi tra ve JSON bi hong (null prefix, double-object...)
            # Dung text parse truc tiep, fallback sang {} neu loi
            try:
                raw_data = r.json()
            except Exception:
                import re as _re
                m = _re.search(r'\{.*\}', r.text, _re.DOTALL)
                raw_data = json.loads(m.group()) if m else {}
            events_list = raw_data.get('result', {}).get('list', []) if isinstance(raw_data, dict) else []
            for ev in events_list:
                try:
                    ev_dt = datetime.strptime(
                        f'{ev.get("date","")} {ev.get("time","00:00:00")}',
                        '%Y-%m-%d %H:%M:%S'
                    ).replace(tzinfo=timezone.utc)
                    calendar_events.append({
                        'title':      ev.get('event', ''),
                        'country':    ev.get('country', ''),
                        'datetime':   ev_dt,
                        'importance': ev.get('importance', 'low').lower(),
                    })
                except Exception:
                    pass
        except Exception as e:
            print(f'  [Fundamental] Calendar loi: {e}')

    # --- 2. News Sentiment (RSS) ---
    headlines = []
    for url in [
        'https://feeds.reuters.com/reuters/businessNews',
        'https://feeds.reuters.com/reuters/topNews',
        'https://rss.fxstreet.com/news',
        'https://forexlive.com/feed/',
        'https://www.dailyfx.com/feeds/all',
    ]:
        h = _fetch_rss_headlines(url)
        if h:
            headlines.extend(h)
            print(f'  [RSS] {len(h)} headlines tu {url.split("/")[2]}')
    if not headlines:
        print('  [RSS] Khong lay duoc headlines tu bat ky feed nao')

    currency_sentiment = {}
    for ccy, keywords in _CURRENCY_KEYWORDS.items():
        relevant = [h for h in headlines if any(k in h for k in keywords)]
        if not relevant:
            currency_sentiment[ccy] = 0.0
            continue
        score = 0.0
        for h in relevant:
            bull = sum(1 for w in _BULLISH_WORDS if w in h)
            bear = sum(1 for w in _BEARISH_WORDS if w in h)
            score += bull - bear
        currency_sentiment[ccy] = float(np.clip(score / max(len(relevant), 1) / 2, -1.0, 1.0))

    # --- 3. Fear & Greed Index (CNN) + VIX fallback ---
    fear_greed = {'value': 50.0, 'label': 'Neutral'}
    _fg_ok = False
    for _fg_url in [
        'https://production.dataviz.cnn.io/index/fearandgreed/graphdata/',
        'https://production.dataviz.cnn.io/index/fearandgreed/graphdata',
    ]:
        try:
            r = requests.get(
                _fg_url, timeout=8,
                headers={
                    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                   'AppleWebKit/537.36 Chrome/124.0 Safari/537.36'),
                    'Referer': 'https://www.cnn.com/markets/fear-and-greed',
                    'Accept': 'application/json, */*',
                },
            )
            if r.status_code == 200 and r.text.strip():
                fg = r.json()['fear_and_greed']
                fear_greed = {
                    'value': float(fg['score']),
                    'label': fg['rating'].replace('_', ' ').title(),
                }
                _fg_ok = True
                break
        except Exception:
            pass
    if not _fg_ok:
        # Fallback: uoc luong Fear&Greed tu VIX (da fetch truoc trong gold_macro)
        _vix_raw = _gold_cache.get('vix_raw', 20.0) if _gold_cache else 20.0
        if _vix_raw > 35:
            fear_greed = {'value': 10.0, 'label': 'Extreme Fear'}
        elif _vix_raw > 28:
            fear_greed = {'value': 25.0, 'label': 'Fear'}
        elif _vix_raw > 22:
            fear_greed = {'value': 38.0, 'label': 'Fear'}
        elif _vix_raw < 12:
            fear_greed = {'value': 85.0, 'label': 'Extreme Greed'}
        elif _vix_raw < 16:
            fear_greed = {'value': 70.0, 'label': 'Greed'}
        else:
            fear_greed = {'value': 50.0, 'label': 'Neutral'}
        print(f'  [Fundamental] Fear&Greed: CNN loi — dung VIX={_vix_raw:.0f} '
              f'→ {fear_greed["value"]:.0f} ({fear_greed["label"]})')

    _fundamental_cache = {
        'calendar':   calendar_events,
        'sentiment':  currency_sentiment,
        'fear_greed': fear_greed,
        'n_headlines': len(headlines),
    }
    fg = fear_greed
    print(f'  [Fundamental] OK — {len(calendar_events)} su kien | '
          f'{len(headlines)} headlines | F&G={fg["value"]:.0f} ({fg["label"]})')
    return _fundamental_cache


def check_calendar(fund, sym, now):
    """
    Kiem tra lich kinh te co anh huong den cap tien khong.
    Tra ve: ('HARD', reason) | ('SOFT', reason) | ('PASS', '')
      HARD: su kien high-impact trong 60p → block hoan toan
      SOFT: su kien medium-impact trong 30p → can them 1 vote
    """
    try:
        base, quote = sym.split('/')
    except ValueError:
        return 'PASS', ''
    relevant = {_CURRENCY_COUNTRY.get(base[:3], ''), _CURRENCY_COUNTRY.get(quote[:3], '')} - {''}
    for ev in fund.get('calendar', []):
        if ev['country'] not in relevant:
            continue
        mins = (ev['datetime'] - now).total_seconds() / 60
        if ev['importance'] == 'high' and -15 <= mins <= 60:
            direction = 'vua qua' if mins < 0 else f'con {int(mins)}p'
            return 'HARD', f'{ev["title"]} ({ev["country"]}, {direction})'
        if ev['importance'] == 'medium' and 0 <= mins <= 30:
            return 'SOFT', f'{ev["title"]} ({ev["country"]}, con {int(mins)}p)'
    return 'PASS', ''


def get_sentiment_score(fund, sym):
    """
    Tinh sentiment score cho cap tien tu headline RSS.
    Logic: score_cap = sentiment_dong_tien_co_so - sentiment_dong_tien_dinh_gia
    EUR/USD BUY tot khi: EUR bullish (+) va/hoac USD bearish (-) → score duong
    Tra ve float [-1, +1].
    """
    try:
        base, quote = sym.split('/')
        sent  = fund.get('sentiment', {})
        base_s  = sent.get(base[:3],  0.0)
        quote_s = sent.get(quote[:3], 0.0)
        return float(np.clip(base_s - quote_s, -1.0, 1.0))
    except Exception:
        return 0.0


def get_fg_context(fund, sym, signal):
    """
    Danh gia tac dong Fear & Greed Index len tin hieu.
    Extreme Fear + tin hieu risk-on → penalty (can them xac nhan)
    Extreme Greed + tin hieu risk-off → penalty (can them xac nhan)
    Cung chieu → bonus (ghi nhan trong tin nhan, khong anh huong vote)
    Tra ve (penalty: int, label: str)
    """
    fg_val = fund.get('fear_greed', {}).get('value', 50.0)
    extreme_fear  = fg_val < 25
    extreme_greed = fg_val > 75

    is_risk_on  = (sym in _RISK_ON_BUYS  and signal == 'BUY') or \
                  (sym in _RISK_OFF_BUYS and signal == 'SELL')
    is_risk_off = (sym in _RISK_OFF_BUYS and signal == 'BUY') or \
                  (sym in _RISK_ON_BUYS  and signal == 'SELL')

    if extreme_fear and is_risk_on:
        return 1, f'F&G={fg_val:.0f} (Extreme Fear) — thi truong so, risk-on gap rui ro'
    if extreme_greed and is_risk_off:
        return 1, f'F&G={fg_val:.0f} (Extreme Greed) — thi truong tham lam, safe-haven qua dat'
    return 0, ''


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
    if rsi_v > 0:   # RSI <= 45: vung qua ban → MUA
        lines.append(f'- H1 RSI={rsi_val:.0f} (vùng quá bán — hỗ trợ MUA)')
    elif rsi_v < 0: # RSI >= 55: vung qua mua → BAN
        lines.append(f'- H1 RSI={rsi_val:.0f} (vùng quá mua — hỗ trợ BÁN)')
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
def analyze(sym, yf_sym, now=None):
    """
    He thong bieu quyet: moi chi bao bau +1 (MUA) / -1 (BAN) / 0 (trung tinh).
    Can it nhat 3/5 phieu cung chieu de phat tin hieu.
    Thay the Composite Score + OLS (qua phuc tap, can du lieu lon).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        cfg = PAIR_CONFIG.get(sym, _DEFAULT_CONFIG)
        closes, highs, lows, timestamps = fetch_ohlcv(sym, yf_sym)
        if closes is None or len(closes) < 60:
            print(f'  [D] du lieu qua it hoac loi fetch')
            return None
        n = min(len(closes), len(highs), len(lows), len(timestamps))
        closes, highs, lows, timestamps = closes[:n], highs[:n], lows[:n], timestamps[:n]
        if n < 60:
            return None

        # Merge vao lich su tich luy va lay long series de phan tich
        new_bars = [{'t': t, 'c': c, 'h': h, 'l': l}
                    for t, c, h, l in zip(timestamps, closes, highs, lows)]
        if sym not in _price_history:
            _price_history[sym] = {'bars': []}
        _price_history[sym]['bars'] = _merge_bars(_price_history[sym].get('bars', []), new_bars)

        long_bars   = _price_history[sym]['bars']
        long_closes = [b['c'] for b in long_bars]
        long_highs  = [b['h'] for b in long_bars]
        long_lows   = [b['l'] for b in long_bars]
        ln = min(len(long_closes), len(long_highs), len(long_lows))
        long_closes = long_closes[:ln]
        long_highs  = long_highs[:ln]
        long_lows   = long_lows[:ln]
        if ln < 60:
            return None

        price = long_closes[-1]

        # [LOC 1] ATR filter: bo qua thi truong qua phang
        atr_val = atr(long_highs, long_lows, long_closes)
        if atr_val < price * 0.00015:
            print(f'  [D] ATR={atr_val:.6f} loc phang')
            return None

        # [LOC 2] Sanity check gia — phat hien du lieu sai (wrong contract, nan, spike)
        lo, hi = PRICE_SANITY.get(sym, (0.0, float('inf')))
        if not (lo <= price <= hi):
            print(f'  [D] Gia {price} ngoai vung hop le [{lo}, {hi}] — du lieu sai, bo qua')
            return None

        # [TANG 1 — FUNDAMENTAL] Economic Calendar: block truoc khi tinh toan nang
        fund = fetch_fundamental(now)
        cal_status, cal_reason = check_calendar(fund, sym, now)
        if cal_status == 'HARD':
            print(f'  [D] Calendar HARD block: {cal_reason}')
            return None

        # [HURST] Dung long_closes (toi da 200 nen) de ket qua on dinh hon
        H      = hurst_exponent(long_closes)
        regime = 'TREND' if H > 0.55 else ('RANGE' if H < 0.45 else 'NEUTRAL')

        # [LOC 3] Block RANGE sau: nguong H rieng tung cap
        if H < cfg['hurst_block']:
            print(f'  [D] H={H:.3f} < {cfg["hurst_block"]} RANGE sau ({ln} bars), bo qua')
            return None

        # [LOC 3b] ADX filter: chan sideways kep — ca ADX lan Hurst deu yeu
        adx_val, pdi, mdi = adx_indicator(long_highs, long_lows, long_closes)
        if adx_val < 15 and H < 0.50:
            print(f'  [D] ADX={adx_val:.1f} + H={H:.3f} ca hai yeu — sideways kep, bo qua')
            return None

        # [INTERMARKET] Tin hieu lien thi truong
        im_s = intermarket_signal(sym)

        # --- Chi bao ky thuat (dung long history cho warmup tot hon) ---
        r_val = rsi(long_closes)
        e20   = ema(long_closes, 20)
        e50   = ema(long_closes, 50)
        mac_s = macd(long_closes)
        upper, _, lower = bollinger(long_closes)
        mom_s = momentum(long_closes)

        # [H4] Phan tich TRUOC bieu quyet — context cho Pullback Mode
        h4_dir, h4_score_val, h4_det = h4_trend(long_closes, long_highs, long_lows)

        # --- H4 Pullback Mode: vote theo context xu huong H4 ---
        # H4 BULL: entry ly tuong la BUY pullback (RSI dip, EMA hoi lui, MACD giam nhe)
        #   RSI nguong mo rong den 58 — RSI 45-58 trong uptrend = pullback chua ve qua ban
        #   EMA: gia > EMA50 nhung < EMA20 = pullback trong uptrend → neutral (khong phat SELL)
        #   MACD: -0.05..0.09 = momentum hoi lui nhe → neutral (pullback, khong phai dao chieu)
        # H4 BEAR: logic nguoc lai cho SELL bounce
        if h4_dir == 'BULL':
            rsi_buy_thr = min(cfg['rsi_buy'] + 10, 58)
            rsi_v = 1 if r_val <= rsi_buy_thr else (-1 if r_val >= cfg['rsi_sell'] else 0)
            ema_v = (1 if price > e20 > e50
                     else 0 if price > e50   # pullback duoi EMA20, van tren EMA50
                     else -1 if price < e20 < e50 else 0)
            mac_v = 1 if mac_s > 0.09 else (0 if mac_s >= -0.05 else -1)
        elif h4_dir == 'BEAR':
            rsi_sell_thr = max(cfg['rsi_sell'] - 10, 42)
            rsi_v = -1 if r_val >= rsi_sell_thr else (1 if r_val <= cfg['rsi_buy'] else 0)
            ema_v = (-1 if price < e20 < e50
                     else 0 if price < e50   # bounce tren EMA20, van duoi EMA50
                     else 1 if price > e20 > e50 else 0)
            mac_v = -1 if mac_s < -0.09 else (0 if mac_s <= 0.05 else 1)
        else:  # NEUTRAL — nguong chuan
            rsi_v = 1 if r_val <= cfg['rsi_buy'] else (-1 if r_val >= cfg['rsi_sell'] else 0)
            ema_v = (1 if price > e20 > e50 else -1 if price < e20 < e50 else 0)
            mac_v = 1 if mac_s > 0.09 else (-1 if mac_s < -0.09 else 0)

        bb_v  = (1 if price < lower else -1 if price > upper else 0)
        mom_v = (1 if mom_s >= 0.2 else -1 if mom_s <= -0.2 else 0)

        votes     = [rsi_v, ema_v, mac_v, bb_v, mom_v]
        vote_lbls = ['RSI', 'EMA', 'MACD', 'BB', 'Mom']
        bull_cnt  = sum(v for v in votes if v > 0)
        bear_cnt  = sum(-v for v in votes if v < 0)

        # Pre-filter: phai co it nhat 2 phieu mot chieu moi phan tich tiep
        if max(bull_cnt, bear_cnt) < 2:
            print(f'  [D] BUY={bull_cnt} BEAR={bear_cnt} — qua it phieu, skip')
            return None

        # H4 dieu chinh nguong phieu: THUAN chieu → ha 1 | NGUOC chieu → nang 1
        prov_dir = 'BUY' if bull_cnt >= bear_cnt else 'SELL'
        h4_aligned = (prov_dir == 'BUY' and h4_dir == 'BULL') or \
                     (prov_dir == 'SELL' and h4_dir == 'BEAR')
        h4_opposed = (prov_dir == 'BUY' and h4_dir == 'BEAR') or \
                     (prov_dir == 'SELL' and h4_dir == 'BULL')

        min_v = cfg['min_votes']
        if h4_aligned:
            min_v = max(2, min_v - 1)
        elif h4_opposed:
            min_v = min(5, min_v + 1)

        # RANGE regime: thi truong ngang, tin hieu chi bao khong dang tin — can them 1 vote
        # Day la bao ve chinh: du hurst_block da cao, van co H vua qua nguong nhung van RANGE
        if regime == 'RANGE':
            min_v = min(5, min_v + 1)

        # TREND regime + H4 nguoc chieu: counter-trend trong xu huong manh = bay nguy hiem
        # H > 0.55 = momentum ro rang — vao nguoc chieu la no tien, block cung
        if regime == 'TREND' and h4_opposed:
            print(f'  [D] TREND regime + H4 nguoc chieu — counter-trend nguy hiem, bo qua')
            return None

        if cal_status == 'SOFT':
            min_v = min(5, min_v + 1)
            print(f'  [!] Calendar SOFT: {cal_reason} — min_votes={min_v}')

        if bull_cnt >= min_v:
            signal     = 'BUY'
            vote_count = bull_cnt
        elif bear_cnt >= min_v:
            signal     = 'SELL'
            vote_count = bear_cnt
        else:
            print(f'  [D] BUY={bull_cnt} BEAR={bear_cnt} — chua du {min_v}/5 (H4={h4_dir})')
            return None

        # [D1] Chi dung de lay key levels (TP/SL) — KHONG dung lam bo loc chieu
        # D1 direction chi hien thi trong Telegram lam tham khao, khong anh huong entry
        d1_dir, d1_score_val, d1_det = d1_trend(sym, yf_sym)

        # Cap nhat aligned/opposed theo signal chinh thuc (chi dung cho confidence)
        d1_aligned = (signal == 'BUY' and d1_dir == 'BULL') or \
                     (signal == 'SELL' and d1_dir == 'BEAR')
        d1_opposed = (signal == 'BUY' and d1_dir == 'BEAR') or \
                     (signal == 'SELL' and d1_dir == 'BULL')
        h4_aligned = (signal == 'BUY' and h4_dir == 'BULL') or \
                     (signal == 'SELL' and h4_dir == 'BEAR')
        h4_opposed = (signal == 'BUY' and h4_dir == 'BEAR') or \
                     (signal == 'SELL' and h4_dir == 'BULL')

        # [H4 S/R] Phat hien ho tro / khang cu tu lich su H4
        h4_s_c, h4_s_h, h4_s_l = resample_to_h4(long_closes, long_highs, long_lows)
        sr_levels = find_sr_levels(h4_s_h, h4_s_l, h4_s_c, lookback=60) if len(h4_s_c) >= 10 else []
        nearest_s = nearest_r = None
        for lv in sr_levels:
            if lv['type'] == 'S' and nearest_s is None:
                nearest_s = lv
            if lv['type'] == 'R' and nearest_r is None:
                nearest_r = lv
            if nearest_s and nearest_r:
                break
        near_support    = nearest_s is not None and (price - nearest_s['price']) / price < 0.005
        near_resistance = nearest_r is not None and (nearest_r['price'] - price) / price < 0.005

        # [FIB] Fibonacci Retracement / Extension tu swing H4
        fib = find_fib_levels(h4_s_c, h4_s_h, h4_s_l, signal, lookback=25)

        # [LOC 4] RSI extreme contradiction
        rsi_contradicts = (r_val < 35 and signal == 'SELL') or (r_val > 65 and signal == 'BUY')
        if rsi_contradicts and vote_count < max(4, min_v):
            print(f'  [D] RSI={r_val:.0f} cuc doan mau thuan {signal}')
            return None

        # [PAIR MACRO] macro rieng tung cap (tai su dung _gold_cache)
        pair_macro = None
        m_score, m_comps = macro_score(sym)
        if m_comps:
            sig_dir     = 1 if signal == 'BUY' else -1
            macro_align = m_score * sig_dir
            if macro_align < -0.30:
                print(f'  [D] Macro={m_score:.2f} mau thuan {signal} — {m_comps}')
                return None
            if macro_align < -0.12 and vote_count < min(5, min_v + 1):
                print(f'  [D] Macro={m_score:.2f} mau thuan ro, can them 1 vote')
                return None
            pair_macro = {'score': round(m_score, 2), **m_comps}

        # [TANG 2 — NEWS SENTIMENT]
        sent_score = get_sentiment_score(fund, sym)
        sig_dir    = 1 if signal == 'BUY' else -1
        sent_align = sent_score * sig_dir
        if sent_align < -0.35:
            print(f'  [D] Sentiment={sent_score:.2f} mau thuan manh voi {signal}')
            return None
        if sent_align < -0.15 and vote_count < min(5, min_v + 1):
            print(f'  [D] Sentiment={sent_score:.2f} mau thuan nhe, can them vote')
            return None

        # [TANG 3 — FEAR & GREED]
        fg_penalty, fg_reason = get_fg_context(fund, sym, signal)
        if fg_penalty and vote_count < min(5, min_v + 1):
            print(f'  [D] F&G: {fg_reason}')
            return None

        aligned_lbls = [vote_lbls[i] for i, v in enumerate(votes)
                        if (v > 0 and signal == 'BUY') or (v < 0 and signal == 'SELL')]

        # --- Do tin cay ---
        base_conf    = {2: 50, 3: 60, 4: 75, 5: 90}.get(vote_count, 60)
        im_aligned   = (im_s > 0.15 and signal == 'BUY') or (im_s < -0.15 and signal == 'SELL')
        im_bonus     = 5 if im_aligned else 0
        regime_bonus = 5 if regime == 'TREND' else (3 if regime == 'RANGE' else 0)
        history_bonus = 2 if ln >= 200 else 0
        mtf_bonus = (5 if h4_aligned else (-3 if h4_opposed else 0)) + \
                    (3 if d1_aligned else (-2 if d1_opposed else 0))
        # S/R bonus: vao lenh gan ho tro (BUY) / khang cu (SELL) = confluence tot
        # S/R penalty: vao lenh sap vao khang cu (BUY) / ho tro (SELL) = chong muc
        sr_bonus = (5 if (signal == 'BUY' and near_support) or (signal == 'SELL' and near_resistance)
                    else (-3 if (signal == 'BUY' and near_resistance) or (signal == 'SELL' and near_support)
                    else 0))
        # Fibonacci bonus: golden zone (38.2-61.8%) = vung pullback ly tuong
        # extreme zone (>78.6%) = pullback qua sau, co the dao chieu
        fib_zone = fib.get('zone') if fib else None
        fib_bonus = (8 if fib_zone == 'golden'
                     else 3 if fib_zone == 'shallow'
                     else 2 if fib_zone == 'deep'
                     else -5 if fib_zone == 'extreme'
                     else 0)
        conf = min(95, base_conf + im_bonus + regime_bonus + history_bonus + mtf_bonus + sr_bonus + fib_bonus)

        # Wyckoff phase
        phase_name = wyckoff_phase(regime, signal, r_val)

        # SL dua tren Swing High/Low (cau truc thi truong) — tot hon ATR co dinh
        # Lay dinh/day cua 10 nen gan nhat lam nguong invalidation thuc te
        swing_low  = min(long_lows[-10:])
        swing_high = max(long_highs[-10:])
        swing_dist = (price - swing_low) if signal == 'BUY' else (swing_high - price)
        # Dam bao SL it nhat bang 1.5×ATR (tranh SL qua chat bi stop-hunt)
        sl_dist  = max(atr_val * 1.5, swing_dist)
        tp_dist  = sl_dist * 2.0   # RR 1:2 mac dinh
        tp2_dist = sl_dist * 3.0   # RR 1:3 mac dinh
        if signal == 'BUY':
            sl = price - sl_dist; tp = price + tp_dist; tp2 = price + tp2_dist
        else:
            sl = price + sl_dist; tp = price - tp_dist; tp2 = price - tp2_dist

        # Dieu chinh TP theo H4 S/R (chinh xac hon D1 cho H1 trading)
        h4_sr_tp = None
        h4_sr_used = False
        if signal == 'BUY' and nearest_r is not None:
            r_price = nearest_r['price']
            if price < r_price < tp:
                new_tp_h4 = r_price * 0.998
                new_rr_h4 = (new_tp_h4 - price) / sl_dist if sl_dist > 0 else 0
                if new_rr_h4 >= 1.2:
                    tp = new_tp_h4; tp2 = r_price; h4_sr_tp = r_price; h4_sr_used = True
                    tp_dist = tp - price; tp2_dist = tp2 - price
        elif signal == 'SELL' and nearest_s is not None:
            s_price = nearest_s['price']
            if tp < s_price < price:
                new_tp_h4 = s_price * 1.002
                new_rr_h4 = (price - new_tp_h4) / sl_dist if sl_dist > 0 else 0
                if new_rr_h4 >= 1.2:
                    tp = new_tp_h4; tp2 = s_price; h4_sr_tp = s_price; h4_sr_used = True
                    tp_dist = price - tp; tp2_dist = price - tp2

        # Dieu chinh TP theo D1 key level (khang cu/ho tro thuc te)
        # TP tai level thuc → co kha nang chot loi cao hon TP ATR co dinh
        d1_tp_level = d1_key_levels(sym, yf_sym, signal, price)
        d1_level_used = False
        if d1_tp_level is not None:
            if signal == 'BUY' and price < d1_tp_level < tp:
                # D1 resistance gan hon TP hien tai → thu hep TP1 vao truoc resistance
                new_tp = d1_tp_level * 0.998   # Chot 0.2% truoc resistance (truot lenh)
                new_rr = (new_tp - price) / sl_dist if sl_dist > 0 else 0
                if new_rr >= 1.2:              # Chi thu hep neu RR van >= 1:1.2
                    tp        = new_tp
                    tp2       = d1_tp_level    # TP2 chinh xac tai resistance
                    tp_dist   = tp - price
                    tp2_dist  = tp2 - price
                    d1_level_used = True
            elif signal == 'SELL' and tp < d1_tp_level < price:
                # D1 support gan hon TP hien tai → thu hep TP1 vao truoc support
                new_tp = d1_tp_level * 1.002
                new_rr = (price - new_tp) / sl_dist if sl_dist > 0 else 0
                if new_rr >= 1.2:
                    tp        = new_tp
                    tp2       = d1_tp_level
                    tp_dist   = price - tp
                    tp2_dist  = price - tp2
                    d1_level_used = True

        # Fib Extension lam TP2 khi chua co S/R hoac D1 dat TP2 ro rang
        fib_tp2_used = False
        if fib and sl_dist > 0:
            # Uu tien e1618 (target chinh), fallback e1272
            for e_cand in [fib.get('e1618'), fib.get('e1272')]:
                if e_cand is None:
                    continue
                if signal == 'BUY' and e_cand > tp:
                    new_rr2 = (e_cand - price) / sl_dist
                    if new_rr2 >= 1.5:
                        tp2 = e_cand; tp2_dist = tp2 - price; fib_tp2_used = True; break
                elif signal == 'SELL' and e_cand < tp:
                    new_rr2 = (price - e_cand) / sl_dist
                    if new_rr2 >= 1.5:
                        tp2 = e_cand; tp2_dist = price - tp2; fib_tp2_used = True; break

        sl_pct  = round(sl_dist  / price * 100, 4)
        tp_pct  = round(tp_dist  / price * 100, 4)
        tp2_pct = round(tp2_dist / price * 100, 4)
        rr1     = round(tp_dist  / sl_dist, 1) if sl_dist > 0 else 0
        rr2     = round(tp2_dist / sl_dist, 1) if sl_dist > 0 else 0

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
            'phase': phase_name, 'hurst': round(H, 3), 'adx': round(adx_val, 1), 'regime': regime,
            'history_bars': ln,
            'mtf': {
                'd1_dir':   d1_dir,   'd1_score': d1_det.get('score', 0),
                'd1_ema':   d1_det.get('ema', 0), 'd1_struct': d1_det.get('struct', 0),
                'h4_dir':   h4_dir,   'h4_score': h4_det.get('score', 0),
                'h4_bars':  h4_det.get('bars', 0),
                'd1_level': round(d1_tp_level, 5) if d1_tp_level else None,
                'd1_level_used': d1_level_used,
            },
            'sr': {
                'levels':   [{'price': round(l['price'], 5), 'type': l['type'],
                              'touches': l['touches']} for l in sr_levels[:5]],
                'nearest_s':     round(nearest_s['price'], 5) if nearest_s else None,
                'nearest_r':     round(nearest_r['price'], 5) if nearest_r else None,
                'near_support':  near_support,
                'near_resistance': near_resistance,
                'h4_sr_tp':      round(h4_sr_tp, 5) if h4_sr_tp else None,
                'h4_sr_used':    h4_sr_used,
            },
            'fib': {**fib, 'tp2_used': fib_tp2_used} if fib else {'tp2_used': False},
            'pair_macro': pair_macro,
            'fundamental': {
                'sentiment':  round(sent_score, 2),
                'fear_greed': fund.get('fear_greed', {}),
                'cal_status': cal_status,
                'cal_reason': cal_reason,
            },
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

def fetch_price_range(yf_sym, since_ts, hours=1):
    """Lay gia cao nhat / thap nhat trong 'hours' tieng ke tu since_ts (UTC epoch)."""
    try:
        df = yf.Ticker(yf_sym).history(period='2d', interval='5m')
        if df is None or len(df) == 0:
            return None, None
        start = pd.Timestamp(since_ts, unit='s', tz='UTC')
        end   = pd.Timestamp(since_ts + hours * 3600, unit='s', tz='UTC')
        if df.index.tzinfo is None:
            df.index = df.index.tz_localize('UTC')
        else:
            df.index = df.index.tz_convert('UTC')
        window = df.loc[(df.index >= start) & (df.index <= end)]
        if len(window) == 0:
            return None, None
        return float(window['High'].max()), float(window['Low'].min())
    except Exception:
        return None, None

# ── Format ────────────────────────────────────────────────────
def fmt_price(sym, price):
    if 'JPY' in sym:                                  return f'{price:,.3f}'
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
            with open(STATE_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

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

        for cp in v.get('checkpoints', []):
            if cp['done']:
                continue
            # Bo qua checkpoint khong con trong CHECKPOINTS_H (don dep gia cu)
            if cp['hours'] not in CHECKPOINTS_H:
                cp['done'] = True
                continue

            # [FIX 1A] Het han: qua 6h sau checkpoint → bao cao va bo qua, khong retry mai mai
            expires_at = v.get('expires_at', cp['at'] + 6 * 3600)
            if now.timestamp() > expires_at:
                sym_e  = v['sym']
                sent_e = datetime.fromtimestamp(v['sent_at'], tz=timezone.utc).astimezone(VN_TZ)
                overdue_h = (now.timestamp() - cp['at']) / 3600
                print(f'  [+{cp["hours"]}h] {sym_e} het han ({overdue_h:.1f}h qua han), bo qua')
                send_telegram(
                    f'⏰ <b>Hết hạn xác nhận +{cp["hours"]}h</b>\n'
                    f'📍 {sym_e} {v["signal"]} @ {fmt_price(sym_e, v["entry_price"])}\n'
                    f'⏱ {sent_e.strftime("%d/%m %H:%M")} — không lấy được kết quả',
                    reply_to=v.get('message_id'),
                )
                cp['done'] = True
                continue

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
                continue
            h1_high, h1_low = fetch_price_range(yf_sym, v['sent_at'], hours=cp['hours'])

            entry   = v['entry_price']
            signal  = v['signal']
            sl_val  = v.get('sl')
            tp_val  = v.get('tp')

            # Kiem tra TP/SL tu HIGH/LOW trong khung gio (chinh xac hon gia hien tai)
            # Su dung h1_high/h1_low de biet TP hay SL da duoc cham trong khung gio
            tp_hit = sl_hit = False
            if sl_val and tp_val and h1_high is not None and h1_low is not None:
                # Trong khung gio: gia co dat toi TP hay SL khong?
                tp_hit = (h1_high >= tp_val) if signal == 'BUY' else (h1_low <= tp_val)
                sl_hit = (h1_low  <= sl_val) if signal == 'BUY' else (h1_high >= sl_val)
            elif sl_val and tp_val:
                # Fallback: dung gia hien tai
                tp_hit = (current >= tp_val) if signal == 'BUY' else (current <= tp_val)
                sl_hit = (current <= sl_val) if signal == 'BUY' else (current >= sl_val)

            # correct = TP hit (thang) > SL hit (thua) > direction (tham khao)
            # Day la metric chinh xac: phan anh P&L thuc te cua giao dich
            if tp_hit and not sl_hit:
                correct = True    # TP dat truoc SL → thang
            elif sl_hit and not tp_hit:
                correct = False   # SL bi cham truoc TP → thua
            elif tp_hit and sl_hit:
                # Ca hai trong khung: can xem cai nao den truoc — dung direction lam tiebreak
                diff_direction = current - entry if signal == 'BUY' else entry - current
                correct = diff_direction > 0
            else:
                # Khong cham ca hai: dung direction tai thoi diem check
                diff    = current - entry if signal == 'BUY' else entry - current
                correct = diff > 0

            diff = current - entry if signal == 'BUY' else entry - current
            pct  = abs(diff/entry) * 100

            if tp_hit and not sl_hit:
                verdict_emoji = '🎉'; verdict = 'CHỐT LỜI (TP)'
                move_text = f'TP chạm! +{abs(tp_val-entry)/entry*100:.3f}%'
            elif sl_hit and not tp_hit:
                verdict_emoji = '💸'; verdict = 'DỪNG LỖ (SL)'
                move_text = f'SL chạm! -{abs(sl_val-entry)/entry*100:.3f}%'
            elif diff > 0:
                verdict_emoji = '✅'; verdict = 'ĐÚNG HƯỚNG'
                move_text = f'{"Tăng" if signal=="BUY" else "Giảm"} {pct:.3f}%'
            else:
                verdict_emoji = '❌'; verdict = 'SAI HƯỚNG'
                move_text = f'{"Giảm" if signal=="BUY" else "Tăng"} {pct:.3f}%'

            inds    = v.get('indicators', {})
            regime  = v.get('regime', '?')
            H       = v.get('hurst', 0.5)
            aligned = v.get('aligned', '?')
            ind_str = (f"RSI{_icon(inds.get('rsi',0))} EMA{_icon(inds.get('ema',0))} "
                      f"MACD{_icon(inds.get('macd',0))} BB{_icon(inds.get('bb',0))} "
                      f"Mom{_icon(inds.get('mom',0))} IM{_icon(inds.get('inter',0))}")
            sent_dt    = datetime.fromtimestamp(v['sent_at'], tz=timezone.utc).astimezone(VN_TZ)
            now_vn_val = now.astimezone(VN_TZ)

            # TP/SL status line
            if sl_val and tp_val:
                if tp_hit and not sl_hit:
                    tp_sl_line = f'🎉 ĐÃ CHẠM TP ({fmt_price(sym, tp_val)}) — CHỐT LỜI!'
                elif sl_hit and not tp_hit:
                    tp_sl_line = f'💸 ĐÃ CHẠM SL ({fmt_price(sym, sl_val)}) — DỪNG LỖ!'
                else:
                    d_tp = abs(tp_val - current) / entry * 100
                    d_sl = abs(current - sl_val) / entry * 100
                    tp_sl_line = (f'TP {fmt_price(sym, tp_val)} (còn {d_tp:.3f}%) | '
                                  f'SL {fmt_price(sym, sl_val)} (còn {d_sl:.3f}%)')
            else:
                tp_sl_line = ''

            if h1_high is not None and h1_low is not None:
                range_line = (
                    f'📉 Đáy {cp["hours"]}h: <b>{fmt_price(sym, h1_low)}</b> | '
                    f'📈 Đỉnh {cp["hours"]}h: <b>{fmt_price(sym, h1_high)}</b>'
                )
            else:
                range_line = ''

            msg_lines = [
                f'{verdict_emoji} <b>Kết quả +{cp["hours"]}h — {verdict}</b>',
                '',
                f'📈 Cặp: <b>{sym}</b>',
                f'📌 {signal} @ {fmt_price(sym, entry)} → {fmt_price(sym, current)}',
                f'📊 Biến động: <b>{move_text}</b>',
            ]
            if range_line:
                msg_lines.append(range_line)
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
            # [FIX 1C] Neu reply_to that bai (tin nhan goc bi xoa / bot bi han), gui lai khong reply
            if not result.get('ok') and v.get('message_id'):
                result = send_telegram(msg)
            if result.get('ok'):
                cp['done'] = True
                print(f'{verdict} ✓')
                # Ghi ket qua theo doi win rate
                state.setdefault('results', []).append({
                    'sym': sym, 'signal': signal, 'correct': correct,
                    'date': sent_dt.strftime('%Y-%m-%d'), 'regime': v.get('regime', '?'),
                })
            else:
                print(f'Loi Telegram: {result}')
            time.sleep(1)

        # [FIX 1B] Tinh lai any_undone SAU khi xu ly — tranh giu signal da done them 1 vong
        any_undone = any(not cp['done'] for cp in v.get('checkpoints', []))
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

    # Buoc 0: load lich su gia tich luy + reset cache phien
    global _d1_cache
    _d1_cache = {}   # Reset D1 cache moi phien (du lieu fresh)
    print('=== Tai lich su gia ===')
    load_price_history()

    # Buoc 1: fetch intermarket + fundamental 1 lan cho ca phien
    print('=== Lay du lieu lien thi truong (DXY, Oil) ===')
    im = fetch_intermarket()
    print(f'  DXY trend: {im.get("dxy",0):+.3f} | Oil trend: {im.get("oil",0):+.3f}')

    print('\n=== Fundamental Intelligence Layer ===')
    fetch_fundamental(now)   # warm cache truoc vong lap, analyze() se dung cache nay

    # Buoc 2: xac nhan lenh cu (luon chay 24/7, khong phu thuoc session)
    print('\n=== Kiem tra xac nhan lenh cu ===')
    run_validations(state, now)

    # [SESSION FILTER] Per-pair: moi cap co trade_hours rieng trong PAIR_CONFIG
    # Chi quet cap nao co now.hour nam trong trade_hours cua cap do
    active_pairs = {
        sym for sym in SYMBOLS
        if now.hour in PAIR_CONFIG.get(sym, _DEFAULT_CONFIG).get('trade_hours', set(range(7, 21)))
    }
    if not active_pairs:
        print(f'\n=== Tat ca cap ngoai session ({now.hour}:xx UTC) — khong quet moi ===')
        save_state(state)
        return

    # Buoc 3: quet tin hieu moi
    session_label = (
        'Asian' if now.hour < 7 or now.hour >= 21 else
        'London' if now.hour < 12 else
        'NY' if now.hour < 17 else 'London+NY'
    )
    print(f'\n=== Forex Scan v4 — {now_vn.strftime("%Y-%m-%d %H:%M")} (Gio VN) | '
          f'{now.hour}:xx UTC ({session_label}) | {len(active_pairs)}/{len(SYMBOLS)} cap ===')

    for sym, yf_sym in SYMBOLS.items():
        cfg_check = PAIR_CONFIG.get(sym, _DEFAULT_CONFIG)
        if now.hour not in cfg_check.get('trade_hours', set(range(7, 21))):
            print(f'Phan tich {sym}... ngoai session, bo qua')
            continue

        print(f'Phan tich {sym}...', end=' ', flush=True)
        r = analyze(sym, yf_sym, now)

        if not r:
            print('NEUTRAL / loc ATR / khong du lieu')
            time.sleep(0.5)
            continue

        inds = r['indicators']
        print(
            f'{r["signal"]} {r["vote_count"]}/5 phieu | '
            f'conf={r["conf"]}% | '
            f'regime={r["regime"]}(H={r["hurst"]:.2f} ADX={r["adx"]:.0f}) | '
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

        # [LOC 5] Phat lat chieu: tin hieu nguoc chieu trong 12h qua → can 4/5 phieu
        # Tranh he thong chay theo noise khi thi truong choppy
        opp_key      = f'{sym}|{"SELL" if r["signal"] == "BUY" else "BUY"}'
        flip_elapsed = (now.timestamp() - state.get(opp_key, 0)) / 3600
        if flip_elapsed < 12 and r['vote_count'] < 4:
            print(f'  -> Lat chieu ({flip_elapsed:.1f}h truoc), can 4/5 phieu ({r["vote_count"]}/5), bo qua')
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

        # Win rate — chi tinh ket qua tu LOGIC_VERSION tro di (danh gia logic hien tai)
        results_all = state.get('results', [])
        wr_line = ''
        versioned_r = [x for x in results_all if x.get('date', '') >= LOGIC_VERSION]
        if len(versioned_r) >= 5:
            wr = sum(1 for x in versioned_r if x['correct']) / len(versioned_r) * 100
            wr_line = f'📈 Win rate ({len(versioned_r)} lệnh từ {LOGIC_VERSION}): {wr:.0f}%'

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

        # MTF summary line
        mtf = r.get('mtf', {})
        d1_icon = '✅' if mtf.get('d1_dir') == ('BULL' if r['signal']=='BUY' else 'BEAR') else \
                  ('⚠️' if mtf.get('d1_dir') == 'NEUTRAL' else '❌')
        h4_icon = '✅' if mtf.get('h4_dir') == ('BULL' if r['signal']=='BUY' else 'BEAR') else \
                  ('⚠️' if mtf.get('h4_dir') == 'NEUTRAL' else '❌')
        mtf_line = (f'📐 MTF: D1 {d1_icon}{mtf.get("d1_dir","?")}({mtf.get("d1_score",0):+.2f}) '
                    f'| H4 {h4_icon}{mtf.get("h4_dir","?")}({mtf.get("h4_score",0):+.2f}) '
                    f'| {mtf.get("h4_bars",0)} bars H4')
        if mtf.get('d1_level_used') and mtf.get('d1_level'):
            d1_lv = mtf['d1_level']
            mtf_line += f'\n🏁 TP nhắm D1 key level: {fmt_price(r["sym"], d1_lv)}'

        # S/R line
        sr = r.get('sr', {})
        sr_parts = []
        if sr.get('nearest_s'):
            s_dist = abs(r['price'] - sr['nearest_s']) / r['price'] * 100
            s_tag  = ' ⬅ GẦN!' if sr.get('near_support') else ''
            sr_parts.append(f'S {fmt_price(sym, sr["nearest_s"])} ({s_dist:.2f}%){s_tag}')
        if sr.get('nearest_r'):
            r_dist = abs(sr['nearest_r'] - r['price']) / r['price'] * 100
            r_tag  = ' ⬅ GẦN!' if sr.get('near_resistance') else ''
            sr_parts.append(f'R {fmt_price(sym, sr["nearest_r"])} ({r_dist:.2f}%){r_tag}')
        sr_line = ('🏗 H4 S/R: ' + ' | '.join(sr_parts)) if sr_parts else ''
        if sr.get('h4_sr_used') and sr.get('h4_sr_tp'):
            sr_line += f'\n🏁 TP nhắm H4 S/R: {fmt_price(sym, sr["h4_sr_tp"])}'

        # Fibonacci line
        fib_d = r.get('fib', {})
        fib_line = ''
        if fib_d.get('zone') and fib_d['zone'] != 'too_shallow':
            zone_lbl = {
                'golden':  '🟡 Golden Zone (38.2–61.8%)',
                'shallow': '🔵 Shallow (<38.2%)',
                'deep':    '🟠 Deep (61.8–78.6%)',
                'extreme': '🔴 Extreme (>78.6%) — thận trọng',
            }.get(fib_d['zone'], '')
            retrace_pct = fib_d.get('retrace_pct', 0)
            fib_line = (f'📐 Fib Retrace: {zone_lbl} | {retrace_pct*100:.1f}%\n'
                        f'   Key: R38={fmt_price(sym, fib_d["r382"])} '
                        f'R50={fmt_price(sym, fib_d["r500"])} '
                        f'R62={fmt_price(sym, fib_d["r618"])}\n'
                        f'   Ext: 127%={fmt_price(sym, fib_d["e1272"])} '
                        f'162%={fmt_price(sym, fib_d["e1618"])}')
            if fib_d.get('tp2_used'):
                fib_line += '  ← TP2'

        pair_macro_line = ''
        if r.get('pair_macro'):
            pm   = r['pair_macro']
            icon = '✅' if pm['score'] > 0 else '⚠️'
            parts = ' | '.join(f'{k.upper()} {v:+.2f}' for k, v in pm.items() if k != 'score')
            pair_macro_line = f'{icon} Macro: {parts}  →  {pm["score"]:+.2f}'

        # Fundamental section (Calendar + Sentiment + F&G)
        fund_lines = []
        fd = r.get('fundamental', {})
        if fd:
            fg    = fd.get('fear_greed', {})
            fg_v  = fg.get('value', 50.0)
            fg_lb = fg.get('label', 'Neutral')
            sent  = fd.get('sentiment', 0.0)
            sent_lbl = ('Bullish' if sent > 0.15 else 'Bearish' if sent < -0.15 else 'Neutral')
            sent_icon = '📈' if sent > 0.15 else ('📉' if sent < -0.15 else '➡️')
            fg_icon = '😱' if fg_v < 25 else ('😰' if fg_v < 45 else ('😐' if fg_v < 55 else ('😀' if fg_v < 75 else '🤑')))
            cal_line = ''
            if fd.get('cal_status') == 'SOFT':
                cal_line = f'  ⚡ Sự kiện: {fd["cal_reason"]}'
            fund_lines = [
                '🌐 Phân tích cơ bản:',
                f'  {sent_icon} Sentiment: {sent:+.2f} ({sent_lbl})',
                f'  {fg_icon} Fear & Greed: {fg_v:.0f}/100 ({fg_lb})',
            ]
            if cal_line:
                fund_lines.append(cal_line)

        msg_parts = [
            f'{emoji} <b>{sym} — {direction}</b> | {conf}% tin cậy',
            f'<code>{bar}</code>  {conf_10}/10',
            '',
            f'🗳 {vote_bar}',
            f'📊 H1: {r["phase"]} | {r["regime"]} (H={r["hurst"]:.2f} | ADX={r["adx"]:.0f} | {r.get("history_bars", 0)} bars)',
            mtf_line,
            *(([sr_line, '']) if sr_line else ['']),
            *(([fib_line, '']) if fib_line else []),
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
        if pair_macro_line:
            msg_parts.append(pair_macro_line)
            msg_parts.append('')
        if fund_lines:
            msg_parts.extend(fund_lines)
            msg_parts.append('')
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
                'expires_at':  now.timestamp() + (max(CHECKPOINTS_H) + 6) * 3600,
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
    print('\n=== Luu lich su gia ===')
    save_price_history()
    print(f'\n=== Hoan thanh. Da gui {sent} tin hieu moi ===')

if __name__ == '__main__':
    main()
