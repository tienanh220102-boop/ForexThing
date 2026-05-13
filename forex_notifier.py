#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Forex Signal Notifier - Chay tren GitHub Actions moi 30 phut
Quet tin hieu va gui Telegram khi co tin hieu dau tu manh.
"""
import json, os, time
from datetime import datetime, timezone

import requests
import yfinance as yf

# ── Cau hinh (doc tu GitHub Secrets) ──────────────────────────
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT',  '')
COOLDOWN_HOURS = 4   # Khong gui lai cung cap/huong trong 4 tieng

STATE_FILE = 'last_signals.json'

# ── Danh sach cap tien te ─────────────────────────────────────
SYMBOLS = {
    'EUR/USD': 'EURUSD=X', 'GBP/USD': 'GBPUSD=X', 'USD/JPY': 'USDJPY=X',
    'USD/CHF': 'USDCHF=X', 'AUD/USD': 'AUDUSD=X', 'USD/CAD': 'USDCAD=X',
    'NZD/USD': 'NZDUSD=X', 'EUR/GBP': 'EURGBP=X', 'EUR/JPY': 'EURJPY=X',
    'GBP/JPY': 'GBPJPY=X', 'XAU/USD': 'GC=F',     'XAG/USD': 'SI=F',
    'UKOIL/USD': 'BZ=F',   'USOIL/USD': 'CL=F',
}

# ── Chi bao ky thuat ──────────────────────────────────────────
def ema(values, period):
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    ag, al = sum(gains) / period, sum(losses) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

def macd(closes):
    if len(closes) < 35:
        return 0.0
    # Tinh nhanh MACD histogram cuoi
    e12, e26 = ema(closes, 12), ema(closes, 26)
    line = e12 - e26
    # Signal line: EMA9 cua chuoi MACD
    series = []
    step = max(1, len(closes) // 50)
    for i in range(26, len(closes), step):
        series.append(ema(closes[:i+1], 12) - ema(closes[:i+1], 26))
    if len(series) < 9:
        return 0.0
    sig = ema(series, 9)
    ref = abs(sig) if sig != 0 else 1e-10
    return max(-1.0, min(1.0, (line - sig) / ref))

def bollinger(closes, period=20):
    if len(closes) < period:
        return closes[-1], closes[-1], closes[-1]
    w   = closes[-period:]
    mid = sum(w) / period
    std = (sum((x - mid)**2 for x in w) / period) ** 0.5
    return mid + 2*std, mid, mid - 2*std

# ── Phan tich tin hieu ────────────────────────────────────────
def analyze(sym, yf_sym):
    try:
        df = yf.Ticker(yf_sym).history(period='60d', interval='1h')
        if df is None or len(df) < 60:
            return None
        closes = list(df['Close'].dropna())
        if len(closes) < 60:
            return None
        price = closes[-1]

        # RSI score
        r = rsi(closes)
        if   r <= 30: rsi_sc = 1.0
        elif r <= 40: rsi_sc = 0.5
        elif r >= 70: rsi_sc = -1.0
        elif r >= 60: rsi_sc = -0.5
        else:         rsi_sc = 0.0

        # EMA trend score
        e20 = ema(closes, 20)
        e50 = ema(closes, 50)
        if   price > e20 > e50: ema_sc = 1.0
        elif price < e20 < e50: ema_sc = -1.0
        elif price > e20:       ema_sc = 0.4
        elif price < e20:       ema_sc = -0.4
        else:                   ema_sc = 0.0

        # MACD score
        macd_sc = macd(closes)

        # Bollinger Bands score
        upper, _, lower = bollinger(closes)
        if   price < lower: bb_sc = 1.0
        elif price > upper: bb_sc = -1.0
        else:               bb_sc = 0.0

        # Composite (trong so giong WatchlistMgr)
        score = rsi_sc*0.25 + ema_sc*0.35 + macd_sc*0.30 + bb_sc*0.10

        if   score >= 0.30: signal = 'BUY'
        elif score <= -0.30: signal = 'SELL'
        else: return None

        return {
            'sym': sym, 'signal': signal,
            'score': round(score, 3), 'price': price, 'rsi': round(r, 1)
        }
    except Exception as e:
        print(f'  [{sym}] Loi: {e}')
        return None

# ── Dinh dang gia ─────────────────────────────────────────────
def fmt_price(sym, price):
    if 'JPY' in sym:            return f'{price:.3f}'
    if sym in ('XAG/USD',):     return f'{price:.3f}'
    if sym in ('XAU/USD', 'UKOIL/USD', 'USOIL/USD'): return f'{price:.2f}'
    return f'{price:.5f}'

# ── Trang thai (tranh spam) ───────────────────────────────────
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

# ── Gui Telegram ──────────────────────────────────────────────
def send_telegram(msg):
    url  = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    resp = requests.post(url, json={
        'chat_id': TELEGRAM_CHAT, 'text': msg, 'parse_mode': 'HTML'
    }, timeout=10)
    return resp.json()

# ── Main ──────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print('TELEGRAM_TOKEN hoac TELEGRAM_CHAT chua duoc dat trong Secrets!')
        return

    now     = datetime.now(timezone.utc)
    state   = load_state()
    sent    = 0

    print(f'=== Forex Scan {now.strftime("%Y-%m-%d %H:%M UTC")} ===')

    for sym, yf_sym in SYMBOLS.items():
        print(f'Phan tich {sym}...', end=' ', flush=True)
        r = analyze(sym, yf_sym)

        if not r:
            print('NEUTRAL / khong du du lieu')
            time.sleep(0.5)
            continue

        print(f'{r["signal"]} (score={r["score"]}, rsi={r["rsi"]})')

        key     = f'{sym}|{r["signal"]}'
        elapsed = (now.timestamp() - state.get(key, 0)) / 3600

        if elapsed < COOLDOWN_HOURS:
            print(f'  -> Cooldown ({elapsed:.1f}h / {COOLDOWN_HOURS}h), bo qua')
            time.sleep(0.5)
            continue

        conf  = int((0.50 + abs(r['score']) * 0.35) * 100)
        emoji = '🟢' if r['signal'] == 'BUY' else '🔴'
        t_str = now.strftime('%d/%m/%Y %H:%M UTC')

        msg = '\n'.join([
            f'{emoji} <b>Tin hieu FOREX — {r["signal"]}</b>',
            '',
            f'📈 Cap: <b>{sym}</b>',
            f'💰 Gia: <b>{fmt_price(sym, r["price"])}</b>',
            f'📊 RSI: {r["rsi"]} | Score: {r["score"]}',
            f'🎯 Do tin cay: ~<b>{conf}%</b>',
            '',
            '⚠ Phan tich ky thuat, khong phai tu van tai chinh',
            f'⏱ {t_str}',
        ])

        result = send_telegram(msg)
        if result.get('ok'):
            state[key] = now.timestamp()
            sent += 1
            print(f'  -> Telegram sent ✓')
        else:
            print(f'  -> Loi Telegram: {result}')

        time.sleep(1)

    save_state(state)
    print(f'=== Hoan thanh. Da gui {sent} thong bao ===')

if __name__ == '__main__':
    main()
