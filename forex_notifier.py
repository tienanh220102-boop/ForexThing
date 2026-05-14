#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Forex Signal Notifier - Chay tren GitHub Actions moi 30 phut
Quet tin hieu, gui Telegram, va xac nhan ket qua sau 2 tieng.
"""
import json, os, time
from datetime import datetime, timezone

import requests
import yfinance as yf

# ── Cau hinh (doc tu GitHub Secrets) ──────────────────────────
TELEGRAM_TOKEN    = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT     = os.environ.get('TELEGRAM_CHAT',  '')
COOLDOWN_HOURS    = 4    # Khong gui lai cung cap/huong trong 4 tieng
VALIDATION_HOURS  = 2    # Xac nhan ket qua sau 2 tieng

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
    # Wilder's smoothed RSI (chinh xac hon simple average)
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas[:period]]
    losses = [max(-d, 0) for d in deltas[:period]]
    ag = sum(gains) / period
    al = sum(losses) / period
    # Wilder smoothing cho phan con lai
    for d in deltas[period:]:
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)

def macd(closes):
    # FIX: Tinh MACD dung chuan, khong dung subsampling
    if len(closes) < 35:
        return 0.0
    k12, k26, k9 = 2.0/13, 2.0/27, 2.0/10

    # Khoi tao EMA12 tai index 11, EMA26 tai index 25
    e12 = sum(closes[:12]) / 12
    for v in closes[12:26]:
        e12 = v*k12 + e12*(1-k12)
    e26 = sum(closes[:26]) / 26

    # Tinh MACD line cho tung diem tu index 25 tro di
    macd_vals = [e12 - e26]
    for v in closes[26:]:
        e12 = v*k12 + e12*(1-k12)
        e26 = v*k26 + e26*(1-k26)
        macd_vals.append(e12 - e26)

    if len(macd_vals) < 9:
        return 0.0

    # Signal line: EMA(9) chinh xac cua MACD line
    sig = sum(macd_vals[:9]) / 9
    for v in macd_vals[9:]:
        sig = v*k9 + sig*(1-k9)

    histogram = macd_vals[-1] - sig
    ref = max(abs(sig), abs(closes[-1]) * 0.0001, 1e-10)
    return max(-1.0, min(1.0, histogram / ref))

def bollinger(closes, period=20):
    if len(closes) < period:
        return closes[-1], closes[-1], closes[-1]
    w   = closes[-period:]
    mid = sum(w) / period
    std = (sum((x - mid)**2 for x in w) / period) ** 0.5
    return mid + 2*std, mid, mid - 2*std

def atr(highs, lows, closes, period=14):
    # Average True Range: do bien dong thi truong
    if len(closes) < period + 1:
        return 0.0
    trs = [max(highs[i]-lows[i],
               abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    if len(trs) < period:
        return 0.0
    # Wilder smoothing
    a = sum(trs[:period]) / period
    for v in trs[period:]:
        a = (a*(period-1) + v) / period
    return a

def momentum(closes, n=5):
    # Ty le nen tang gia trong n nen cuoi: +1.0 (tat ca tang) .. -1.0 (tat ca giam)
    if len(closes) < n + 1:
        return 0.0
    gains  = sum(1 for i in range(-n, 0) if closes[i] > closes[i-1])
    losses = n - gains
    return (gains - losses) / n

# ── Phan tich tin hieu (cai tien) ─────────────────────────────
def analyze(sym, yf_sym):
    try:
        df = yf.Ticker(yf_sym).history(period='60d', interval='1h')
        if df is None or len(df) < 60:
            return None
        closes = list(df['Close'].dropna())
        highs  = list(df['High'].dropna())
        lows   = list(df['Low'].dropna())
        n = min(len(closes), len(highs), len(lows))
        closes, highs, lows = closes[:n], highs[:n], lows[:n]
        if n < 60:
            return None
        price = closes[-1]

        # [LOC] ATR filter: loai thi truong qua phang (noise > signal)
        atr_val = atr(highs, lows, closes)
        if atr_val < price * 0.00015:
            return None

        # RSI (Wilder's smoothed)
        r = rsi(closes)
        if   r <= 30: rsi_sc = 1.0
        elif r <= 40: rsi_sc = 0.5
        elif r >= 70: rsi_sc = -1.0
        elif r >= 60: rsi_sc = -0.5
        else:         rsi_sc = 0.0

        # EMA trend
        e20 = ema(closes, 20)
        e50 = ema(closes, 50)
        if   price > e20 > e50: ema_sc = 1.0
        elif price < e20 < e50: ema_sc = -1.0
        elif price > e20:       ema_sc = 0.4
        elif price < e20:       ema_sc = -0.4
        else:                   ema_sc = 0.0

        # MACD (da fix tinh dung chuan)
        macd_sc = macd(closes)

        # Bollinger Bands
        upper, _, lower = bollinger(closes)
        if   price < lower: bb_sc = 1.0
        elif price > upper: bb_sc = -1.0
        else:               bb_sc = 0.0

        # Momentum nen (5 nen cuoi)
        mom_sc = momentum(closes, n=5)

        # [PHAT HUY] Trong so: them momentum, giam trong so RSI
        # EMA + MACD (trend) + momentum la backbone
        # RSI + BB (mean-reversion) la xac nhan phu
        score = rsi_sc*0.20 + ema_sc*0.30 + macd_sc*0.30 + bb_sc*0.10 + mom_sc*0.10

        # [FIX] Nang nguong tu 0.30 len 0.40 de loc tin hieu yeu
        if   score >= 0.40:  signal = 'BUY'
        elif score <= -0.40: signal = 'SELL'
        else: return None

        # [MOI] Kiem tra xung dot RSI vs EMA: neu cung chieu thi tang do tin cay
        rsi_ema_agree = (rsi_sc > 0 and ema_sc > 0) or (rsi_sc < 0 and ema_sc < 0)

        return {
            'sym': sym, 'signal': signal,
            'score': round(score, 3), 'price': price, 'rsi': round(r, 1),
            'atr': round(atr_val, 6),
            'indicators': {
                'rsi': rsi_sc, 'ema': ema_sc, 'macd': round(macd_sc, 2),
                'bb': bb_sc, 'mom': round(mom_sc, 2),
            },
            'consensus': rsi_ema_agree,
        }
    except Exception as e:
        print(f'  [{sym}] Loi: {e}')
        return None

# ── Lay gia hien tai (dung cho xac nhan) ──────────────────────
def fetch_current_price(yf_sym):
    try:
        df = yf.Ticker(yf_sym).history(period='1d', interval='5m')
        if df is None or len(df) == 0:
            return None
        return float(df['Close'].iloc[-1])
    except Exception:
        return None

# ── Dinh dang gia ─────────────────────────────────────────────
def fmt_price(sym, price):
    if 'JPY' in sym:                                      return f'{price:.3f}'
    if sym in ('XAG/USD',):                               return f'{price:.3f}'
    if sym in ('XAU/USD', 'UKOIL/USD', 'USOIL/USD'):     return f'{price:.2f}'
    return f'{price:.5f}'

# ── Dinh dang chi bao (icon) ──────────────────────────────────
def _ind_icon(v):
    if v > 0.1:  return '⬆'
    if v < -0.1: return '⬇'
    return '➡'

# ── Trang thai ────────────────────────────────────────────────
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

# ── Xac nhan ket qua cac lenh cu ──────────────────────────────
def run_validations(state, now):
    pending   = state.get('pending_validations', [])
    remaining = []

    for v in pending:
        if now.timestamp() < v['validate_at']:
            remaining.append(v)
            continue

        sym    = v['sym']
        yf_sym = SYMBOLS.get(sym)
        if not yf_sym:
            continue

        print(f'Xac nhan lenh {v["signal"]} {sym}...', end=' ', flush=True)
        current = fetch_current_price(yf_sym)

        if current is None:
            print('Khong lay duoc gia, thu lai lan sau')
            remaining.append(v)
            continue

        entry   = v['entry_price']
        signal  = v['signal']
        diff    = current - entry if signal == 'BUY' else entry - current
        pct     = abs(diff / entry) * 100
        correct = diff > 0

        if correct:
            verdict_emoji = '✅'
            verdict       = 'DUNG HUONG'
            move_text     = f'Tang {pct:.3f}%' if signal == 'BUY' else f'Giam {pct:.3f}%'
        else:
            verdict_emoji = '❌'
            verdict       = 'SAI HUONG'
            move_text     = f'Giam {pct:.3f}%' if signal == 'BUY' else f'Tang {pct:.3f}%'

        sent_time = datetime.fromtimestamp(v['sent_at'], tz=timezone.utc)
        # Hien thi chi bao nao da dong gop vao lenh nay
        inds = v.get('indicators', {})
        ind_line = (f"RSI{_ind_icon(inds.get('rsi',0))} "
                    f"EMA{_ind_icon(inds.get('ema',0))} "
                    f"MACD{_ind_icon(inds.get('macd',0))} "
                    f"BB{_ind_icon(inds.get('bb',0))} "
                    f"MOM{_ind_icon(inds.get('mom',0))}")
        consensus_note = ' (RSI+EMA dong thuan)' if v.get('consensus') else ''

        msg = '\n'.join([
            f'{verdict_emoji} <b>Xac nhan lenh — {verdict}</b>',
            '',
            f'📈 Cap: <b>{sym}</b>',
            f'📌 Lenh: <b>{signal}</b> @ {fmt_price(sym, entry)}',
            f'💰 Gia sau {VALIDATION_HOURS}h: <b>{fmt_price(sym, current)}</b>',
            f'📊 Bien dong: <b>{move_text}</b>',
            f'🔍 Chi bao: {ind_line}{consensus_note}',
            '',
            f'⏱ Lenh dat luc: {sent_time.strftime("%d/%m/%Y %H:%M UTC")}',
            f'⏱ Xac nhan luc: {now.strftime("%d/%m/%Y %H:%M UTC")}',
        ])

        result = send_telegram(msg)
        if result.get('ok'):
            print(f'{verdict} ✓')
        else:
            print(f'Loi gui Telegram: {result}')

        time.sleep(1)

    state['pending_validations'] = remaining

# ── Main ──────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print('TELEGRAM_TOKEN hoac TELEGRAM_CHAT chua duoc dat trong Secrets!')
        return

    now   = datetime.now(timezone.utc)
    state = load_state()
    sent  = 0

    # --- Buoc 1: Xac nhan cac lenh cu truoc ---
    print(f'=== Kiem tra xac nhan lenh cu ===')
    run_validations(state, now)

    # --- Buoc 2: Quet tin hieu moi ---
    print(f'\n=== Forex Scan {now.strftime("%Y-%m-%d %H:%M UTC")} ===')

    for sym, yf_sym in SYMBOLS.items():
        print(f'Phan tich {sym}...', end=' ', flush=True)
        r = analyze(sym, yf_sym)

        if not r:
            print('NEUTRAL / khong du du lieu / bien dong qua thap')
            time.sleep(0.5)
            continue

        consensus_tag = ' [RSI+EMA DONG THUAN]' if r['consensus'] else ''
        print(f'{r["signal"]} (score={r["score"]}, rsi={r["rsi"]}){consensus_tag}')

        key     = f'{sym}|{r["signal"]}'
        elapsed = (now.timestamp() - state.get(key, 0)) / 3600

        if elapsed < COOLDOWN_HOURS:
            print(f'  -> Cooldown ({elapsed:.1f}h / {COOLDOWN_HOURS}h), bo qua')
            time.sleep(0.5)
            continue

        conf     = int((0.45 + abs(r['score']) * 0.40) * 100)
        strength = int(abs(r['score']) * 100)
        emoji    = '🟢' if r['signal'] == 'BUY' else '🔴'
        rsi_note = 'Qua ban' if r['rsi'] <= 35 else ('Qua mua' if r['rsi'] >= 65 else 'Trung tinh')
        t_str    = now.strftime('%d/%m/%Y %H:%M UTC')

        inds = r['indicators']
        ind_line = (f"RSI{_ind_icon(inds['rsi'])} "
                    f"EMA{_ind_icon(inds['ema'])} "
                    f"MACD{_ind_icon(inds['macd'])} "
                    f"BB{_ind_icon(inds['bb'])} "
                    f"MOM{_ind_icon(inds['mom'])}")
        consensus_line = '✔ RSI va EMA cung chieu' if r['consensus'] else '⚠ RSI va EMA trai chieu'

        msg = '\n'.join([
            f'{emoji} <b>Tin hieu FOREX — {r["signal"]}</b>',
            '',
            f'📈 Cap: <b>{sym}</b>',
            f'💰 Gia: <b>{fmt_price(sym, r["price"])}</b>',
            f'📊 RSI: {r["rsi"]} ({rsi_note})',
            f'🔍 Chi bao: {ind_line}',
            f'📋 {consensus_line}',
            f'💪 Suc manh tin hieu: <b>{strength}%</b>',
            f'🎯 Do tin cay: ~<b>{conf}%</b>',
            f'🔔 Ket qua se duoc xac nhan sau {VALIDATION_HOURS} tieng',
            '',
            '⚠ Phan tich ky thuat, khong phai tu van tai chinh',
            f'⏱ {t_str}',
        ])

        result = send_telegram(msg)
        if result.get('ok'):
            state[key] = now.timestamp()
            if 'pending_validations' not in state:
                state['pending_validations'] = []
            state['pending_validations'].append({
                'sym':         sym,
                'signal':      r['signal'],
                'entry_price': r['price'],
                'sent_at':     now.timestamp(),
                'validate_at': now.timestamp() + VALIDATION_HOURS * 3600,
                'indicators':  r['indicators'],
                'consensus':   r['consensus'],
            })
            sent += 1
            print(f'  -> Telegram sent OK')
        else:
            print(f'  -> Loi Telegram: {result}')

        time.sleep(1)

    save_state(state)
    print(f'\n=== Hoan thanh. Da gui {sent} tin hieu moi ===')

if __name__ == '__main__':
    main()
