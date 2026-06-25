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
import json, os, time, logging
from pathlib import Path
import numpy as np
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf
import pandas as pd

# ── Cau hinh ──────────────────────────────────────────────────
# parent = cloud_notifier/ locally va repo root trong GitHub Actions
_ROOT            = Path(__file__).parent

# Decision log
_LOG_FILE = str(_ROOT / 'data' / 'decisions.log')
(_ROOT / 'data').mkdir(exist_ok=True)
# Trim log truoc khi mo handler — file duoc commit moi run nen phai chan tang truong
# ~96 runs/ngay × ~10 dong = ~1000 dong/ngay → 30000 dong ≈ 1 thang audit
_MAX_LOG_LINES = 30000
try:
    if os.path.exists(_LOG_FILE):
        with open(_LOG_FILE, encoding='utf-8', errors='replace') as _f:
            _lines = _f.readlines()
        if len(_lines) > _MAX_LOG_LINES:
            with open(_LOG_FILE, 'w', encoding='utf-8') as _f:
                _f.writelines(_lines[-_MAX_LOG_LINES:])
except Exception:
    pass
logging.basicConfig(
    filename=_LOG_FILE, level=logging.INFO,
    format='%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S UTC',
)
_log = logging.getLogger('forex')

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT    = os.environ.get('TELEGRAM_CHAT',  '')
TWELVE_DATA_KEY  = os.environ.get('TWELVE_DATA_KEY', '')  # Twelve Data API (free: 800 req/ngay)
COOLDOWN_HOURS  = 6
LOT_SIZE        = 0.01   # Lot size thuc te moi lenh — chinh khi scale von
ACCOUNT_SIZE    = float(os.environ.get('ACCOUNT_SIZE', '1000'))  # Von tai khoan (USD) — dung tinh lot de xuat
STATE_FILE      = str(_ROOT / 'last_signals.json')
MONITOR_HOURS   = 48     # Theo doi lien tuc moi 15p cho den khi cham TP/SL; qua nguong nay -> dong theo doi (khop gioi han data 2 ngay)
MIN_CONFIDENCE  = 65     # 3/5 phieu = 60 (truoc bonus) | can bonus H4/Fib/SR de dat 65
# VOTING_MODE: 'info' (mac dinh) = he Phan tich CHUA co edge kiem chung (mọi cap CI om 0)
#   -> chi gui tin THAM KHAO co banner canh bao + KHONG nut "Da vao lenh", KHONG moi vao lenh;
#   van ghi pending_validations de tiep tuc thu data WR. 'live' = bat lai tin hieu actionable.
#   Doi qua GitHub Repo Variable vars.VOTING_MODE (giong vars.BIAS). Quyet dinh 25/06: PA la he
#   duy nhat qua moi cong kiem chung -> voting chay info-only cho den khi co edge.
VOTING_MODE     = os.environ.get('VOTING_MODE', 'info').strip().lower()
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
#   12/06/2026: chi con la FALLBACK cold-start — nguong thuc te la dong
#   (dynamic_hurst_block: percentile 30 lich su 14 ngay, kep [0.35, 0.45])
# min_votes: so phieu toi thieu (3 = chuan | 4 = yeu cau cao hon cho cap nhieu nhieu)
PAIR_CONFIG = {
    # === MAJORS ===
    # EUR/USD: 83% WR — cap tot nhat, giu nguong hien tai
    'EUR/USD':   {'rsi_buy': 45, 'rsi_sell': 55, 'hurst_block': 0.47, 'min_votes': 3,
                  'trade_hours': set(range(7, 21))},
    # USD/JPY: yield differential + risk sentiment; strict RSI tranh false signal
    'USD/JPY':   {'rsi_buy': 38, 'rsi_sell': 62, 'hurst_block': 0.48, 'min_votes': 3,
                  'trade_hours': set(range(0, 21))},

    # === KIM LOAI QUY — TRONG TAM CHINH ===
    # XAU/USD: PRIMARY PAIR — macro 5-factor (DXY/TIPS/TNX/VIX/Oil)
    # hurst_block 0.39: vang thuong co H thap hon forex nhung van trend tot
    # cooldown_hours 4: gold co session-based volatility cao, 6h bỏ lỡ co hoi
    'XAU/USD':   {'rsi_buy': 40, 'rsi_sell': 60, 'hurst_block': 0.39, 'min_votes': 3,
                  'trade_hours': set(range(6, 21)), 'cooldown_hours': 4},

    # === PAIRS DA LOAI ===
    # USD/CHF: loai 09/06/2026 — correlation ~0.70 voi USD/JPY (ca hai safe haven USD/JPY du dien)
    # USD/CAD: loai 09/06/2026 — proxy oil, thay bang WTI truc tiep (Phase 2)
    # WTI/USD: loai 25/06/2026 — backtest OOS exp -0.53R negative_significant (prob_le_0=1.0),
    #          random-entry p=0.12 = zero skill, spread 8 pips cao nhat. Them lai neu dung duoc edge rieng.
    # EUR/JPY: loai 09/06/2026 — synthetic EUR/USD × USD/JPY, khong doc lap
    # NZD/USD: loai 09/06/2026 — correlation cao voi risk-on bucket, sample size chua du
    # XAG/USD: loai (0% WR, 7 lenh thua — 2026-06-03); industrial demand kho model
    # GBP/USD: loai (27% WR); Brexit/BoE surprise khong the model hoa
    # AUD/USD: loai (0% WR); China manufacturing demand khong co trong model
    # GBP/JPY: loai (33% WR); double uncertainty, qua nhieu noise
    # AUD/JPY: loai; carry trade phu thuoc China demand khong co data
    # CAD/JPY: loai; complex carry, sample size qua nho
    # USOIL/UKOIL: loai (0%/33% WR); OPEC+ surprise-driven, kho du bao
}
# EUR/GBP da bi loai: 17% WR (1/6), pair phu thuoc chinh sach Brexit/UK-EU

_DEFAULT_CONFIG = {'rsi_buy': 45, 'rsi_sell': 55, 'hurst_block': 0.47, 'min_votes': 3,
                   'trade_hours': set(range(7, 21))}

# USD direction map: True = giao dich nay la "short USD" (USD yeu thi co loi)
# EUR/USD BUY = ban USD; USD/JPY BUY = mua USD; WTI+XAU BUY = USD-denominated → nghich chieu
_USD_SHORT = {
    'EUR/USD': {'BUY': True,  'SELL': False},
    'USD/JPY': {'BUY': False, 'SELL': True},
    'WTI/USD': {'BUY': True,  'SELL': False},
    'XAU/USD': {'BUY': True,  'SELL': False},
}

SYMBOLS = {
    # Majors
    'EUR/USD': 'EURUSD=X', 'USD/JPY': 'USDJPY=X',
    # WTI/USD: LOAI 25/06/2026 — backtest OOS exp -0.53R negative_significant, zero skill (random p=0.12)
    # Kim loai quy — TRONG TAM CHINH
    'XAU/USD': 'GC=F',
}

# Vung gia hop le tung symbol — loc du lieu sai tu yfinance/TwelveData
# Dat rong de chi loai gia co ban ro rang bi loi (0, nan, data nham contract)
PRICE_SANITY = {
    'EUR/USD':   (0.70, 1.80),
    'USD/JPY':   (70,   220),
    'WTI/USD':   (30,   250),
    'XAU/USD':   (1200, 8000),
}

_im_cache          = {}   # Cache intermarket data (chi fetch 1 lan moi phien)
_gold_cache        = {}   # Cache macro data rieng cho XAU/USD (TNX, VIX)
_fundamental_cache = {}   # Cache Fundamental Intelligence Layer (Calendar/Sentiment/F&G)
_price_history     = {}   # Lich su gia tich luy — load tu file, save cuoi phien
_d1_cache          = {}   # D1 OHLCV cache per pair per session (180 ngay daily)
_w1_cache          = {}   # W1 Weekly cache per pair per session (52 tuan)

PRICE_HISTORY_FILE = str(_ROOT / 'price_history.json')
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
    'XAU': ['gold', 'bullion', 'precious metal', 'safe haven',
            'central bank gold', 'gold etf', 'gld', 'real yield',
            'stagflation', 'inflation hedge', 'brics gold'],
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
_RISK_ON_BUYS  = {'EUR/USD', 'WTI/USD'}
_RISK_OFF_BUYS = {'USD/JPY', 'XAU/USD'}

# Symbol mapping cho Twelve Data API (3 cap × 96 lan/ngay = 288 req — trong quota free 800)
TWELVE_DATA_SYMBOLS = {
    'EUR/USD': 'EUR/USD', 'USD/JPY': 'USD/JPY',
    'XAU/USD': 'XAU/USD',
}

# Reverse mapping: yfinance symbol -> Twelve Data symbol (dung fallback khi yfinance fail)
_YF_TO_TD_SYM = {yf: TWELVE_DATA_SYMBOLS[k] for k, yf in SYMBOLS.items() if k in TWELVE_DATA_SYMBOLS}

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

# ── Nguong Hurst dong (12/06/2026) ───────────────────────────
# Thay hang so tay bang percentile lich su cua CHINH cap do: hang so 0.47
# (dat 01/06 de giam false signal) bi loi thoi sau khi them cac lop bao ve
# RANGE+1 phieu / ADX kep / counter_trend / counter_w1 — thanh 2 lop chan
# trung 1 rui ro. Bang chung decisions.log 08-12/06: EUR bi chan 60 lan
# o H=0.435-0.467 (sat nut nguong 0.47), 0 signal EUR tu 03/06.
HURST_HIST_DAYS  = 14    # cua so lich su H
HURST_HIST_MIN_N = 48    # ~2 ngay mau hourly truoc khi nguong dong co hieu luc
HURST_DYN_PCT    = 30    # block 30% trang thai H thap nhat cua chinh cap do
HURST_DYN_FLOOR  = 0.35  # duoi muc nay = mean-revert sau, khong trade trend du percentile noi gi
HURST_DYN_CAP    = 0.45  # tren muc nay = chan ca NEUTRAL regime (dung loi cua nguong 0.47 cu)

def dynamic_hurst_block(state, sym, H, now_ts, static_block):
    """Nguong block = percentile 30 cua H 14 ngay gan nhat cua cap, kep
    [0.35, 0.45]. Moi cap tu thich nghi voi 'tinh cach' rieng (USD/JPY
    song o H~0.32, EUR/USD o ~0.46) thay vi dung chung thuoc do tuyet doi.
    Mau luu toi da 1 lan/gio (H tren 200 nen doi cham, mau 15p chi phinh state).
    Chua du mau → fallback nguong tinh trong PAIR_CONFIG."""
    if state is None:
        return static_block
    hist = state.setdefault('hurst_hist', {}).setdefault(sym, [])
    if not hist or now_ts - hist[-1][0] >= 3300:
        hist.append([int(now_ts), round(H, 3)])
    cutoff = now_ts - HURST_HIST_DAYS * 86400
    hist = [x for x in hist if x[0] >= cutoff]
    state['hurst_hist'][sym] = hist
    if len(hist) < HURST_HIST_MIN_N:
        return static_block
    vals = sorted(x[1] for x in hist)
    pct  = vals[min(int(len(vals) * HURST_DYN_PCT / 100), len(vals) - 1)]
    return min(max(pct, HURST_DYN_FLOOR), HURST_DYN_CAP)

# ── Exhaustion Guard (12/06/2026) ────────────────────────────
# Nghich ly lagging system: xac nhan dat cuc dai o CUOI trend → he cang
# "chac chan" thi entry cang gan diem dao chieu. Vu XAU 11/06: SELL conf=81%
# phat ra khi D1 RSI=28, move -5.9%/5 phien, ngay truoc V-reversal +4%.
# Nguyen tac: KHONG chan theo huong (van cho trend tiep dien) — chan theo
# VI TRI: trong vung kiet suc, lenh thuan-move chi duoc phep khi gia dang
# pha day/dinh moi (trend tu chung minh con song), cam ban bounce/mua dip.
EXH_RSI_LO       = 28    # D1 RSI duoi muc nay = ban da kiet (cho SELL)
EXH_RSI_HI       = 72    # D1 RSI tren muc nay = mua da kiet (cho BUY)
EXH_PCTL_HARD    = 85    # |move 5 phien| vuot percentile nay cua lich su → vung kiet
EXH_PCTL_SOFT    = 70    # vung canh bao: chi tru confidence, khong chan
EXH_LOOKBACK_D   = 5     # cua so do move
EXH_EXTREME_ATR  = 0.3   # gia trong 0.3 ATR-D1 cua day/dinh 5 ngay = "dang pha extreme"

def exhaustion_state(bars):
    """Do vi tri trong trend tu D1 (resample tu H1 bars cua price_history).
    Tra ve None neu < 18 ngay du lieu. Percentile cua move la adaptive theo
    chinh lich su cap do — khong dung nguong % cung.
    Returns: {'dir': 'DOWN'|'UP'|None, 'soft': bool, 'd1_rsi', 'pctl',
              'at_extreme': bool (gia dang ep day/dinh 5 ngay)}"""
    days = {}
    for b in bars:
        d = int(b['t'] // 86400)
        rec = days.get(d)
        if rec is None:
            days[d] = {'c': b['c'], 'h': b['h'], 'l': b['l']}
        else:
            rec['c'] = b['c']
            rec['h'] = max(rec['h'], b['h'])
            rec['l'] = min(rec['l'], b['l'])
    keys = sorted(days)
    cl = [days[k]['c'] for k in keys]
    hi = [days[k]['h'] for k in keys]
    lo = [days[k]['l'] for k in keys]
    if len(cl) < 18:
        return None
    d1_rsi = rsi(cl)
    moves  = [abs(cl[i] - cl[i - EXH_LOOKBACK_D]) / cl[i - EXH_LOOKBACK_D]
              for i in range(EXH_LOOKBACK_D, len(cl))]
    cur    = (cl[-1] - cl[-1 - EXH_LOOKBACK_D]) / cl[-1 - EXH_LOOKBACK_D]
    pctl   = sum(1 for m in moves if m <= abs(cur)) / len(moves) * 100
    atr_d1 = sum(h - l for h, l in zip(hi[-14:], lo[-14:])) / 14
    price  = cl[-1]
    exh_dir = None
    soft    = False
    at_extreme = False
    if cur < 0 and d1_rsi < EXH_RSI_LO and pctl >= EXH_PCTL_HARD:
        exh_dir    = 'DOWN'
        at_extreme = (price - min(lo[-EXH_LOOKBACK_D:])) <= EXH_EXTREME_ATR * atr_d1
    elif cur > 0 and d1_rsi > EXH_RSI_HI and pctl >= EXH_PCTL_HARD:
        exh_dir    = 'UP'
        at_extreme = (max(hi[-EXH_LOOKBACK_D:]) - price) <= EXH_EXTREME_ATR * atr_d1
    elif pctl >= EXH_PCTL_SOFT and (d1_rsi < 35 or d1_rsi > 65):
        soft = True
    return {'dir': exh_dir, 'soft': soft, 'd1_rsi': round(d1_rsi, 1),
            'pctl': round(pctl, 0), 'at_extreme': at_extreme,
            'move_dir': 'DOWN' if cur < 0 else 'UP'}

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
    Du lieu: DXY (suc manh USD).
    - DXY tang → USD manh → USD/* tang, */USD giam, Vang giam
    """
    im  = fetch_intermarket()
    dxy = im.get('dxy', 0.0)

    if sym == 'XAU/USD':
        return -dxy
    if sym.startswith('USD/'):
        return dxy
    if sym.endswith('/USD'):
        return -dxy
    return 0.0

# ── Phuong trinh macro XAU/USD ───────────────────────────────
def fetch_gold_macro():
    """
    Lay 5 nhan to macro anh huong den XAU/USD, cache 1 lan moi phien.
      DXY   (UUP)   — da co trong _im_cache, tai su dung
      TIPS  (^DFII10)— US 10Y Real Yield = nominal - inflation expectations
                        Driver MANH NHAT cua vang: real yield am → vang tang
      10Y   (^TNX)  — US Nominal Treasury Yield, nghich chieu Vang
      VIX   (^VIX)  — Chi so so hai, cung chieu Vang (safe haven)
      Oil   (CL=F)  — da co trong _im_cache, cung chieu nhe (lam phat)
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
        'tips_s':  0.0,
    }
    # TIPS 10Y Real Yield (^DFII10) — driver manh nhat cua vang
    # Real yield tang → co hoi tai chinh thuc su → Vang giam (no dividend/coupon)
    # Real yield < 0 → negative carry → Vang rat bullish
    try:
        tips = yf.Ticker('^DFII10').history(period='7d', interval='1d')['Close'].dropna()
        if len(tips) >= 2:
            tips_now   = float(tips.iloc[-1])
            tips_delta = float(tips.iloc[-1] - tips.iloc[max(0, len(tips)-3)])
            # Level penalty: real yield duong = co hoi tai chinh ton tai → bearish gold
            level_pen  = -0.20 if tips_now > 0.5 else (0.25 if tips_now < 0 else 0.0)
            # Delta: real yield tang → score am (bearish gold); giam → score duong
            _gold_cache['tips_s'] = float(np.clip(-tips_delta * 4.0 + level_pen, -1.0, 1.0))
    except Exception:
        pass
    # US 10-Year Nominal Yield — nghich chieu: yield tang → Vang giam
    try:
        tny = yf.Ticker('^TNX').history(period='7d', interval='1d')['Close'].dropna()
        if len(tny) >= 3:
            delta = float(tny.iloc[-1] - tny.iloc[-3])   # thay doi trong 3 phien (% point)
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
    Phuong trinh lien thi truong danh rieng XAU/USD — 5 factors.

    score = 0.30*DXY + 0.25*TIPS + 0.20*TNX + 0.15*VIX + 0.10*Oil

    Nguyen tac:
      DXY  tang → USD manh  → Vang GIAM  (nghich, w=0.30)
      TIPS tang → real yield tang → Vang GIAM  (nghich, w=0.25 — driver manh nhat)
      TNX  tang → nominal yield → Vang GIAM  (nghich, w=0.20)
      VIX  tang → so hai    → Vang TANG  (thuan,  w=0.15 — safe haven)
      Oil  tang → lam phat  → Vang TANG  (thuan nhe, w=0.10)

    Tra ve: (score[-1,+1], components_dict)
      +1.0 = macro ung ho manh MUA Vang
      -1.0 = macro ung ho manh BAN Vang
    """
    g     = fetch_gold_macro()
    dxy_s  = -g['dxy_raw']   # DXY tang → bearish gold
    tips_s =  g['tips_s']    # da tinh dau trong fetch: real yield giam → duong
    tny_s  =  g['tny_s']     # nominal yield giam → duong
    vix_s  =  g['vix_s']     # VIX tang → duong (safe haven)
    oil_s  =  g['oil_raw']   # Oil tang → bullish gold (lam phat)
    score  = 0.30*dxy_s + 0.25*tips_s + 0.20*tny_s + 0.15*vix_s + 0.10*oil_s
    comps  = {
        'dxy':  round(dxy_s,  2),
        'tips': round(tips_s, 2),
        'tny':  round(tny_s,  2),
        'vix':  round(vix_s,  2),
        'oil':  round(oil_s,  2),
    }
    return float(np.clip(score, -1.0, 1.0)), comps


# ── Phương trình macro JPY pairs ──────────────────────────────
def jpy_macro_score(sym):
    """
    Macro equation cho JPY pairs — tai su dung _gold_cache (zero API cost).
    score > 0: JPY suy yeu → BUY pair | score < 0: JPY manh → SELL pair

    USD/JPY: yield differential (BoJ ~0% → TNX la proxy spread) + risk + DXY

    Sign convention:
      raw_yield = -tny_s : duong khi US yield TANG (carry trade vao USD → JPY yeu)
      risk_on   = -vix_s : duong khi VIX GIAM (risk-on → JPY yeu)
      dxy_raw   : duong khi DXY tang (USD manh)
    """
    g         = fetch_gold_macro()
    raw_yield = -g['tny_s']
    risk_on   = -g['vix_s']
    dxy_raw   =  g['dxy_raw']
    oil_raw   =  g['oil_raw']

    if sym == 'USD/JPY':
        score = 0.50 * raw_yield + 0.30 * risk_on + 0.20 * dxy_raw
        comps = {'yield': round(raw_yield, 2), 'risk': round(risk_on, 2), 'dxy': round(dxy_raw, 2)}
    else:
        return 0.0, {}
    return float(np.clip(score, -1.0, 1.0)), comps


def macro_score(sym):
    """
    Router macro thong nhat — 3 cap (EUR/USD, USD/JPY, XAU/USD).
    100% tai su dung _gold_cache (DXY, TIPS, TNX, VIX, Oil) — zero API cost them.

    Tra ve (score[-1,+1], comps) hoac (None, {}) neu khong co macro.
    score > 0: macro ung ho BUY  |  score < 0: macro ung ho SELL
    """
    if sym == 'XAU/USD':       return gold_macro_score()
    if sym.endswith('/JPY'):   return jpy_macro_score(sym)

    g       = fetch_gold_macro()
    dxy_inv = -g['dxy_raw']
    risk_on = -g['vix_s']

    if sym == 'EUR/USD':
        s = 0.60*dxy_inv + 0.40*risk_on
        c = {'dxy': round(dxy_inv, 2), 'risk': round(risk_on, 2)}

    elif sym == 'WTI/USD':
        # Oil: DXY inverse (USD-denominated) + risk sentiment (demand) + yield (growth proxy)
        # tny_s > 0 khi yields giam → USD yeu + expansion expectations → bullish oil
        tny_s = g['tny_s']
        s = 0.40*dxy_inv + 0.40*risk_on + 0.20*tny_s
        c = {'dxy': round(dxy_inv, 2), 'risk': round(risk_on, 2), 'yield': round(tny_s, 2)}

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
    Tim muc khang cu / ho tro D1 gan nhat lam TP thuc te.

    [FIX #3] Dung pivot point (swing high/low) thay vi min/max toan bo nen.
    Pivot high: nen cao hon ca lb nen trai lan lb nen phai (dinh cuc bo xac nhan)
    Pivot low:  nen thap hon ca lb nen trai lan lb nen phai (day cuc bo xac nhan)

    BUY: tim pivot high gan nhat phia TREN gia (it nhat 0.3% tren)
    SELL: tim pivot low gan nhat phia DUOI gia (it nhat 0.3% duoi)
    Returns: float hoac None.
    """
    d1 = fetch_d1_data(sym, yf_sym)
    if d1 is None:
        return None

    highs  = d1['highs']
    lows   = d1['lows']
    lb     = 3   # 3 nen xac nhan moi phia
    n      = len(highs)

    if signal == 'BUY':
        pivot_highs = [
            highs[i]
            for i in range(lb, n - lb)
            if all(highs[i] >= highs[j] for j in range(i - lb, i + lb + 1) if j != i)
        ]
        candidates = [h for h in pivot_highs if h > price * 1.003]
        return min(candidates) if candidates else None
    else:
        pivot_lows = [
            lows[i]
            for i in range(lb, n - lb)
            if all(lows[i] <= lows[j] for j in range(i - lb, i + lb + 1) if j != i)
        ]
        candidates = [l for l in pivot_lows if l < price * 0.997]
        return max(candidates) if candidates else None


def w1_trend(sym, yf_sym):
    """
    Xu huong Weekly (W1) — buc tranh lon nhat, loc counter-trend trong xu huong tuan.
    Fetch 52 tuan tu yfinance, cache per session.

    3 thanh phan:
      ema_s    (50%): gia vs EMA20 tuan (~5 thang) — xu huong chien luoc
      mom_s    (30%): 4 tuan gan nhat tang/giam — dong luc ngan han tuan
      struct_s (20%): HH/HL vs LH/LL — cau truc gia tuan

    Returns: ('BULL'|'BEAR'|'NEUTRAL', score, details)
    """
    global _w1_cache
    if sym in _w1_cache:
        return _w1_cache[sym]
    neutral = ('NEUTRAL', 0.0, {})
    try:
        df = yf.Ticker(yf_sym).history(period='1y', interval='1wk')
        if df is None or len(df) < 10:
            _w1_cache[sym] = neutral; return neutral
        closes = list(df['Close'].dropna())
        highs  = list(df['High'].dropna())
        lows   = list(df['Low'].dropna())
        n      = min(len(closes), len(highs), len(lows))
        closes = closes[:n]; highs = highs[:n]; lows = lows[:n]
        price  = closes[-1]

        # EMA 20 weekly (~5 thang trend)
        e20   = ema(closes, 20)
        ema_s = 1 if price > e20 * 1.001 else (-1 if price < e20 * 0.999 else 0)

        # Momentum: 4 tuan gan nhat
        w = closes[-5:] if len(closes) >= 5 else closes
        up_w  = sum(1 for i in range(1, len(w)) if w[i] > w[i-1])
        tot_w = len(w) - 1
        mom_s = (1 if up_w >= round(tot_w*0.75) else
                 -1 if up_w <= round(tot_w*0.25) else 0)

        # Market structure: HH/HL vs LH/LL (10 tuan)
        n2 = min(n, 10); mid = n2 // 2; struct_s = 0
        if n2 > mid > 0:
            ph = max(highs[-n2:-mid]); ch = max(highs[-mid:])
            pl = min(lows[-n2:-mid]);  cl = min(lows[-mid:])
            struct_s = 1 if (ch > ph and cl > pl) else (-1 if (ch < ph and cl < pl) else 0)

        score     = ema_s*0.50 + mom_s*0.30 + struct_s*0.20
        direction = 'BULL' if score > 0.20 else ('BEAR' if score < -0.20 else 'NEUTRAL')
        details   = {'ema': ema_s, 'mom': mom_s, 'struct': struct_s, 'score': round(score, 2)}
        _w1_cache[sym] = (direction, float(score), details)
        return _w1_cache[sym]
    except Exception as e:
        print(f'  [W1] fetch loi {sym}: {e}')
        _w1_cache[sym] = neutral; return neutral


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


# ── Book-Enhanced Modules (Technical Analysis for Mega Profit) ────────────────
# Bab 5 Trend + Bab 7 S/R + Bab 8 Angka Psikologi + Bab 17 Dow Theory

def _count_tl_touches(side_prices, idx1, p1, slope, tol=0.003):
    """Dem so lan gia cham gan trendline tu idx1 den hien tai."""
    touches = 2
    for i in range(idx1 + 1, len(side_prices)):
        line_val = p1 + slope * (i - idx1)
        if line_val <= 0:
            continue
        if abs(side_prices[i] - line_val) / line_val < tol:
            touches += 1
    return touches


def find_trendlines(closes, highs, lows, lookback=80, lb=5):
    """
    Phat hien Trendline tu swing points (sach: Technical Analysis for Mega Profit Bab 5).
    - Uptrend  : noi 2 swing low gan nhat co Higher Low (doc len)
    - Downtrend: noi 2 swing high gan nhat co Lower High (doc xuong)
    Valid break (Bab 9 Batas Toleransi): gia dong cua vuot 1.5% moi la break hop le.
    Touch signal (Bab 5): cham +/-0.5% = tin hieu entry.

    Returns dict:
      up  : {'value', 'valid', 'slope', 'touches'} | None
      dn  : {'value', 'valid', 'slope', 'touches'} | None
      tl_vote : +1 (buy signal) / -1 (sell signal) / 0 (neutral)
      tl_label: mo ta tin hieu
    """
    n = min(len(closes), len(highs), len(lows), lookback)
    if n < 20:
        return {'up': None, 'dn': None, 'tl_vote': 0, 'tl_label': 'NO_TL'}

    c = closes[-n:]
    h = highs[-n:]
    l = lows[-n:]
    last_idx = len(c) - 1
    price = c[-1]

    # Tim swing lows (lb bars moi phia)
    swing_lows = []
    for i in range(lb, last_idx - lb + 1):
        if (all(l[i] <= l[j] for j in range(i - lb, i)) and
                all(l[i] <= l[j] for j in range(i + 1, i + lb + 1))):
            swing_lows.append((i, l[i]))

    # Tim swing highs
    swing_highs = []
    for i in range(lb, last_idx - lb + 1):
        if (all(h[i] >= h[j] for j in range(i - lb, i)) and
                all(h[i] >= h[j] for j in range(i + 1, i + lb + 1))):
            swing_highs.append((i, h[i]))

    up_tl = dn_tl = None

    # Uptrend trendline: 2 swing low gan nhat co slope duong
    for i in range(len(swing_lows) - 1, 0, -1):
        idx2, p2 = swing_lows[i]
        idx1, p1 = swing_lows[i - 1]
        if p2 > p1 and idx2 > idx1:
            slope = (p2 - p1) / (idx2 - idx1)
            value = p2 + slope * (last_idx - idx2)
            if value <= 0:
                continue
            valid = c[-1] > value * 0.997
            touches = _count_tl_touches(l, idx1, p1, slope)
            up_tl = {'value': value, 'valid': valid, 'slope': slope, 'touches': touches}
            break

    # Downtrend trendline: 2 swing high gan nhat co slope am
    for i in range(len(swing_highs) - 1, 0, -1):
        idx2, p2 = swing_highs[i]
        idx1, p1 = swing_highs[i - 1]
        if p2 < p1 and idx2 > idx1:
            slope = (p2 - p1) / (idx2 - idx1)
            value = p2 + slope * (last_idx - idx2)
            if value <= 0:
                continue
            valid = c[-1] < value * 1.003
            touches = _count_tl_touches(h, idx1, p1, slope)
            dn_tl = {'value': value, 'valid': valid, 'slope': slope, 'touches': touches}
            break

    tol_touch = 0.005   # 0.5%: dang cham trendline
    tol_break = 0.015   # 1.5%: batas toleransi — valid break

    tl_vote = 0
    tl_label = 'NO_TL'

    if up_tl and up_tl['valid']:
        dist = (price - up_tl['value']) / up_tl['value']
        if dist < -tol_break:
            tl_vote = -1
            tl_label = f'UP_TL_BREAK'
        elif abs(dist) <= tol_touch:
            tl_vote = 1
            tl_label = f'UP_TL_TOUCH(x{up_tl["touches"]})'

    if dn_tl and dn_tl['valid'] and tl_vote == 0:
        dist = (price - dn_tl['value']) / dn_tl['value']
        if dist > tol_break:
            tl_vote = 1
            tl_label = f'DN_TL_BREAK'
        elif abs(dist) <= tol_touch:
            tl_vote = -1
            tl_label = f'DN_TL_TOUCH(x{dn_tl["touches"]})'

    return {'up': up_tl, 'dn': dn_tl, 'tl_vote': tl_vote, 'tl_label': tl_label}


def dow_structure(closes, highs, lows, lookback=60, lb=5):
    """
    Phan tich cau truc Dow Theory: HH+HL = uptrend, LH+LL = downtrend (sach Bab 17).
    Dow Theory mat 20-25% truoc khi xac nhan — day la chi phi cua su chac chan.

    Returns: {'structure': str, 'score': float}
      UPTREND (+1.0), DOWNTREND (-1.0),
      REVERSAL_UP_RISK (+0.2), REVERSAL_DN_RISK (-0.2), SIDEWAYS (0.0)
    """
    n = min(len(closes), len(highs), len(lows), lookback)
    if n < 20:
        return {'structure': 'INSUFFICIENT', 'score': 0.0}

    h = highs[-n:]
    l = lows[-n:]
    last_idx = len(h) - 1

    s_highs = [(i, h[i]) for i in range(lb, last_idx - lb + 1)
               if all(h[i] >= h[j] for j in range(i - lb, i)) and
               all(h[i] >= h[j] for j in range(i + 1, i + lb + 1))]
    s_lows  = [(i, l[i]) for i in range(lb, last_idx - lb + 1)
               if all(l[i] <= l[j] for j in range(i - lb, i)) and
               all(l[i] <= l[j] for j in range(i + 1, i + lb + 1))]

    if len(s_highs) < 2 or len(s_lows) < 2:
        return {'structure': 'INSUFFICIENT', 'score': 0.0}

    hh1, hh2 = s_highs[-2][1], s_highs[-1][1]
    ll1, ll2 = s_lows[-2][1],  s_lows[-1][1]

    higher_high = hh2 > hh1
    higher_low  = ll2 > ll1
    lower_high  = hh2 < hh1
    lower_low   = ll2 < ll1

    if higher_high and higher_low:
        return {'structure': 'UPTREND',            'score':  1.0}
    if lower_high and lower_low:
        return {'structure': 'DOWNTREND',           'score': -1.0}
    if higher_high and lower_low:
        return {'structure': 'REVERSAL_UP_RISK',   'score':  0.2}
    if lower_high and higher_low:
        return {'structure': 'REVERSAL_DN_RISK',   'score': -0.2}
    return {'structure': 'SIDEWAYS', 'score': 0.0}


def psychological_levels(price, n_near=4):
    """
    Tim cac muc gia tam ly (so tron) gan gia hien tai (sach Bab 8 Angka Psikologi).
    Sach: dat lenh mua NGAY TREN, lenh ban NGAY DUOI muc tam ly.

    Returns: list of {'price', 'type': 'R'|'S', 'strength': 2-5, 'is_psych': True}
    """
    if price >= 1000:
        steps = [50, 100, 500]
    elif price >= 100:
        steps = [10, 25, 50]
    elif price >= 10:
        steps = [1, 5, 10]
    elif price >= 1:
        steps = [0.25, 0.5, 1.0]
    elif price >= 0.1:
        steps = [0.005, 0.01, 0.05]
    else:
        steps = [0.0005, 0.001, 0.005]

    seen = set()
    result = []
    for step in steps:
        base = round(price / step) * step
        for i in range(-n_near, n_near + 1):
            lvl = round(base + i * step, 8)
            if lvl <= 0 or lvl in seen:
                continue
            seen.add(lvl)
            dist = abs(lvl - price) / price
            if dist > 0.06:
                continue
            # Strength: so tron hon → manh hon (1000 > 500 > 100 > ...)
            if step >= 100 and round(lvl % (step * 10), 6) < 1e-6:
                strength = 5
            elif round(lvl % (step * 5), 6) < 1e-6:
                strength = 4
            elif round(lvl % (step * 2), 6) < 1e-6:
                strength = 3
            else:
                strength = 2
            result.append({
                'price':    lvl,
                'type':     'R' if lvl > price else 'S',
                'strength': strength,
                'is_psych': True,
            })

    result.sort(key=lambda x: abs(x['price'] - price))
    return result[:8]


# ── Chart Pattern detectors (H4) — dung boi gold_pa_bot.py ──
# Nghien cuu Bulkowski (thepatternsite.com): failure rate thap nhat thuoc ve
#   Inverse H&S (~11%), H&S top (~14%), Double Bottom/Top, Ascending Triangle.
# He Price Action XAU doc lap nam o gold_pa_bot.py — import cac detector nay.

PA_PIVOT_LB     = 3       # pivot H4: cao/thap hon 3 nen moi ben (~12h)
PA_PEAK_TOL     = 0.0035  # 2 dinh/day coi la "bang nhau" neu chenh < 0.35%
PA_BREAK_FRESH  = 3       # neckline break phai moi xay ra trong 3 nen H4 cuoi


def _pa_pivots(highs, lows, lb=PA_PIVOT_LB):
    """Tim swing high/low tren H4. Returns (peak_idx_list, trough_idx_list)."""
    peaks, troughs = [], []
    n = len(highs)
    for i in range(lb, n - lb):
        if all(highs[i] >= highs[i-j] for j in range(1, lb+1)) and \
           all(highs[i] >  highs[i+j] for j in range(1, lb+1)):
            peaks.append(i)
        if all(lows[i] <= lows[i-j] for j in range(1, lb+1)) and \
           all(lows[i] <  lows[i+j] for j in range(1, lb+1)):
            troughs.append(i)
    return peaks, troughs


def _pa_fresh_break(h4_c, level, direction):
    """Break neckline phai MOI: nen hien tai vuot level nhung trong
    PA_BREAK_FRESH nen truoc do van con nen dong cung phia cu."""
    if direction == 'SELL':   # break xuong
        if h4_c[-1] >= level:
            return False
        recent = h4_c[-(PA_BREAK_FRESH+1):-1]
        return any(c >= level for c in recent)
    else:                     # break len
        if h4_c[-1] <= level:
            return False
        recent = h4_c[-(PA_BREAK_FRESH+1):-1]
        return any(c <= level for c in recent)


def _detect_double_top_bottom(h4_c, h4_h, h4_l):
    """Double Top / Double Bottom voi xac nhan neckline break.
    Target = measured move (chieu cao pattern chieu xuong/len tu neckline)."""
    out = []
    peaks, troughs = _pa_pivots(h4_h, h4_l)
    price = h4_c[-1]

    # Double Top: 2 dinh gan bang nhau, cach >= 5 nen H4, break valley
    if len(peaks) >= 2:
        p1, p2 = peaks[-2], peaks[-1]
        if p2 - p1 >= 5 and abs(h4_h[p2] - h4_h[p1]) / h4_h[p1] <= PA_PEAK_TOL:
            valley = min(h4_l[p1:p2+1])
            height = (h4_h[p1] + h4_h[p2]) / 2 - valley
            if height > 0 and _pa_fresh_break(h4_c, valley, 'SELL'):
                out.append({
                    'code': 'double_top', 'kind': 'chart', 'dir': 'SELL',
                    'entry': price, 'neckline': valley,
                    'target': valley - height,
                    'inval':  max(h4_h[p1], h4_h[p2]),
                    'note':   f'2 đỉnh {h4_h[p1]:,.1f}/{h4_h[p2]:,.1f}, neckline {valley:,.1f} đã phá',
                })

    # Double Bottom: 2 day gan bang nhau, break peak giua
    if len(troughs) >= 2:
        t1, t2 = troughs[-2], troughs[-1]
        if t2 - t1 >= 5 and abs(h4_l[t2] - h4_l[t1]) / h4_l[t1] <= PA_PEAK_TOL:
            ridge  = max(h4_h[t1:t2+1])
            height = ridge - (h4_l[t1] + h4_l[t2]) / 2
            if height > 0 and _pa_fresh_break(h4_c, ridge, 'BUY'):
                out.append({
                    'code': 'double_bottom', 'kind': 'chart', 'dir': 'BUY',
                    'entry': price, 'neckline': ridge,
                    'target': ridge + height,
                    'inval':  min(h4_l[t1], h4_l[t2]),
                    'note':   f'2 đáy {h4_l[t1]:,.1f}/{h4_l[t2]:,.1f}, neckline {ridge:,.1f} đã phá',
                })
    return out


def _detect_head_shoulders(h4_c, h4_h, h4_l):
    """Head & Shoulders (top) + Inverse H&S voi xac nhan neckline break.
    Vai phai/trai phai can xung (chenh < 50% bien do dau-vai)."""
    out = []
    peaks, troughs = _pa_pivots(h4_h, h4_l)
    price = h4_c[-1]

    # H&S top: 3 dinh cuoi — dau cao hon 2 vai, 2 vai can xung
    if len(peaks) >= 3:
        l_i, h_i, r_i = peaks[-3], peaks[-2], peaks[-1]
        ls, hd, rs = h4_h[l_i], h4_h[h_i], h4_h[r_i]
        head_amp = hd - max(ls, rs)
        if hd > ls and hd > rs and head_amp / hd >= 0.002 and \
           abs(ls - rs) <= 0.5 * head_amp:
            low1 = min(h4_l[l_i:h_i+1])
            low2 = min(h4_l[h_i:r_i+1])
            neckline = (low1 + low2) / 2
            if _pa_fresh_break(h4_c, neckline, 'SELL'):
                out.append({
                    'code': 'hs_top', 'kind': 'chart', 'dir': 'SELL',
                    'entry': price, 'neckline': neckline,
                    'target': neckline - (hd - neckline),
                    'inval':  rs,
                    'note':   f'Đầu {hd:,.1f}, vai {ls:,.1f}/{rs:,.1f}, neckline {neckline:,.1f} đã phá',
                })

    # Inverse H&S: 3 day cuoi — dau thap hon 2 vai, 2 vai can xung
    if len(troughs) >= 3:
        l_i, h_i, r_i = troughs[-3], troughs[-2], troughs[-1]
        ls, hd, rs = h4_l[l_i], h4_l[h_i], h4_l[r_i]
        head_amp = min(ls, rs) - hd
        if hd < ls and hd < rs and head_amp / max(hd, 1e-9) >= 0.002 and \
           abs(ls - rs) <= 0.5 * head_amp:
            hi1 = max(h4_h[l_i:h_i+1])
            hi2 = max(h4_h[h_i:r_i+1])
            neckline = (hi1 + hi2) / 2
            if _pa_fresh_break(h4_c, neckline, 'BUY'):
                out.append({
                    'code': 'hs_inv', 'kind': 'chart', 'dir': 'BUY',
                    'entry': price, 'neckline': neckline,
                    'target': neckline + (neckline - hd),
                    'inval':  rs,
                    'note':   f'Đầu {hd:,.1f}, vai {ls:,.1f}/{rs:,.1f}, neckline {neckline:,.1f} đã phá',
                })
    return out


# ── Gold Macro Outlook — ban tin vi mo hang ngay (THONG TIN, khong phai lenh) ──

_OUTLOOK_FACTORS = [
    ('dxy',  'DXY (chỉ số USD — nghịch chiều vàng)',          0.30),
    ('tips', 'TIPS real yield (driver mạnh nhất — nghịch)',    0.25),
    ('tny',  'TNX nominal yield 10Y (nghịch chiều)',           0.20),
    ('vix',  'VIX chỉ số sợ hãi (thuận chiều — safe haven)',   0.15),
    ('oil',  'Oil — kỳ vọng lạm phát (thuận chiều nhẹ)',       0.10),
]


def send_gold_outlook(state, now):
    """Ban tin vi mo vang hang ngay — tra loi cau hoi 'vi mo dang noi gi ve vang?'
    ngay ca khi he thong vote khong co signal. Moi 24h, trong session vang."""
    last = state.get('last_gold_outlook', 0)
    if (now.timestamp() - last) < 24 * 3600:
        return
    if now.hour not in PAIR_CONFIG['XAU/USD']['trade_hours']:
        return

    score, comps = gold_macro_score()
    sym, yf_sym = 'XAU/USD', SYMBOLS['XAU/USD']
    bars = _price_history.get(sym, {}).get('bars', [])
    if len(bars) < 60:
        return
    closes = [b['c'] for b in bars]
    highs  = [b['h'] for b in bars]
    lows   = [b['l'] for b in bars]
    price  = closes[-1]

    h4_dir, h4_score_v, _ = h4_trend(closes, highs, lows)
    d1_dir, d1_score_v, _ = d1_trend(sym, yf_sym)
    w1_dir, w1_score_v, _ = w1_trend(sym, yf_sym)
    r_val = rsi(closes)

    if score > 0.25:
        verdict, v_icon = 'NGHIÊNG TĂNG (bullish)', '🟢'
    elif score > 0.10:
        verdict, v_icon = 'Nghiêng tăng nhẹ', '🟡'
    elif score < -0.25:
        verdict, v_icon = 'NGHIÊNG GIẢM (bearish)', '🔴'
    elif score < -0.10:
        verdict, v_icon = 'Nghiêng giảm nhẹ', '🟡'
    else:
        verdict, v_icon = 'TRUNG TÍNH', '⚪'

    # Doi chieu vi mo vs ky thuat — diem dang gia nhat cua ban tin
    tech_dir = h4_dir
    macro_dir = 'BULL' if score > 0.10 else ('BEAR' if score < -0.10 else 'NEUTRAL')
    if macro_dir == 'NEUTRAL' or tech_dir == 'NEUTRAL':
        align_line = '➡️ Vĩ mô / kỹ thuật chưa đủ rõ — chờ thêm tín hiệu'
    elif macro_dir == tech_dir:
        align_line = '✅ Vĩ mô và kỹ thuật H4 ĐỒNG THUẬN — đáng chú ý'
    else:
        align_line = ('⚠️ Vĩ mô và kỹ thuật H4 MÂU THUẪN — thường là pullback/'
                      'tích lũy, tránh vào lệnh sớm')

    # S/R H4 gan nhat
    h4_c, h4_h, h4_l = resample_to_h4(closes, highs, lows)
    sr_line = ''
    if len(h4_c) >= 10:
        lvs = find_sr_levels(h4_h, h4_l, h4_c, lookback=60)
        ns = next((lv for lv in lvs if lv['type'] == 'S'), None)
        nr = next((lv for lv in lvs if lv['type'] == 'R'), None)
        parts = []
        if ns: parts.append(f'S {fmt_price(sym, ns["price"])}')
        if nr: parts.append(f'R {fmt_price(sym, nr["price"])}')
        if parts:
            sr_line = '🏗 H4 S/R gần nhất: ' + ' | '.join(parts)

    factor_lines = []
    for key, label, w in _OUTLOOK_FACTORS:
        v = comps.get(key, 0.0)
        factor_lines.append(f'  {_icon(v)} {label}: {v:+.2f} (w={w:.2f})')

    def _tf(d, s):
        ic = '🟢' if d == 'BULL' else ('🔴' if d == 'BEAR' else '⚪')
        return f'{ic}{d}({s:+.2f})'

    now_vn = now.astimezone(VN_TZ)
    msg = '\n'.join([
        '🥇 <b>GOLD MACRO OUTLOOK</b> — bản tin vĩ mô hàng ngày',
        '<i>Thông tin định hướng — KHÔNG phải tín hiệu vào lệnh</i>',
        '━━━━━━━━━━━━━━━━━━━━',
        '',
        f'{v_icon} Vĩ mô tổng hợp: <b>{verdict}</b>  (score {score:+.2f})',
        '',
        '🌐 5 yếu tố (điểm đã quy về chiều vàng — dương = ủng hộ tăng):',
        *factor_lines,
        '',
        f'📊 Kỹ thuật: H4 {_tf(h4_dir, h4_score_v)} | D1 {_tf(d1_dir, d1_score_v)} '
        f'| W1 {_tf(w1_dir, w1_score_v)} | RSI(H1) {r_val:.0f}',
        f'💰 Giá: {fmt_price(sym, price)}',
        *(([sr_line]) if sr_line else []),
        '',
        align_line,
        '',
        f'⏰ {now_vn.strftime("%H:%M %d/%m/%Y")} | Bản tin tiếp theo sau 24h',
    ])
    result = send_telegram(msg)
    if result.get('ok'):
        state['last_gold_outlook'] = now.timestamp()
        _log.info(f'[XAU/USD] OUTLOOK sent score={score:+.2f} h4={h4_dir} d1={d1_dir}')
        print(f'  [OUTLOOK] Da gui ban tin vang (score={score:+.2f})')
    else:
        print(f'  [OUTLOOK] Loi Telegram: {result}')


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
def analyze(sym, yf_sym, now=None, state=None):
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
            _log.info(f'[{sym}] BLOCKED no_data bars={len(closes) if closes else 0}')
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
            _log.info(f'[{sym}] BLOCKED sanity_fail price={price:.5f} valid=[{lo},{hi}]')
            return None

        # [TANG 1 — FUNDAMENTAL] Economic Calendar: block truoc khi tinh toan nang
        fund = fetch_fundamental(now)
        cal_status, cal_reason = check_calendar(fund, sym, now)
        if cal_status == 'HARD':
            print(f'  [D] Calendar HARD block: {cal_reason}')
            _log.info(f'[{sym}] BLOCKED calendar_hard {cal_reason}')
            return None

        # [HURST] Dung long_closes (toi da 200 nen) de ket qua on dinh hon
        H      = hurst_exponent(long_closes)
        regime = 'TREND' if H > 0.55 else ('RANGE' if H < 0.45 else 'NEUTRAL')

        # [LOC 3] Block RANGE sau: nguong dong theo percentile lich su cua cap
        # (fallback nguong tinh PAIR_CONFIG khi chua du 48 mau hourly)
        eff_block = dynamic_hurst_block(state, sym, H,
                                        now.timestamp(), cfg['hurst_block'])
        n_hist  = len(state.get('hurst_hist', {}).get(sym, [])) if state is not None else 0
        blk_src = 'dyn' if n_hist >= HURST_HIST_MIN_N else 'static'
        if H < eff_block:
            print(f'  [D] H={H:.3f} < {eff_block:.3f} ({blk_src}) RANGE sau ({ln} bars), bo qua')
            _log.info(f'[{sym}] BLOCKED hurst H={H:.3f} threshold={eff_block:.3f} src={blk_src} bars={ln}')
            return None

        # [LOC 3b] ADX filter: chan sideways kep — ca ADX lan Hurst deu yeu
        adx_val, pdi, mdi = adx_indicator(long_highs, long_lows, long_closes)
        if adx_val < 15 and H < 0.50:
            print(f'  [D] ADX={adx_val:.1f} + H={H:.3f} ca hai yeu — sideways kep, bo qua')
            _log.info(f'[{sym}] BLOCKED adx_hurst ADX={adx_val:.1f} H={H:.3f}')
            return None

        # [DOW THEORY] Cau truc thi truong HH/HL — Bab 17 sach TAFMP
        dow = dow_structure(long_closes, long_highs, long_lows, lookback=60)

        # [TRENDLINE] Phat hien trendline tu swing points — Bab 5 sach TAFMP
        tl = find_trendlines(long_closes, long_highs, long_lows, lookback=80)
        tl_v = tl['tl_vote']

        # [PSYCH LEVELS] Muc gia tam ly (so tron) — Bab 8 sach TAFMP
        psych_lvls = psychological_levels(price)

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

        # [TRENDLINE VOTE] Tin hieu trendline tham gia bau phieu (Bab 5)
        votes     = [rsi_v, ema_v, mac_v, bb_v, mom_v, tl_v]
        vote_lbls = ['RSI', 'EMA', 'MACD', 'BB', 'Mom', 'TL']
        bull_cnt  = sum(v for v in votes if v > 0)
        bear_cnt  = sum(-v for v in votes if v < 0)

        # Pre-filter: phai co it nhat 2 phieu mot chieu moi phan tich tiep
        if max(bull_cnt, bear_cnt) < 2:
            print(f'  [D] BUY={bull_cnt} BEAR={bear_cnt} — qua it phieu, skip')
            _log.info(f'[{sym}] BLOCKED few_votes BUY={bull_cnt} BEAR={bear_cnt}')
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
            _log.info(f'[{sym}] BLOCKED counter_trend H={H:.3f} H4={h4_dir}')
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
            print(f'  [D] BUY={bull_cnt} BEAR={bear_cnt} — chua du {min_v}/6 (H4={h4_dir})')
            _log.info(f'[{sym}] BLOCKED low_votes BUY={bull_cnt} BEAR={bear_cnt} need={min_v} H4={h4_dir}')
            return None

        # [D1] Chi dung de lay key levels (TP/SL) — KHONG dung lam bo loc chieu
        # D1 direction chi hien thi trong Telegram lam tham khao, khong anh huong entry
        d1_dir, d1_score_val, d1_det = d1_trend(sym, yf_sym)

        # [W1] Weekly trend — buc tranh lon nhat, loc counter-trend tuan
        w1_dir, w1_score_val, w1_det = w1_trend(sym, yf_sym)

        # Cap nhat aligned/opposed theo signal chinh thuc (chi dung cho confidence)
        d1_aligned = (signal == 'BUY' and d1_dir == 'BULL') or \
                     (signal == 'SELL' and d1_dir == 'BEAR')
        d1_opposed = (signal == 'BUY' and d1_dir == 'BEAR') or \
                     (signal == 'SELL' and d1_dir == 'BULL')
        h4_aligned = (signal == 'BUY' and h4_dir == 'BULL') or \
                     (signal == 'SELL' and h4_dir == 'BEAR')
        h4_opposed = (signal == 'BUY' and h4_dir == 'BEAR') or \
                     (signal == 'SELL' and h4_dir == 'BULL')
        w1_aligned = (signal == 'BUY' and w1_dir == 'BULL') or \
                     (signal == 'SELL' and w1_dir == 'BEAR')
        w1_opposed = (signal == 'BUY' and w1_dir == 'BEAR') or \
                     (signal == 'SELL' and w1_dir == 'BULL')

        # Block counter-weekly-trend khi xu huong tuan manh (TREND + H > 0.52)
        # W1 nguoc chieu trong momentum manh = bay counter-trend nguy hiem nhat
        if w1_opposed and regime == 'TREND' and H > 0.52:
            print(f'  [D] W1={w1_dir} nguoc chieu {signal} trong TREND (H={H:.3f}) — counter weekly trend')
            _log.info(f'[{sym}] BLOCKED counter_w1 {signal} W1={w1_dir} H={H:.3f}')
            return None

        # [M15] Phan tich M15 — xac nhan entry timing va phat hien "chasing"
        m15_dir = analyze_m15(sym, yf_sym)

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
            _log.info(f'[{sym}] BLOCKED rsi_extreme RSI={r_val:.0f} {signal} votes={vote_count}')
            return None

        # [LOC 4b — EXHAUSTION GUARD 12/06/2026] Chan lenh thuan-move trong vung
        # kiet suc, TRU KHI gia dang pha day/dinh moi (trend con song thi van theo).
        # Khac rsi_extreme (H1, bi override boi 4 phieu): guard nay do D1 + khong
        # override duoc bang vote — vote cao o cuoi trend chinh la cai bay.
        exh = exhaustion_state(long_bars)
        exh_pen  = 0
        exh_warn = None
        if exh:
            sig_into_move = (signal == 'SELL' and exh.get('dir') == 'DOWN') or \
                            (signal == 'BUY'  and exh.get('dir') == 'UP')
            if sig_into_move and not exh['at_extreme']:
                print(f'  [D] Exhaustion: D1 RSI={exh["d1_rsi"]} pctl={exh["pctl"]:.0f}% '
                      f'— {signal} vao vung kiet ma gia khong pha extreme, bo qua')
                _log.info(f'[{sym}] BLOCKED exhaustion {signal} d1_rsi={exh["d1_rsi"]} '
                          f'pctl={exh["pctl"]:.0f} at_extreme=False')
                return None
            if sig_into_move:                      # cho phep vi dang pha extreme
                exh_pen  = 8
                exh_warn = (f'⚠️ Vùng kiệt sức (D1 RSI {exh["d1_rsi"]}, move 5 phiên '
                            f'> {exh["pctl"]:.0f}% lịch sử) — chỉ vào vì giá đang phá '
                            f'đáy/đỉnh mới. Cân nhắc giảm lot, SL kỷ luật.')
            elif exh['soft'] and ((signal == 'SELL' and exh['move_dir'] == 'DOWN') or
                                  (signal == 'BUY' and exh['move_dir'] == 'UP')):
                exh_pen = 5                        # vung canh bao: tru conf, khong chan

        # [PAIR MACRO] macro rieng tung cap (tai su dung _gold_cache)
        pair_macro = None
        m_score, m_comps = macro_score(sym)
        if m_comps:
            sig_dir     = 1 if signal == 'BUY' else -1
            macro_align = m_score * sig_dir
            if macro_align < -0.30:
                print(f'  [D] Macro={m_score:.2f} mau thuan {signal} — {m_comps}')
                _log.info(f'[{sym}] BLOCKED macro_strong {signal} score={m_score:.2f}')
                return None
            if macro_align < -0.12 and vote_count < min(5, min_v + 1):
                print(f'  [D] Macro={m_score:.2f} mau thuan ro, can them 1 vote')
                _log.info(f'[{sym}] BLOCKED macro_mild {signal} score={m_score:.2f} votes={vote_count}')
                return None
            pair_macro = {'score': round(m_score, 2), **m_comps}

        # [TANG 2 — NEWS SENTIMENT]
        sent_score = get_sentiment_score(fund, sym)
        sig_dir    = 1 if signal == 'BUY' else -1
        sent_align = sent_score * sig_dir
        if sent_align < -0.35:
            print(f'  [D] Sentiment={sent_score:.2f} mau thuan manh voi {signal}')
            _log.info(f'[{sym}] BLOCKED sentiment_strong {signal} sent={sent_score:.2f}')
            return None
        if sent_align < -0.15 and vote_count < min(5, min_v + 1):
            print(f'  [D] Sentiment={sent_score:.2f} mau thuan nhe, can them vote')
            _log.info(f'[{sym}] BLOCKED sentiment_mild {signal} sent={sent_score:.2f} votes={vote_count}')
            return None

        # [TANG 3 — FEAR & GREED]
        fg_penalty, fg_reason = get_fg_context(fund, sym, signal)
        if fg_penalty and vote_count < min(5, min_v + 1):
            print(f'  [D] F&G: {fg_reason}')
            _log.info(f'[{sym}] BLOCKED fear_greed {signal} {fg_reason}')
            return None

        aligned_lbls = [vote_lbls[i] for i, v in enumerate(votes)
                        if (v > 0 and signal == 'BUY') or (v < 0 and signal == 'SELL')]

        # --- Do tin cay ---
        base_conf    = {2: 50, 3: 60, 4: 75, 5: 90, 6: 95}.get(vote_count, 60)
        im_aligned   = (im_s > 0.15 and signal == 'BUY') or (im_s < -0.15 and signal == 'SELL')
        im_bonus     = 5 if im_aligned else 0
        regime_bonus = 5 if regime == 'TREND' else 0  # RANGE không cộng bonus — min_votes đã +1 ở trên
        history_bonus = 2 if ln >= 200 else 0
        # W1: +7 khi dong thuan (xu huong tuan ung ho), -5 khi nguoc chieu (da qua block → chi penalty nhe)
        # H4: +5/-3 | D1: +3/-2 | M15: +4 khi hop luu, -2 khi nguoc (entry timing)
        m15_aligned = (m15_dir == signal)
        m15_opposed = (m15_dir is not None and m15_dir != signal)
        mtf_bonus = (7 if w1_aligned else (-5 if w1_opposed else 0)) + \
                    (5 if h4_aligned else (-3 if h4_opposed else 0)) + \
                    (3 if d1_aligned else (-2 if d1_opposed else 0)) + \
                    (4 if m15_aligned else (-2 if m15_opposed else 0))
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

        # [DOW THEORY BONUS] Bab 17: cau truc HH/HL xac nhan = +5, nguoc chieu = -5
        sig_dir    = 1 if signal == 'BUY' else -1
        dow_bonus  = (5  if dow['score'] * sig_dir > 0.5
                      else -5 if dow['score'] * sig_dir < -0.5
                      else 0)

        # [TRENDLINE BONUS] Bab 5: cham trendline = +5, pha trendline thuan = +3
        tl_bonus = 0
        if tl_v == 1 and signal == 'BUY':
            tl_bonus = 5 if 'TOUCH' in tl['tl_label'] else 3
        elif tl_v == -1 and signal == 'SELL':
            tl_bonus = 5 if 'TOUCH' in tl['tl_label'] else 3

        # [PSYCH LEVEL PENALTY] Bab 8: vao lenh sap vuong muc tam ly nguoc chieu = -3
        psych_penalty = 0
        for pl in psych_lvls[:3]:
            dist = abs(pl['price'] - price) / price
            if dist < 0.003:  # rat gan muc tam ly
                if (signal == 'BUY' and pl['type'] == 'R') or (signal == 'SELL' and pl['type'] == 'S'):
                    psych_penalty = -3
                    break

        conf = min(95, base_conf + im_bonus + regime_bonus + history_bonus + mtf_bonus
                   + sr_bonus + fib_bonus + dow_bonus + tl_bonus + psych_penalty
                   - exh_pen)

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

        entry_low   = price - atr_val * 0.2
        entry_high  = price + atr_val * 0.2
        # Chase limit: nguong gia toi da chap nhan khi user thay tin tri hoan 10-15p
        # Vuot nguong nay = gia da chay xa, entry risk/reward bi xau di ro rang
        chase_limit = price + atr_val * 0.5 if signal == 'BUY' else price - atr_val * 0.5

        # [ANTI-FOMO 12/06/2026] Vote system von lagging — khi du phieu dong thuan,
        # gia thuong DA chay. Vao market luc do = duoi gia (FOMO co he thong).
        # Gia cach EMA20 > 1.2 ATR theo huong tin hieu → de xuat LIMIT cho hoi ve
        # gan EMA20. SL giu nguyen muc cau truc (swing) → R:R tot len, lot to len.
        # Tracking (pending_validations) van theo gia signal de so sanh duoc voi lich su.
        ext_atr = (((price - e20) if signal == 'BUY' else (e20 - price)) / atr_val
                   if atr_val > 0 else 0.0)
        entry_mode  = 'MARKET'
        limit_entry = None
        if ext_atr > 1.2:
            entry_mode  = 'LIMIT'
            limit_entry = e20 + 0.3 * atr_val if signal == 'BUY' else e20 - 0.3 * atr_val

        _log.info(f'[{sym}] SIGNAL {signal} conf={conf} votes={vote_count}/6 regime={regime} H={H:.3f} ADX={adx_val:.1f}')
        return {
            'sym': sym, 'signal': signal, 'price': price, 'rsi': round(r_val, 1),
            'vote_count': vote_count, 'vote_lbls': aligned_lbls,
            'conf': conf,
            'sl': sl, 'tp': tp, 'tp2': tp2,
            'sl_pct': sl_pct, 'tp_pct': tp_pct, 'tp2_pct': tp2_pct,
            'rr1': rr1, 'rr2': rr2,
            'entry_low': entry_low, 'entry_high': entry_high, 'chase_limit': chase_limit,
            'entry_mode': entry_mode, 'limit_entry': limit_entry,
            'ext_atr': round(ext_atr, 2),
            'atr': atr_val, 'exh_warn': exh_warn,
            'm15_dir': m15_dir,
            'phase': phase_name, 'hurst': round(H, 3), 'adx': round(adx_val, 1), 'regime': regime,
            'history_bars': ln,
            'mtf': {
                'w1_dir':   w1_dir,   'w1_score': w1_det.get('score', 0),
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
                'tl':   tl_v,
            },
            # [BOOK-ENHANCED] Cac module tu sach Technical Analysis for Mega Profit
            'trendline': {
                'label':   tl['tl_label'],
                'tl_vote': tl_v,
                'up_valid':   tl['up']['valid']   if tl['up']  else False,
                'dn_valid':   tl['dn']['valid']   if tl['dn']  else False,
                'up_touches': tl['up']['touches'] if tl['up']  else 0,
                'dn_touches': tl['dn']['touches'] if tl['dn']  else 0,
            },
            'dow': {
                'structure': dow['structure'],
                'score':     round(dow['score'], 2),
                'bonus':     dow_bonus,
            },
            'psych_levels': [
                {'price': round(p['price'], 5), 'type': p['type'], 'strength': p['strength']}
                for p in psych_lvls[:4]
            ],
            'consensus': True,   # Luon True khi da qua nguong 3/5
        }
    except Exception as e:
        print(f'  [{sym}] Loi: {e}')
        return None

# ── Lay gia hien tai ──────────────────────────────────────────
def fetch_current_price(yf_sym):
    try:
        df = yf.Ticker(yf_sym).history(period='1d', interval='5m')
        if df is not None and len(df) > 0:
            return float(df['Close'].iloc[-1])
    except Exception:
        pass
    # Fallback: Twelve Data /price (1 req, khong ton quota time_series)
    if TWELVE_DATA_KEY:
        td_sym = _YF_TO_TD_SYM.get(yf_sym)
        if td_sym:
            try:
                r = requests.get(
                    'https://api.twelvedata.com/price',
                    params={'symbol': td_sym, 'apikey': TWELVE_DATA_KEY},
                    timeout=10,
                )
                data = r.json()
                if 'price' in data:
                    return float(data['price'])
            except Exception:
                pass
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

def check_tp_sl(yf_sym, since_ts, entry, sl, tp, signal):
    """Quet nen 5m tu since_ts -> now, tra ve cu CHAM TP hay SL DAU TIEN.
    Tra: (outcome, exit_price, cur_price, win_high, win_low)
      outcome: 'TP' | 'SL' | None (chua cham cai nao)
    Neu mot nen cham ca TP lan SL -> gia dinh SL truoc (pessimistic, chuan backtest).
    """
    try:
        df = yf.Ticker(yf_sym).history(period='2d', interval='5m')
    except Exception:
        return None, None, None, None, None
    if df is None or len(df) == 0:
        return None, None, None, None, None
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize('UTC')
    else:
        df.index = df.index.tz_convert('UTC')
    start  = pd.Timestamp(since_ts, unit='s', tz='UTC')
    window = df.loc[df.index >= start]
    if len(window) == 0:
        return None, None, float(df['Close'].iloc[-1]), None, None
    cur_price = float(window['Close'].iloc[-1])
    win_high  = float(window['High'].max())
    win_low   = float(window['Low'].min())
    for _, row in window.iterrows():
        hi = float(row['High']); lo = float(row['Low'])
        if signal == 'BUY':
            tp_bar = hi >= tp
            sl_bar = lo <= sl
        else:
            tp_bar = lo <= tp
            sl_bar = hi >= sl
        if tp_bar and sl_bar:
            return 'SL', sl, cur_price, win_high, win_low   # cung nen -> SL truoc
        if tp_bar:
            return 'TP', tp, cur_price, win_high, win_low
        if sl_bar:
            return 'SL', sl, cur_price, win_high, win_low
    return None, None, cur_price, win_high, win_low

# ── Format ────────────────────────────────────────────────────
def fmt_price(sym, price):
    if 'JPY' in sym:    return f'{price:,.3f}'
    if sym in ('XAU/USD', 'WTI/USD'): return f'{price:,.2f}'
    return f'{price:.5f}'

def price_to_pips(sym, raw_diff):
    """
    Chuyen doi raw price diff (co dau) thanh pips.
    raw_diff > 0 = co loi theo chieu signal, < 0 = thua lo.
    - Standard (EURUSD...): 1 pip = 0.0001 → × 10000
    - JPY pairs:             1 pip = 0.01   → × 100
    - XAU/USD, WTI/USD:     1 pip = $0.10  → × 10
    """
    if 'JPY' in sym:                  return round(raw_diff * 100,   1)
    if sym in ('XAU/USD', 'WTI/USD'): return round(raw_diff * 10,    1)
    return                            round(raw_diff * 10000, 1)

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
def send_telegram(msg, reply_to=None, keyboard=None):
    url     = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT, 'text': msg, 'parse_mode': 'HTML'}
    if reply_to:
        payload['reply_to_message_id'] = reply_to
    if keyboard:
        payload['reply_markup'] = json.dumps({'inline_keyboard': keyboard})
    resp = requests.post(url, json=payload, timeout=10)
    return resp.json()

def get_tg_updates(offset=None):
    """Lay callback_query tu nguoi dung (nut xac nhan vao/bo qua lenh)."""
    url    = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates'
    params = {'limit': 100, 'timeout': 0, 'allowed_updates': ['callback_query']}
    if offset:
        params['offset'] = offset
    try:
        return requests.get(url, params=params, timeout=10).json().get('result', [])
    except Exception:
        return []


def answer_callback(callback_query_id):
    """Tra loi callback de xoa loading indicator tren nut Telegram."""
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery',
            json={'callback_query_id': callback_query_id}, timeout=5,
        )
    except Exception:
        pass


def _remove_keyboard(message_id):
    """Xoa inline keyboard tren tin nhan signal sau khi user da bam nut (tranh bam lai)."""
    if not message_id or not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup',
            json={'chat_id': TELEGRAM_CHAT, 'message_id': message_id, 'reply_markup': {}},
            timeout=5,
        )
    except Exception:
        pass


def get_active_corr(state, sym, signal, now_ts):
    """Tra ve list pending signals co cung USD direction voi (sym, signal) hien tai.
    confirmed=True → user da vao lenh (hard block); None/False → chua xac nhan (warn only).
    """
    direction = _USD_SHORT.get(sym, {}).get(signal)
    if direction is None:
        return []
    conflicts = []
    for v in state.get('pending_validations', []):
        if v['sym'] == sym:
            continue
        if v.get('expires_at', 0) < now_ts:
            continue
        other_dir = _USD_SHORT.get(v['sym'], {}).get(v['signal'])
        if other_dir == direction:
            conflicts.append({'sym': v['sym'], 'signal': v['signal'],
                              'confirmed': v.get('entry_confirmed')})
    return conflicts


def process_callbacks(state):
    """Doc xac nhan vao/bo qua lenh tu Telegram inline keyboard, cap nhat pending_validations."""
    if not TELEGRAM_TOKEN:
        return
    last_id = state.get('last_tg_update_id', 0)
    updates = get_tg_updates(last_id + 1 if last_id else None)
    if not updates:
        return
    cb_lookup = {v['cb_key']: v for v in state.get('pending_validations', []) if 'cb_key' in v}
    for upd in updates:
        cb = upd.get('callback_query')
        if not cb:
            continue
        state['last_tg_update_id'] = upd['update_id']
        answer_callback(cb['id'])
        data = cb.get('data', '')
        if not (data.startswith('confirm_yes_') or data.startswith('confirm_no_')):
            continue
        parts = data.split('_', 3)   # ['confirm', 'yes'/'no', sym_key, ts_key]
        if len(parts) != 4:
            continue
        _, decision, sym_key, ts_key = parts
        key = f'{sym_key}_{ts_key}'
        if key in cb_lookup:
            v = cb_lookup[key]
            v['entry_confirmed'] = (decision == 'yes')
            sym    = v.get('sym', sym_key)
            signal = v.get('signal', '?')
            entry  = v.get('entry_price', 0)
            if decision == 'yes':
                confirm_msg = (
                    f'<b>Ghi nhan: DA VAO LENH</b>\n'
                    f'{sym} {signal} @ {fmt_price(sym, entry)}'
                )
            else:
                confirm_msg = (
                    f'<b>Ghi nhan: BO QUA</b>\n'
                    f'{sym} {signal}'
                )
            send_telegram(confirm_msg, reply_to=v.get('message_id'))
            _remove_keyboard(v.get('message_id'))
            label = 'DA VAO' if decision == 'yes' else 'BO QUA'
            print(f'  [callback] {sym_key} -> {label}')


# ── Theo doi den khi cham TP/SL ───────────────────────────────
def run_validations(state, now):
    """Theo doi MOI LENH dang cho cho den khi gia THUC SU cham TP hoac SL,
    roi moi bao 1 lan duy nhat. KHONG con bao 'dung huong/sai huong'.
    Lenh chua cham TP/SL sau MONITOR_HOURS -> dong theo doi (tin trung tinh)."""
    pending   = state.get('pending_validations', [])
    remaining = []

    for v in pending:
        sym    = v['sym']
        signal = v['signal']
        entry  = v.get('entry_price')
        sl_val = v.get('sl')
        tp_val = v.get('tp')
        yf_sym = SYMBOLS.get(sym)

        # Bo qua format cu / thieu thong tin can thiet
        if not yf_sym or entry is None or sl_val is None or tp_val is None:
            continue

        elapsed_h  = (now.timestamp() - v['sent_at']) / 3600
        sent_dt    = datetime.fromtimestamp(v['sent_at'], tz=timezone.utc).astimezone(VN_TZ)
        now_vn_val = now.astimezone(VN_TZ)

        print(f'[theo doi] {signal} {sym} ({elapsed_h:.1f}h)...', end=' ', flush=True)
        outcome, exit_price, cur_price, win_high, win_low = check_tp_sl(
            yf_sym, v['sent_at'], entry, sl_val, tp_val, signal)

        # ── Chua cham TP lan SL ──────────────────────────────────
        if outcome is None:
            if elapsed_h >= MONITOR_HOURS:
                print('het han theo doi, dong')
                send_telegram(
                    f'⏰ <b>Đóng theo dõi — chưa chạm TP/SL sau {int(MONITOR_HOURS)}h</b>\n'
                    f'📍 {sym} {signal} @ {fmt_price(sym, entry)}\n'
                    f'⏱ Đặt lệnh: {sent_dt.strftime("%d/%m %H:%M")} (Giờ VN)\n'
                    f'— Lệnh không chạm TP hay SL trong {int(MONITOR_HOURS)}h, ngừng theo dõi (không tính kết quả).',
                    reply_to=v.get('message_id'),
                )
                time.sleep(1)
                # KHONG ghi vao results — chi TP/SL moi tinh ket qua
            else:
                print('chua cham, theo doi tiep')
                remaining.append(v)   # giu lai, check lai o lan chay sau
            continue

        # ── Da cham TP hoac SL -> bao 1 lan duy nhat ─────────────
        if outcome == 'TP':
            correct       = True
            verdict_emoji = '🎉'; verdict = 'CHỐT LỜI (TP)'
            pip_result    = price_to_pips(sym,  abs(tp_val - entry))   # luon duong
            move_text     = f'TP chạm! +{abs(tp_val-entry)/entry*100:.3f}%'
            tp_sl_line    = f'🎉 ĐÃ CHẠM TP ({fmt_price(sym, tp_val)}) — CHỐT LỜI!'
        else:  # SL
            correct       = False
            verdict_emoji = '💸'; verdict = 'DỪNG LỖ (SL)'
            pip_result    = price_to_pips(sym, -abs(sl_val - entry))   # luon am
            move_text     = f'SL chạm! -{abs(sl_val-entry)/entry*100:.3f}%'
            tp_sl_line    = f'💸 ĐÃ CHẠM SL ({fmt_price(sym, sl_val)}) — DỪNG LỖ!'

        _chk_usd = round(pip_result * LOT_SIZE * 10, 2)
        _sign    = '+' if _chk_usd >= 0 else ''
        _usd_str = f'{_sign}{_chk_usd:.2f} USD ({pip_result:+.1f} pips × {LOT_SIZE} lot)'

        inds    = v.get('indicators', {})
        regime  = v.get('regime', '?')
        H       = v.get('hurst', 0.5)
        aligned = v.get('aligned', '?')
        ind_str = (f"RSI{_icon(inds.get('rsi',0))} EMA{_icon(inds.get('ema',0))} "
                  f"MACD{_icon(inds.get('macd',0))} BB{_icon(inds.get('bb',0))} "
                  f"Mom{_icon(inds.get('mom',0))} IM{_icon(inds.get('inter',0))}")

        if win_high is not None and win_low is not None:
            range_line = (f'📉 Đáy: <b>{fmt_price(sym, win_low)}</b> | '
                          f'📈 Đỉnh: <b>{fmt_price(sym, win_high)}</b>')
        else:
            range_line = ''

        msg_lines = [
            f'{verdict_emoji} <b>Kết quả — {verdict}</b>',
            '',
            f'📈 Cặp: <b>{sym}</b>',
            f'📌 {signal} @ {fmt_price(sym, entry)} → {fmt_price(sym, exit_price)}',
            f'📊 Biến động: <b>{move_text}</b>',
            f'💰 P&L: <b>{_usd_str}</b>',
        ]
        if range_line:
            msg_lines.append(range_line)
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
        # Neu reply_to that bai (tin goc bi xoa / bot bi han), gui lai khong reply
        if not result.get('ok') and v.get('message_id'):
            result = send_telegram(msg)
        if result.get('ok'):
            print(f'{verdict} OK')
            sl_pips   = price_to_pips(sym, abs(entry - sl_val))
            tp_pips   = price_to_pips(sym, abs(tp_val - entry))
            confirmed = v.get('entry_confirmed')
            state.setdefault('results', []).append({
                'sym':             sym,     'signal':  signal,  'correct': correct,
                'date':            sent_dt.strftime('%Y-%m-%d'),
                'regime':          v.get('regime', '?'),
                'outcome':         outcome,
                'pips':            pip_result,
                'entry':           round(entry,      5),
                'exit':            round(exit_price, 5),
                'sl_pips':         sl_pips,
                'tp_pips':         tp_pips,
                'conf':            v.get('conf', 0),
                'votes':           v.get('aligned', 0),
                'entry_confirmed': confirmed,
                'actual_pnl_pips': pip_result if confirmed is True else None,
            })
            # Lenh da dong -> KHONG dua lai vao remaining
        else:
            print(f'Loi Telegram: {result}')
            remaining.append(v)   # gui that bai -> giu lai, thu lai lan sau
        time.sleep(1)

    state['pending_validations'] = remaining

# ── Bao cao hieu suat tuan ────────────────────────────────────
def send_weekly_report(state):
    """Gui bao cao hieu suat per-pair qua Telegram, kích hoat moi 7 ngay."""
    results  = state.get('results', [])
    versioned = [x for x in results if x.get('date', '') >= LOGIC_VERSION]
    if len(versioned) < 5:
        return

    pair_stats = {}
    for x in versioned:
        key = f"{x.get('sym','?')} {x.get('signal','?')}"
        if key not in pair_stats:
            pair_stats[key] = {'n': 0, 'wins': 0, 'pips': []}
        pair_stats[key]['n'] += 1
        if x.get('correct'):
            pair_stats[key]['wins'] += 1
        if 'pips' in x:
            pair_stats[key]['pips'].append(x['pips'])

    rows = []
    for key, s in pair_stats.items():
        if s['n'] >= 3:
            wr      = s['wins'] / s['n'] * 100
            avg_pip = sum(s['pips']) / len(s['pips']) if s['pips'] else 0
            rows.append((key, wr, s['n'], avg_pip))
    rows.sort(key=lambda x: -x[1])

    total     = len(versioned)
    n_wins    = sum(1 for x in versioned if x.get('correct'))
    overall   = n_wins / total * 100 if total > 0 else 0
    all_pips  = [x['pips'] for x in versioned if 'pips' in x]
    total_usd = round(sum(all_pips) * LOT_SIZE * 10, 2) if all_pips else 0
    usd_sign  = '+' if total_usd >= 0 else ''

    resolved  = [x for x in versioned if x.get('outcome') in ('TP', 'SL')]
    tp_rate_str = ''
    if len(resolved) >= 5:
        tp_hits = sum(1 for x in resolved if x.get('outcome') == 'TP')
        tp_rate_str = f' | TP rate: {tp_hits/len(resolved)*100:.0f}% ({len(resolved)}R)'

    lines = [
        f'📊 <b>Báo cáo hiệu suất</b> (từ {LOGIC_VERSION})',
        f'Tổng: {n_wins}/{total} = <b>{overall:.0f}%</b>{tp_rate_str}  |  💰 {usd_sign}{total_usd:.2f} USD ({LOT_SIZE} lot)',
        '',
    ]
    for key, wr, n, avg_pip in rows:
        icon    = '🔥' if wr >= 65 else ('⚠️' if wr < 45 else '  ')
        avg_usd = round(avg_pip * LOT_SIZE * 10, 2)
        usd_s   = '+' if avg_usd >= 0 else ''
        pip_str = f'  {avg_pip:+.1f}p ({usd_s}{avg_usd:.2f}$)' if avg_pip != 0 else ''
        lines.append(f'{icon} {key}: <b>{wr:.0f}%</b> ({n}L{pip_str})')

    # Regime breakdown — Kaufman Ch21: biet regime nao he thong hoat dong tot nhat
    regime_stats = {}
    for x in versioned:
        reg = x.get('regime', '?')
        st  = regime_stats.setdefault(reg, {'n': 0, 'wins': 0})
        st['n'] += 1
        if x.get('correct'):
            st['wins'] += 1
    regime_parts = []
    for reg, s in sorted(regime_stats.items(), key=lambda kv: -kv[1]['n']):
        if s['n'] >= 3:
            wr   = s['wins'] / s['n'] * 100
            icon = '🔥' if wr >= 65 else ('⚠️' if wr < 45 else '  ')
            regime_parts.append(f'{icon} {reg}: {wr:.0f}% ({s["n"]}L)')
    if regime_parts:
        lines += ['', '📐 Theo regime:'] + [f'  {p}' for p in regime_parts]

    # (Thong ke Price Action XAU nam o bao cao tuan rieng cua gold_pa_bot.py)

    confirmed = [x for x in versioned if x.get('entry_confirmed') is True]
    if len(confirmed) >= 3:
        c_wins  = sum(1 for x in confirmed if x.get('correct'))
        c_wr    = c_wins / len(confirmed) * 100
        c_pips  = [x['actual_pnl_pips'] for x in confirmed if x.get('actual_pnl_pips') is not None]
        c_usd   = round(sum(c_pips) * LOT_SIZE * 10, 2) if c_pips else 0
        c_sign  = '+' if c_usd >= 0 else ''
        lines  += ['', f'✋ Lệnh thực vào: {c_wins}/{len(confirmed)} = <b>{c_wr:.0f}%</b>  |  {c_sign}{c_usd:.2f} USD']

    # Circuit breaker alert trong weekly report
    resolved_recent = [x for x in versioned if x.get('outcome') in ('TP', 'SL')][-5:]
    if len(resolved_recent) >= 3:
        recent_loss_streak = 0
        for x in reversed(resolved_recent):
            if not x.get('correct'):
                recent_loss_streak += 1
            else:
                break
        if recent_loss_streak >= 3:
            lines += ['', f'🛑 <b>Chuỗi thua hiện tại: {recent_loss_streak} lệnh liên tiếp</b> — cân nhắc giảm lot tuần tới']

    send_telegram('\n'.join(lines))
    state['last_weekly_report'] = datetime.now(timezone.utc).timestamp()
    _log.info(f'[REPORT] Weekly performance sent total={total} overall={overall:.0f}%')


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

    # Buoc 2: doc phan hoi nguoi dung (vao/bo qua lenh) truoc khi xac nhan
    print('\n=== Doc phan hoi nguoi dung (Telegram callback) ===')
    process_callbacks(state)

    # Buoc 2b: xac nhan lenh cu (luon chay 24/7, khong phu thuoc session)
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
        r = analyze(sym, yf_sym, now, state)

        if not r:
            print('NEUTRAL / loc ATR / khong du lieu')
            _log.info(f'[{sym}] NO_SIGNAL')
            time.sleep(0.5)
            continue

        inds = r['indicators']
        print(
            f'{r["signal"]} {r["vote_count"]}/6 phieu | '
            f'conf={r["conf"]}% | '
            f'regime={r["regime"]}(H={r["hurst"]:.2f} ADX={r["adx"]:.0f}) | '
            f'dong thuan: {", ".join(r["vote_lbls"])}'
        )

        key            = f'{sym}|{r["signal"]}'
        elapsed        = (now.timestamp() - state.get(key, 0)) / 3600
        pair_cooldown  = PAIR_CONFIG.get(sym, _DEFAULT_CONFIG).get('cooldown_hours', COOLDOWN_HOURS)
        if elapsed < pair_cooldown:
            print(f'  -> Cooldown ({elapsed:.1f}h / {pair_cooldown}h), bo qua')
            _log.info(f'[{sym}] COOLDOWN {r["signal"]} {elapsed:.1f}h/{pair_cooldown}h')
            time.sleep(0.5)
            continue

        # [LOC 5b — ANTI-PYRAMID 12/06/2026] Re-send CUNG huong trong 12h chi khi
        # entry TOT hon lenh truoc >= 0.2 ATR (SELL: gia cao hon / BUY: thap hon).
        # 11/06: 2 lenh SELL XAU cach 4h tai cung gia 4080 = nhan doi exposure
        # 1 y tuong ma khong co loi the moi — ca hai cung SL.
        prev_entry = state.get(key + '|entry')
        if elapsed < 12 and prev_entry:
            improve = ((r['price'] - prev_entry) if r['signal'] == 'SELL'
                       else (prev_entry - r['price']))
            if improve < 0.2 * r['atr']:
                print(f'  -> Trung lenh {r["signal"]} {elapsed:.1f}h truoc '
                      f'(entry cu {prev_entry:.5f}, khong tot hon), bo qua')
                _log.info(f'[{sym}] DUP_SAMEDIR {r["signal"]} {elapsed:.1f}h '
                          f'prev={prev_entry:.5f} now={r["price"]:.5f} improve={improve:.5f}')
                time.sleep(0.5)
                continue

        conf = r['conf']
        if conf < MIN_CONFIDENCE:
            print(f'  -> Do tin cay {conf}% < {MIN_CONFIDENCE}%, bo qua')
            _log.info(f'[{sym}] LOW_CONF {r["signal"]} {conf}%<{MIN_CONFIDENCE}% votes={r["vote_count"]}/6')
            time.sleep(0.5)
            continue

        # [LOC 5] Phat lat chieu: tin hieu nguoc chieu trong 12h qua → can 4/5 phieu
        # Tranh he thong chay theo noise khi thi truong choppy
        opp_key      = f'{sym}|{"SELL" if r["signal"] == "BUY" else "BUY"}'
        flip_elapsed = (now.timestamp() - state.get(opp_key, 0)) / 3600
        if flip_elapsed < 12 and r['vote_count'] < 4:
            print(f'  -> Lat chieu ({flip_elapsed:.1f}h truoc), can 4/6 phieu ({r["vote_count"]}/6), bo qua')
            _log.info(f'[{sym}] FLIP_BLOCK {r["signal"]} {flip_elapsed:.1f}h votes={r["vote_count"]}/6')
            time.sleep(0.5)
            continue

        # [LOC 6] Correlated position check — tranh double risk tren cung USD direction
        active_corr = get_active_corr(state, sym, r['signal'], now.timestamp())
        hard_block  = any(c['confirmed'] is True for c in active_corr)
        if hard_block:
            confl = next(c for c in active_corr if c['confirmed'] is True)
            print(f'  -> BLOCKED corr_bucket: {confl["sym"]} {confl["signal"]} da vao (cung USD dir)')
            _log.info(f'[{sym}] BLOCKED corr_bucket {confl["sym"]} {confl["signal"]}')
            time.sleep(0.5)
            continue
        corr_warning = ''
        if active_corr:
            names = ', '.join(f'{c["sym"]} {c["signal"]}' for c in active_corr)
            corr_warning = f'\n⚠️ <b>Rủi ro tương quan:</b> {names} đang pending (cùng hướng USD — tổng rủi ro tăng)'

        # Circuit breaker — canh bao chuoi thua
        resolved_recent = [x for x in state.get('results', []) if x.get('outcome') in ('TP', 'SL')][-3:]
        consec_loss = len(resolved_recent) >= 3 and all(not x.get('correct') for x in resolved_recent)
        cb_warning  = '\n🛑 <b>Cảnh báo:</b> 3 lệnh liên tiếp thua — cân nhắc giảm lot hoặc bỏ qua' if consec_loss else ''

        # M15 confluence — da tinh ben trong analyze(), lay tu result
        m15_dir   = r.get('m15_dir')
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

        # Win rate + Expectancy — chi tinh ket qua tu LOGIC_VERSION tro di
        results_all = state.get('results', [])
        wr_line = ''
        versioned_r = [x for x in results_all if x.get('date', '') >= LOGIC_VERSION]
        if len(versioned_r) >= 5:
            n_total  = len(versioned_r)
            n_win    = sum(1 for x in versioned_r if x['correct'])
            wr_pct   = n_win / n_total * 100

            # Expectancy (chi tinh neu da co du lieu pip)
            pip_data = [x for x in versioned_r if 'pips' in x]
            exp_line = ''
            if len(pip_data) >= 5:
                wins_pip   = [x['pips'] for x in pip_data if x['correct']]
                losses_pip = [x['pips'] for x in pip_data if not x['correct']]
                avg_w = sum(wins_pip)   / len(wins_pip)   if wins_pip   else 0.0
                avg_l = sum(losses_pip) / len(losses_pip) if losses_pip else 0.0
                wr_r  = len(wins_pip) / len(pip_data)
                exp   = wr_r * avg_w + (1 - wr_r) * avg_l   # avg_l am nen + la dung
                sign  = '+' if exp >= 0 else ''
                exp_line = (f' | ⚡ Exp: {sign}{exp:.1f}p/lệnh'
                            f' (W: +{avg_w:.0f}p L: {avg_l:.0f}p, {len(pip_data)} lệnh)')

            pair_r = [x for x in versioned_r if x.get('sym') == sym and x.get('signal') == r['signal']]
            pair_wr_str = ''
            if len(pair_r) >= 3:
                pair_wr = sum(1 for x in pair_r if x['correct']) / len(pair_r) * 100
                pair_wr_str = f' | {sym} {r["signal"]}: {pair_wr:.0f}% ({len(pair_r)}L)'
            wr_line = f'📈 Win rate: {wr_pct:.0f}% ({n_total} lệnh từ {LOGIC_VERSION}){exp_line}{pair_wr_str}'

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

        vote_bar = '|'.join(r['vote_lbls']) + f'  ({r["vote_count"]}/6 đồng thuận)'

        # MTF summary line — W1 + D1 + H4
        mtf = r.get('mtf', {})
        sig_tf = 'BULL' if r['signal'] == 'BUY' else 'BEAR'
        def _tf_icon(tf_dir):
            return '✅' if tf_dir == sig_tf else ('⚠️' if tf_dir == 'NEUTRAL' else '❌')
        w1_icon = _tf_icon(mtf.get('w1_dir', 'NEUTRAL'))
        d1_icon = _tf_icon(mtf.get('d1_dir', 'NEUTRAL'))
        h4_icon = _tf_icon(mtf.get('h4_dir', 'NEUTRAL'))
        mtf_line = (f'📐 MTF: W1 {w1_icon}{mtf.get("w1_dir","?")}({mtf.get("w1_score",0):+.2f}) '
                    f'| D1 {d1_icon}{mtf.get("d1_dir","?")}({mtf.get("d1_score",0):+.2f}) '
                    f'| H4 {h4_icon}{mtf.get("h4_dir","?")}({mtf.get("h4_score",0):+.2f})')
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

        # Trendline line (Bab 5)
        tl_line = ''
        tl_d = r.get('trendline', {})
        if tl_d.get('tl_vote') and tl_d['tl_vote'] != 0:
            tl_icon = '📈' if tl_d['tl_vote'] > 0 else '📉'
            tl_touches = tl_d.get('up_touches', 0) if tl_d['tl_vote'] > 0 else tl_d.get('dn_touches', 0)
            tl_line = f'{tl_icon} Trendline: {tl_d["label"]} ({tl_touches} lần chạm)'

        # Dow Theory line (Bab 17)
        dow_line = ''
        dow_d = r.get('dow', {})
        if dow_d.get('structure') and dow_d['structure'] != 'NEUTRAL':
            dow_icon = '🔺' if dow_d['score'] > 0 else '🔻'
            bonus_str = f' | bonus {dow_d["bonus"]:+d}%' if dow_d.get('bonus') else ''
            dow_line = f'{dow_icon} Dow Theory: {dow_d["structure"]} (score={dow_d["score"]:.1f}{bonus_str})'

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

        # LIMIT mode: SL/TP/R:R/lot tinh tu gia limit (SL van o muc cau truc cu)
        entry_ref = r['limit_entry'] if r.get('entry_mode') == 'LIMIT' else r['price']
        sl_raw  = abs(entry_ref - r['sl'])
        tp1_raw = abs(r['tp']   - entry_ref)
        tp2_raw = abs(r['tp2']  - entry_ref)
        rr1_disp = round(tp1_raw / sl_raw, 1) if sl_raw > 0 else r['rr1']
        rr2_disp = round(tp2_raw / sl_raw, 1) if sl_raw > 0 else r['rr2']

        sl_pips_val  = price_to_pips(sym, sl_raw)
        tp1_pips_val = price_to_pips(sym, tp1_raw)
        tp2_pips_val = price_to_pips(sym, tp2_raw)

        sl_usd  = round(sl_pips_val  * LOT_SIZE * 10, 2)
        tp1_usd = round(tp1_pips_val * LOT_SIZE * 10, 2)
        tp2_usd = round(tp2_pips_val * LOT_SIZE * 10, 2)

        sl_pips_str = f'${sl_raw:.2f}' if sym in ('XAU/USD', 'WTI/USD') else f'{sl_pips_val:.1f} pips'

        # ATR-normalized lot recommendation: risk 1% account equity
        rec_lot = None
        if ACCOUNT_SIZE > 0 and sl_usd > 0:
            risk_amount = ACCOUNT_SIZE * 0.01
            rec_lot = round(risk_amount / (sl_usd / LOT_SIZE), 2)
            rec_lot = max(rec_lot, 0.01)  # floor micro lot

        msg_parts = [
            *(['⚠️ <b>THAM KHẢO — KHÔNG PHẢI LỆNH</b>',
               'Hệ Phân tích chưa có edge kiểm chứng (CI ôm 0). Đang thu thập dữ liệu; '
               'đừng vào lệnh theo tín hiệu này. (PA vàng vẫn là hệ chính.)',
               ''] if VOTING_MODE != 'live' else []),
            f'{emoji} <b>{sym} — {direction}</b> | {conf}% tin cậy',
            f'<code>{bar}</code>  {conf_10}/10',
            '',
            f'🗳 {vote_bar}',
            f'📊 H1: {r["phase"]} | {r["regime"]} (H={r["hurst"]:.2f} | ADX={r["adx"]:.0f} | {r.get("history_bars", 0)} bars)',
            mtf_line,
            *(([sr_line, '']) if sr_line else ['']),
            *(([fib_line, '']) if fib_line else []),
            *(([tl_line]) if tl_line else []),
            *(([dow_line, '']) if dow_line else (([''] if tl_line else []))),
            (f'📍 Entry: ⏳ <b>LIMIT @ {fmt_price(sym, entry_ref)}</b> — giá đã chạy '
             f'{r["ext_atr"]}×ATR từ EMA20, KHÔNG đuổi; treo lệnh chờ hồi'
             if r.get('entry_mode') == 'LIMIT' else f'📍 Entry: {entry_zone}'),
            f'🛑 SL:  {fmt_price(sym, r["sl"])}  ({sl_pips_str} / -${sl_usd:.2f})',
            f'✅ TP1: {fmt_price(sym, r["tp"])} (R:R 1:{rr1_disp} / +${tp1_usd:.2f})',
            f'✅ TP2: {fmt_price(sym, r["tp2"])} (R:R 1:{rr2_disp} / +${tp2_usd:.2f})',
            *(([f'📐 Lot đề xuất: {rec_lot} lot  (1% rủi ro / ${ACCOUNT_SIZE:.0f} vốn)']) if rec_lot else []),
            '',
            ('🚫 Hủy lệnh chờ nếu sau 12h chưa khớp, hoặc giá chạm TP1 trước khi khớp'
             if r.get('entry_mode') == 'LIMIT' else
             ('🚫 Bỏ qua nếu giá đã vượt: ' + fmt_price(sym, r['chase_limit'])
              if r['signal'] == 'BUY'
              else '🚫 Bỏ qua nếu giá đã thủng: ' + fmt_price(sym, r['chase_limit']))),
            '⚠️ Vô hiệu nếu: ' + inval_text,
            *(([r['exh_warn']]) if r.get('exh_warn') else []),
            '',
            '💡 Lý do:',
            reason,
            '',
            '🔍 Bằng chứng PA/Vol:',
            pa_vol,
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
        if corr_warning:
            msg_parts.append(corr_warning)
        if cb_warning:
            msg_parts.append(cb_warning)
        msg = '\n'.join(msg_parts)

        sym_key = sym.replace('/', '')
        ts_key  = int(now.timestamp())
        # Info mode: KHONG nut "Da vao lenh" (khong moi vao lenh). Live mode moi co keyboard.
        keyboard = None
        if VOTING_MODE == 'live':
            keyboard = [[
                {'text': '✅ Đã vào lệnh', 'callback_data': f'confirm_yes_{sym_key}_{ts_key}'},
                {'text': '❌ Bỏ qua',      'callback_data': f'confirm_no_{sym_key}_{ts_key}'},
            ]]
        result = send_telegram(msg, keyboard=keyboard)
        if result.get('ok'):
            msg_id = result.get('result', {}).get('message_id')
            state[key] = now.timestamp()
            state[key + '|entry'] = r['price']   # cho anti-pyramid guard [LOC 5b]
            _log.info(f'[{sym}] SENT {r["signal"]} conf={conf}% mode={r.get("entry_mode","MARKET")} '
                      f'entry={entry_ref:.5f} ext={r.get("ext_atr", 0)} sl={r["sl"]:.5f} tp={r["tp"]:.5f}')
            if 'pending_validations' not in state:
                state['pending_validations'] = []
            state['pending_validations'].append({
                'sym':            sym,
                'signal':         r['signal'],
                'entry_price':    r['price'],
                'entry_mode':     r.get('entry_mode', 'MARKET'),
                'limit_entry':    r.get('limit_entry'),
                'sl':             r['sl'],
                'tp':             r['tp'],
                'sent_at':        now.timestamp(),
                'message_id':     msg_id,
                'indicators':     r['indicators'],
                'regime':         r['regime'],
                'hurst':          r['hurst'],
                'conf':           conf,
                'aligned':        r['aligned'],
                'consensus':      r['consensus'],
                'entry_confirmed': None,
                'cb_key':         f'{sym_key}_{ts_key}',
            })
            sent += 1
            print(f'  -> Telegram OK | theo doi den khi cham TP/SL | {r["vote_count"]}/6 phieu | conf={conf}%')
        else:
            print(f'  -> Loi Telegram: {result}')

        time.sleep(1)

    save_state(state)

    # XAU Price Action: da tach sang gold_pa_bot.py (he doc lap, chay sau
    # buoc nay trong cung workflow — xem .github/workflows/main.yml)

    # Gold Macro Outlook — ban tin vi mo hang ngay (thong tin, khong phai lenh)
    try:
        send_gold_outlook(state, now)
    except Exception as e:
        print(f'  [OUTLOOK] Loi: {e}')
    save_state(state)

    # Bao cao tuan (moi 7 ngay) — gui qua Telegram neu du du lieu
    last_rpt  = state.get('last_weekly_report', 0)
    days_since = (now.timestamp() - last_rpt) / 86400
    if days_since >= 7 and len(state.get('results', [])) >= 5:
        print('\n=== Bao cao hieu suat tuan ===')
        send_weekly_report(state)
        save_state(state)

    print('\n=== Luu lich su gia ===')
    save_price_history()
    print(f'\n=== Hoan thanh. Da gui {sent} tin hieu moi ===')

if __name__ == '__main__':
    main()
