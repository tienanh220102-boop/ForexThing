#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backtest & Validation Script
Tai du lieu lich su, chay lai thuat toan, do chinh xac tung cong thuc.
"""
import numpy as np
import yfinance as yf
from collections import defaultdict

SYMBOLS = {
    'EUR/USD': 'EURUSD=X', 'GBP/USD': 'GBPUSD=X', 'USD/JPY': 'USDJPY=X',
    'USD/CAD': 'USDCAD=X', 'AUD/USD': 'AUDUSD=X', 'XAU/USD': 'GC=F',
    'GBP/JPY': 'GBPJPY=X', 'EUR/JPY': 'EURJPY=X',
}
SCORE_THRESHOLD = 0.40
WARMUP          = 60    # Can it nhat 60 nen de tinh chi bao
CHECK_HOURS     = [2, 4, 24]

# ── Cac ham indicator (copy y chang tu forex_notifier.py) ─────
def ema(values, period):
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v*k + e*(1-k)
    return e

def rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
    ag = sum(max(d,0) for d in deltas[:period]) / period
    al = sum(max(-d,0) for d in deltas[:period]) / period
    for d in deltas[period:]:
        ag = (ag*(period-1)+max(d,0)) / period
        al = (al*(period-1)+max(-d,0)) / period
    return 100.0 if al == 0 else 100 - 100/(1+ag/al)

def macd(closes):
    if len(closes) < 35: return 0.0
    k12, k26, k9 = 2.0/13, 2.0/27, 2.0/10
    e12 = sum(closes[:12])/12
    for v in closes[12:26]: e12 = v*k12+e12*(1-k12)
    e26 = sum(closes[:26])/26
    mv = [e12-e26]
    for v in closes[26:]:
        e12 = v*k12+e12*(1-k12); e26 = v*k26+e26*(1-k26)
        mv.append(e12-e26)
    if len(mv) < 9: return 0.0
    sig = sum(mv[:9])/9
    for v in mv[9:]: sig = v*k9+sig*(1-k9)
    ref = max(abs(sig), abs(closes[-1])*0.0001, 1e-10)
    return float(np.clip((mv[-1]-sig)/ref, -1, 1))

def bollinger(closes, period=20):
    if len(closes) < period: return closes[-1], closes[-1], closes[-1]
    w=closes[-period:]; mid=sum(w)/period
    std=(sum((x-mid)**2 for x in w)/period)**0.5
    return mid+2*std, mid, mid-2*std

def momentum(closes, n=5):
    if len(closes)<n+1: return 0.0
    gains=sum(1 for i in range(-n,0) if closes[i]>closes[i-1])
    return (gains-(n-gains))/n

def hurst_exponent(closes):
    # [FIX #6] Dung 200 nen (khop voi forex_notifier.py) thay vi 50 nen cu
    # 50 nen: dao dong lon, ket qua phan loai TREND/RANGE/NEUTRAL khac live
    # 200 nen: on dinh ve mat thong ke (Peters 1994), max_lag mo rong len 50
    n = min(len(closes), 200)
    if n < 50: return 0.5
    ts = np.array(closes[-n:], dtype=float)
    max_lag = min(n // 4, 50)
    lags = list(range(2, max_lag))
    if len(lags) < 5: return 0.5
    tau = np.array([np.std(ts[lag:] - ts[:-lag]) for lag in lags])
    valid = tau > 1e-10
    if valid.sum() < 5: return 0.5
    poly = np.polyfit(np.log(np.array(lags)[valid]), np.log(tau[valid]), 1)
    return float(np.clip(poly[0], 0, 1))

def fourier_signal(closes):
    if len(closes)<32: return 0.0
    n=min(128,len(closes))
    ts=np.array(closes[-n:],dtype=float)
    trend=np.linspace(ts[0],ts[-1],n)
    windowed=(ts-trend)*np.hanning(n)
    fft=np.fft.rfft(windowed); power=np.abs(fft)**2
    threshold=np.percentile(power,80)
    cycle=np.fft.irfft(np.where(power>=threshold,fft,0),n=n)
    std=np.std(cycle)
    if std<1e-12: return 0.0
    return float(np.clip(-cycle[-1]/(std*2),-1,1))

# ── Tinh diem cho 1 period ────────────────────────────────────
def compute_signal(closes):
    p=closes[-1]
    r=rsi(closes)
    rsi_s=(1.0 if r<=30 else 0.5 if r<=40 else -1.0 if r>=70 else -0.5 if r>=60 else 0.0)
    e20=ema(closes,20); e50=ema(closes,50)
    ema_s=(1.0 if p>e20>e50 else -1.0 if p<e20<e50 else 0.4 if p>e20 else -0.4 if p<e20 else 0.0)
    mac_s=macd(closes)
    upper,_,lower=bollinger(closes)
    bb_s=(1.0 if p<lower else -1.0 if p>upper else 0.0)
    mom_s=momentum(closes)
    fft_s=fourier_signal(closes)
    H=hurst_exponent(closes)
    regime='TREND' if H>0.55 else ('RANGE' if H<0.45 else 'NEUTRAL')

    # Fixed weights (khong dung dynamic trong backtest cho don gian)
    w=[0.20,0.30,0.30,0.10,0.10]
    base=rsi_s*w[0]+ema_s*w[1]+mac_s*w[2]+bb_s*w[3]+mom_s*w[4]
    score=base*0.80+fft_s*0.10

    if regime=='TREND':
        if (score>0 and ema_s>0) or (score<0 and ema_s<0): score*=1.20
    elif regime=='RANGE':
        if (score>0 and rsi_s>0) or (score<0 and rsi_s<0): score*=1.10
        else: score*=0.80

    score=float(np.clip(score,-2,2))
    if   score>=SCORE_THRESHOLD: direction='BUY'
    elif score<=-SCORE_THRESHOLD: direction='SELL'
    else: direction=None

    return {
        'dir': direction, 'score': score,
        'rsi_s': rsi_s, 'ema_s': ema_s, 'mac_s': mac_s,
        'bb_s': bb_s, 'mom_s': mom_s, 'fft_s': fft_s,
        'H': H, 'regime': regime, 'rsi_val': r
    }

# ── Backtest cho 1 symbol ─────────────────────────────────────
def backtest_symbol(sym, yf_sym):
    print(f'  Downloading {sym}...', end=' ', flush=True)
    try:
        df = yf.Ticker(yf_sym).history(period='180d', interval='1h')
        if df is None or len(df) < 100:
            print('Khong du data')
            return None
        closes = list(df['Close'].dropna())
        print(f'{len(closes)} nen 1h')
    except Exception as e:
        print(f'Loi: {e}')
        return None

    results = []
    max_h   = max(CHECK_HOURS)

    for i in range(WARMUP, len(closes) - max_h):
        sig = compute_signal(closes[:i+1])
        if sig['dir'] is None:
            continue

        outcomes = {}
        for h in CHECK_HOURS:
            if i+h >= len(closes):
                continue
            fut_price = closes[i+h]
            cur_price = closes[i]
            diff = fut_price - cur_price if sig['dir']=='BUY' else cur_price - fut_price
            outcomes[h] = diff > 0

        if outcomes:
            results.append({**sig, 'outcomes': outcomes, 'price': closes[i]})

    return results

# ── Phan tich thong ke ────────────────────────────────────────
def analyze_results(sym, results):
    if not results:
        return

    total = len(results)
    buys  = [r for r in results if r['dir']=='BUY']
    sells = [r for r in results if r['dir']=='SELL']

    print(f'\n{"="*55}')
    print(f'  {sym}  |  Tong tin hieu: {total} (BUY={len(buys)}, SELL={len(sells)})')
    print(f'{"="*55}')

    # 1. Do chinh xac theo moc thoi gian
    print('\n[1] Do chinh xac theo moc thoi gian:')
    for h in CHECK_HOURS:
        subset = [r for r in results if h in r['outcomes']]
        if not subset: continue
        acc = sum(1 for r in subset if r['outcomes'][h]) / len(subset)
        buy_acc  = (sum(1 for r in buys  if h in r['outcomes'] and r['outcomes'][h]) /
                    max(len([r for r in buys  if h in r['outcomes']]),1))
        sell_acc = (sum(1 for r in sells if h in r['outcomes'] and r['outcomes'][h]) /
                    max(len([r for r in sells if h in r['outcomes']]),1))
        verdict = 'TOT' if acc>=0.55 else ('TRUNG BINH' if acc>=0.50 else 'YEU')
        print(f'   +{h:>2}h: {acc*100:5.1f}%  '
              f'(BUY={buy_acc*100:.1f}% | SELL={sell_acc*100:.1f}%)  [{verdict}]')

    # 2. Do chinh xac theo do manh tin hieu (score band)
    print('\n[2] Do chinh xac +4h theo do manh tin hieu:')
    bands = [(0.40,0.55,'Yeu'), (0.55,0.70,'Trung binh'), (0.70,1.00,'Manh'), (1.00,2.01,'Rat manh')]
    for lo, hi, label in bands:
        subset = [r for r in results if lo<=abs(r['score'])<hi and 4 in r['outcomes']]
        if len(subset) < 3: continue
        acc = sum(1 for r in subset if r['outcomes'][4]) / len(subset)
        print(f'   score [{lo:.2f}-{hi:.2f}) {label:12s}: {acc*100:5.1f}%  (n={len(subset)})')

    # 3. Do chinh xac theo regime
    print('\n[3] Do chinh xac +4h theo Hurst Regime:')
    for regime in ['TREND','RANGE','NEUTRAL']:
        subset=[r for r in results if r['regime']==regime and 4 in r['outcomes']]
        if len(subset)<3: continue
        acc=sum(1 for r in subset if r['outcomes'][4])/len(subset)
        avg_H=np.mean([r['H'] for r in subset])
        print(f'   {regime:8s}: {acc*100:5.1f}%  (n={len(subset)}, avg Hurst={avg_H:.3f})')

    # 4. Correlation tung indicator voi ket qua thuc
    print('\n[4] Tuong quan (correlation) tung indicator vs ket qua +4h:')
    ind_keys = [('rsi_s','RSI'), ('ema_s','EMA'), ('mac_s','MACD'),
                ('bb_s','BolBand'), ('mom_s','Momentum'), ('fft_s','Fourier')]
    subset_4h = [r for r in results if 4 in r['outcomes']]
    if subset_4h:
        y = np.array([1 if r['outcomes'][4] else -1 for r in subset_4h])
        for key, label in ind_keys:
            x = np.array([r[key] for r in subset_4h])
            if np.std(x)<1e-10: continue
            # Correlation theo dung huong (sign match)
            sign_x = np.sign(x); sign_pred = np.sign(np.array([r['score'] for r in subset_4h]))
            x_dir = np.where(sign_pred>0, x, -x)  # flip nếu SELL signal
            corr = np.corrcoef(x_dir, (y+1)/2)[0,1]
            impact = 'MANH' if abs(corr)>0.15 else ('TRUNG BINH' if abs(corr)>0.08 else 'YEU')
            print(f'   {label:12s}: corr={corr:+.3f}  [{impact}]')

    # 5. Phat hien pattern: khi nao chinh xac cao nhat
    print('\n[5] Pattern chinh xac cao nhat (+4h):')
    # RSI extreme + EMA cung chieu
    p1 = [r for r in subset_4h if abs(r['rsi_s'])>=0.5 and
          (r['rsi_s']>0)==(r['ema_s']>0)]
    if p1:
        acc1=sum(1 for r in p1 if r['outcomes'][4])/len(p1)
        print(f'   RSI extreme + EMA cung chieu: {acc1*100:.1f}%  (n={len(p1)})')

    # Score manh + MACD cung chieu
    p2 = [r for r in subset_4h if abs(r['score'])>0.6 and
          (r['score']>0)==(r['mac_s']>0)]
    if p2:
        acc2=sum(1 for r in p2 if r['outcomes'][4])/len(p2)
        print(f'   Score manh (>0.6) + MACD cung chieu: {acc2*100:.1f}%  (n={len(p2)})')

    # TREND + EMA manh
    p3 = [r for r in subset_4h if r['regime']=='TREND' and abs(r['ema_s'])>=1.0]
    if p3:
        acc3=sum(1 for r in p3 if r['outcomes'][4])/len(p3)
        print(f'   TREND regime + EMA manh (1.0): {acc3*100:.1f}%  (n={len(p3)})')

    # Fourier cung chieu voi tin hieu
    p4 = [r for r in subset_4h if (r['score']>0)==(r['fft_s']>0.1) and abs(r['fft_s'])>0.1]
    if p4:
        acc4=sum(1 for r in p4 if r['outcomes'][4])/len(p4)
        print(f'   Fourier xac nhan cung chieu: {acc4*100:.1f}%  (n={len(p4)})')

    # RSI EMA TRAI CHIEU (vung nguy hiem)
    p5 = [r for r in subset_4h if abs(r['rsi_s'])>=0.5 and
          (r['rsi_s']>0)!=(r['ema_s']>0)]
    if p5:
        acc5=sum(1 for r in p5 if r['outcomes'][4])/len(p5)
        verdict = '!! NGUY HIEM' if acc5<0.45 else 'OK'
        print(f'   RSI vs EMA TRAI CHIEU: {acc5*100:.1f}%  (n={len(p5)}) {verdict}')

    return {
        'sym': sym, 'total': total, 'results': results
    }

# ── Tong ket toan bo ─────────────────────────────────────────
def global_summary(all_results):
    flat = []
    for r_list in all_results:
        flat.extend(r_list['results'])

    print(f'\n{"#"*55}')
    print(f'  TONG KET TOAN BO ({len(all_results)} cap tien te)')
    print(f'  Tong tin hieu backtest: {len(flat)}')
    print(f'{"#"*55}')

    if not flat: return

    # Accuracy tong the
    print('\n[TONG] Do chinh xac trung binh:')
    for h in CHECK_HOURS:
        sub = [r for r in flat if h in r['outcomes']]
        if not sub: continue
        acc = sum(1 for r in sub if r['outcomes'][h]) / len(sub)
        print(f'   +{h:>2}h: {acc*100:.1f}%  (n={len(sub)})')

    # Tim nguong score toi uu
    print('\n[TONG] Tim nguong score toi uu cho +4h:')
    print('   [!] IN-SAMPLE — best-of-8 tren cung tap = data snooping. KHONG ap vao live.')
    best_thresh, best_acc, best_n = 0.40, 0, 0
    for thresh in [0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
        sub = [r for r in flat if abs(r['score'])>=thresh and 4 in r['outcomes']]
        if len(sub) < 10: continue
        acc = sum(1 for r in sub if r['outcomes'][4]) / len(sub)
        print(f'   score >= {thresh:.2f}: {acc*100:.1f}%  (n={len(sub)})')
        if acc > best_acc:
            best_acc, best_thresh, best_n = acc, thresh, len(sub)
    print(f'\n   => Nguong TOI UU: score >= {best_thresh:.2f}  '
          f'(chinh xac {best_acc*100:.1f}%, n={best_n})')

    # Indicator nao quan trong nhat
    print('\n[TONG] Trong so indicator theo ket qua thuc (tuong quan voi +4h):')
    print('   [!] IN-SAMPLE — trong so suy tu correlation tren cung tap. Chi tham khao,')
    print('       KHONG nhet thang vao live (se overfit). Kiem chung bang OOS truoc.')
    sub4 = [r for r in flat if 4 in r['outcomes']]
    y = np.array([(r['score']>0) == r['outcomes'][4] for r in sub4], dtype=float)
    ind_keys = [('rsi_s','RSI'), ('ema_s','EMA'), ('mac_s','MACD'),
                ('bb_s','BolBand'), ('mom_s','Momentum'), ('fft_s','Fourier')]
    corrs = []
    for key, label in ind_keys:
        x = np.abs(np.array([r[key] for r in sub4]))
        if np.std(x) < 1e-10:
            corrs.append((label, 0))
            continue
        c = np.corrcoef(x, y)[0,1]
        corrs.append((label, 0 if np.isnan(c) else c))
    corrs.sort(key=lambda x: -abs(x[1]))
    total_c = sum(abs(c) for _,c in corrs) or 1
    for label, c in corrs:
        suggested_w = abs(c)/total_c
        print(f'   {label:12s}: corr={c:+.3f}  => trong so de xuat: {suggested_w*100:.1f}%')

    # Phat hien loi chinh
    print('\n[TONG] Phat hien cac loi va thieu sot trong thuat toan:')
    issues = []

    # Kiem tra RSI-EMA conflict
    conflict = [r for r in sub4 if abs(r['rsi_s'])>=0.5 and (r['rsi_s']>0)!=(r['ema_s']>0)]
    agree    = [r for r in sub4 if abs(r['rsi_s'])>=0.5 and (r['rsi_s']>0)==(r['ema_s']>0)]
    if conflict and agree:
        acc_c = sum(1 for r in conflict if r['outcomes'][4]) / len(conflict)
        acc_a = sum(1 for r in agree    if r['outcomes'][4]) / len(agree)
        gap   = acc_a - acc_c
        if gap > 0.05:
            issues.append(f'RSI-EMA XUNG DOT: chinh xac -{gap*100:.1f}% khi trai chieu '
                         f'({acc_c*100:.1f}% vs {acc_a*100:.1f}%)')

    # Kiem tra score yeu
    weak  = [r for r in sub4 if 0.40<=abs(r['score'])<0.50]
    strong= [r for r in sub4 if abs(r['score'])>=0.60]
    if weak and strong:
        acc_w = sum(1 for r in weak   if r['outcomes'][4]) / len(weak)
        acc_s = sum(1 for r in strong if r['outcomes'][4]) / len(strong)
        if acc_s - acc_w > 0.05:
            issues.append(f'NGUONG QUA THAP: score 0.40-0.50 chi dat {acc_w*100:.1f}% '
                         f'(nen nang len >= 0.50)')

    # Kiem tra Fourier
    fft_no  = [r for r in sub4 if abs(r['fft_s'])<0.1]
    fft_yes = [r for r in sub4 if abs(r['fft_s'])>=0.1 and (r['score']>0)==(r['fft_s']>0)]
    if fft_no and fft_yes:
        acc_fn = sum(1 for r in fft_no  if r['outcomes'][4]) / len(fft_no)
        acc_fy = sum(1 for r in fft_yes if r['outcomes'][4]) / len(fft_yes)
        if acc_fy - acc_fn < 0.02:
            issues.append(f'FOURIER IT HIEU QUA: chi cai thien {(acc_fy-acc_fn)*100:.1f}% '
                         f'({acc_fn*100:.1f}% → {acc_fy*100:.1f}%)')

    # Kiem tra Regime NEUTRAL
    neutral_sub = [r for r in sub4 if r['regime']=='NEUTRAL']
    if neutral_sub:
        acc_n = sum(1 for r in neutral_sub if r['outcomes'][4]) / len(neutral_sub)
        if acc_n < 0.50:
            issues.append(f'REGIME NEUTRAL QUA NHIEU NHIEU: chinh xac {acc_n*100:.1f}% '
                         f'(< 50%, nen bo qua)')

    if issues:
        for i, issue in enumerate(issues, 1):
            print(f'   [{i}] {issue}')
    else:
        print('   Khong phat hien van de nghiem trong.')

# ── Main ─────────────────────────────────────────────────────
def main():
    print('=' * 55)
    print('  FOREX ALGORITHM BACKTEST & VALIDATION')
    print('  Du lieu: 180 ngay, timeframe 1h')
    print('  Thuat toan: v3 (Hurst + Fourier + RSI/EMA/MACD/BB/MOM)')
    print('=' * 55)
    print('  !!! CANH BAO — CHI MANG TINH MO TA (IN-SAMPLE) !!!')
    print('  - Moi "nguong toi uu" / "trong so de xuat" duoi day deu duoc do')
    print('    TREN CHINH DU LIEU NAY = data snooping. KHONG dung de chinh live')
    print('    (se overfit vao nhieu). Muon danh gia that: dung walk-forward')
    print('    out-of-sample trong backtest.py, hoac forward test live.')
    print('  - Logic o day la WEIGHTED-SCORE v3, KHAC he live (da chuyen sang')
    print('    VOTING). Ket luan o day khong mo ta chinh xac he dang chay.')
    print('=' * 55)

    all_results = []
    for sym, yf_sym in SYMBOLS.items():
        data = backtest_symbol(sym, yf_sym)
        if data:
            result = analyze_results(sym, data)
            if result:
                all_results.append(result)

    global_summary(all_results)
    print('\nHoan thanh backtest.')

if __name__ == '__main__':
    main()
