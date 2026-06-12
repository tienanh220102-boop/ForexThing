# -*- coding: utf-8 -*-
"""Chart renderer cho he Gold PA — ve nen H1 + vung S/R + level + setup.

Thiet ke (12/06/2026):
- KHONG import gold_pa_bot o module level (gold_pa_bot se import nguoc lai
  module nay de gui anh qua Telegram — tranh circular import).
- render_chart() la pure function: nhan bars/levels/signals tu caller.
- Nen khong co gia open trong price_history → open xap xi = close nen truoc
  (dung quy uoc san co cua he: _avg_body trong gold_pa_bot).
- CLI: python chart_render.py [n_bars] → ve chart hien tai ra data/chart_xau.png
"""
import os
import datetime as _dt

import matplotlib
matplotlib.use('Agg')          # khong can display — chay duoc tren Actions
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(_ROOT, 'data', 'chart_xau.png')

UP_COLOR   = '#26a69a'
DOWN_COLOR = '#ef5350'


def _ema_series(closes, n=20):
    out = []
    e = closes[0]
    k = 2.0 / (n + 1)
    for c in closes:
        e = e + k * (c - e)
        out.append(e)
    return out


def render_chart(bars, levels=None, signals=None, out_path=DEFAULT_OUT,
                 n_bars=130, atr_val=None, title='XAU/USD — H1', note=''):
    """Ve nen H1 + vung S/R + tin hieu. Tra ve out_path, hoac None neu loi.

    bars    : list {'t','c','h','l'} (price_history format, da sort theo t)
    levels  : list level dict — ho tro 3 loai:
              {'type':'S'/'R', 'touches': n}            → S/R H4 (band xanh/do)
              {'is_psych': True, 'strength': n}         → tam ly (dash xam)
              {'is_range': True, 'touches': n}          → bien range 48h (cam)
    signals : list {'ts','dir','entry','sl','tp1','tp2','setup',
                    'entry_type'?, 'outcome'?} — ve entry/SL/TP + marker
    """
    if not bars or len(bars) < 10:
        return None
    view = bars[-n_bars:]
    ts   = [b['t'] for b in view]
    cl   = [b['c'] for b in view]
    hi   = [b['h'] for b in view]
    lo   = [b['l'] for b in view]
    # open xap xi = close truoc; nen dau tien cua view lay tu nen lien truoc no
    prev_c = bars[-n_bars - 1]['c'] if len(bars) > n_bars else cl[0]
    op = [prev_c] + cl[:-1]

    y_min, y_max = min(lo), max(hi)
    pad = (y_max - y_min) * 0.04
    if atr_val is None:
        atr_val = sum(h - l for h, l in zip(hi[-14:], lo[-14:])) / 14

    fig, ax = plt.subplots(figsize=(14, 7.5), dpi=110)
    x = list(range(len(view)))

    # ── Vung S/R (ve truoc de nen nam tren) ──
    half_band = 0.12 * atr_val
    for lv in (levels or []):
        p = lv['price']
        if not (y_min - pad <= p <= y_max + pad):
            continue
        strength = lv.get('touches') or lv.get('strength', 2)
        if lv.get('is_psych'):
            ax.axhline(p, color='#9e9e9e', lw=0.9, ls='--', alpha=0.55, zorder=1)
            ax.annotate(f'Psych {p:,.0f} (s{strength})', xy=(0.4, p),
                        fontsize=7.5, color='#757575', va='bottom')
        elif lv.get('is_range'):
            ax.axhline(p, color='#ff9800', lw=1.4, ls='-.', alpha=0.85, zorder=1)
            ax.annotate(f'Range 48h {p:,.1f} ({strength} cham)', xy=(0.4, p),
                        fontsize=8, color='#e65100', va='bottom', fontweight='bold')
        else:
            is_res = lv.get('type') == 'R'
            color  = '#c62828' if is_res else '#1565c0'
            alpha  = min(0.10 + 0.06 * strength, 0.38)
            ax.add_patch(Rectangle((x[0], p - half_band), x[-1] - x[0] + 2,
                                   2 * half_band, facecolor=color, alpha=alpha,
                                   edgecolor='none', zorder=1))
            ax.annotate(f'{"KC" if is_res else "HT"} H4 {p:,.1f} ({strength} cham)',
                        xy=(0.4, p), fontsize=8, va='center', fontweight='bold',
                        color=color)

    # ── Nen ──
    for i in range(len(view)):
        c_up = cl[i] >= op[i]
        col  = UP_COLOR if c_up else DOWN_COLOR
        ax.vlines(i, lo[i], hi[i], color=col, lw=0.9, zorder=3)
        body_lo, body_hi = min(op[i], cl[i]), max(op[i], cl[i])
        if body_hi - body_lo < 1e-9:
            body_hi = body_lo + (y_max - y_min) * 0.0005
        ax.add_patch(Rectangle((i - 0.33, body_lo), 0.66, body_hi - body_lo,
                               facecolor=col, edgecolor=col, zorder=3))

    # ── EMA20 ──
    ema_full = _ema_series([b['c'] for b in bars], 20)[-len(view):]
    ax.plot(x, ema_full, color='#7b1fa2', lw=1.3, alpha=0.9,
            label='EMA20 H1', zorder=4)

    # ── Tin hieu ──
    for s in (signals or []):
        # +3h: signal moi phat co the tre hon nen dong cuoi (stale guard cho 2.5h)
        if not ts[0] <= s.get('ts', 0) <= ts[-1] + 3 * 3600:
            continue
        xi = min(range(len(ts)), key=lambda i: abs(ts[i] - s['ts']))
        is_buy = s['dir'] == 'BUY'
        mcol   = UP_COLOR if is_buy else DOWN_COLOR
        marker = '^' if is_buy else 'v'
        my     = lo[xi] - 0.6 * atr_val if is_buy else hi[xi] + 0.6 * atr_val
        ax.scatter([xi], [my], marker=marker, s=130, color=mcol, zorder=6,
                   edgecolors='black', linewidths=0.6)
        outcome = s.get('outcome') or 'OPEN'
        etype   = ' LIMIT' if s.get('entry_type') == 'limit' else ''
        ax.annotate(f"{s['dir']}{etype} {s.get('setup','')}\n"
                    f"@{s['entry']:,.1f} → {outcome}",
                    xy=(xi, my), fontsize=8, fontweight='bold', color=mcol,
                    va='top' if is_buy else 'bottom', ha='center',
                    xytext=(xi, my - 0.5 * atr_val if is_buy else my + 0.5 * atr_val))
        seg = range(xi, min(xi + 30, len(view)))
        ax.plot(list(seg), [s['entry']] * len(list(seg)), color=mcol, lw=1.0, ls=':', zorder=5)
        ax.plot(list(seg), [s['sl']] * len(list(seg)), color='#d32f2f', lw=1.0, ls=':', zorder=5)
        ax.plot(list(seg), [s['tp1']] * len(list(seg)), color='#2e7d32', lw=1.0, ls=':', zorder=5)
        if s.get('tp2'):
            ax.plot(list(seg), [s['tp2']] * len(list(seg)), color='#2e7d32',
                    lw=0.8, ls=':', alpha=0.6, zorder=5)

    # ── Truc & nhan ──
    tick_idx = list(range(0, len(view), max(1, len(view) // 10)))
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([_dt.datetime.fromtimestamp(ts[i], _dt.timezone.utc)
                        .strftime('%d/%m %Hh') for i in tick_idx],
                       fontsize=8, rotation=0)
    ax.set_xlim(-1, len(view) + 14)            # chua cho de nhan level ben phai
    ax.set_ylim(y_min - pad, y_max + pad)
    ax.grid(True, alpha=0.18)
    ax.legend(loc='upper left', fontsize=8)
    now_lbl = _dt.datetime.now(_dt.timezone.utc).strftime('%d/%m/%Y %H:%M UTC')
    full_title = f'{title}  |  {now_lbl}'
    if note:
        full_title += f'\n{note}'
    ax.set_title(full_title, fontsize=11, fontweight='bold')
    fig.tight_layout()

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def render_current_xau(n_bars=130, out_path=DEFAULT_OUT, with_history_signals=True):
    """Tien ich: ve chart XAU hien tai voi day du nhung gi he PA dang thay.
    Import gold_pa_bot tai day (lazy) de tranh circular import."""
    import json
    import gold_pa_bot as gp
    import forex_notifier as fx

    fx.load_price_history()
    bars = fx._price_history.get('XAU/USD', {}).get('bars', [])
    if len(bars) < 60:
        print('[CHART] Khong du du lieu')
        return None
    closes = [b['c'] for b in bars]
    highs  = [b['h'] for b in bars]
    lows   = [b['l'] for b in bars]
    atr_v  = fx.atr(highs, lows, closes)
    levels = gp._gold_levels(closes, highs, lows, atr_v)

    signals = []
    if with_history_signals and os.path.exists(gp.STATE_FILE):
        try:
            with open(gp.STATE_FILE, encoding='utf-8') as f:
                signals = json.load(f).get('signals', [])
        except Exception:
            pass

    note = ''
    exh = fx.exhaustion_state(bars)
    if exh:
        note = (f"Exhaustion: dir={exh['dir']} | D1 RSI={exh['d1_rsi']} | "
                f"move 5 phien pctl={exh['pctl']:.0f}% | at_extreme={exh['at_extreme']}")
    return render_chart(bars, levels=levels, signals=signals,
                        out_path=out_path, n_bars=n_bars, atr_val=atr_v, note=note)


if __name__ == '__main__':
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 130
    p = render_current_xau(n_bars=n)
    # Khong print duong dan: path co dau tieng Viet → cp1252 crash tren Windows
    print('[CHART] Saved: data/chart_xau.png' if p else '[CHART] FAILED')
