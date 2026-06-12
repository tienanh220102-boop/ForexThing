#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gold PA Bot — He thong tin hieu XAU/USD doc lap, thuan Price Action.

Chay SONG SONG voi forex_notifier.py (vote system) — khong block lan nhau,
state rieng, log rieng, thong ke rieng. Muc dich: phat LENH (entry/SL/TP/lot)
de user vao tay, khong phai bao cao.

Kien truc 3 module doc lap (theo blueprint da thong nhat 11/06/2026):
  [INGESTION]   — tai su dung fetch_ohlcv + price_history cua forex_notifier
                  (Twelve Data uu tien, yfinance fallback) + DXY rieng
  [BRAIN]       — 4 setup Price Action + 3 bo loc sinh tu (news/session/ATR)
                  Muon doi Rule-based sang AI: chi thay ruot cac ham detect_*
  [BROADCASTER] — send_telegram tai su dung; format message rieng 🥇

4 insight XAU/USD da so hoa:
  1. Liquidity sweep:   KHONG bao gio mua raw breakout — chi vao khi co
                        sweep + reclaim (wick >= 2x than, dong nguoc lai)
  2. Round numbers:     proximity filter — canh bao + keo TP1 ve truoc
                        "buc tuong" so tron (2300/2350/2400...)
  3. Velocity/nen:      3 nen than lon lien tiep = momentum regime →
                        khoa reversal nguoc chieu, mo setup pullback EMA20
  4. DXY divergence:    DXY HH ma vang khong LL → noi luc phe mua (confluence)

Bo loc sinh tu (moi signal phai qua het):
  Tram 1 — News:    check_calendar HARD (high-impact ±[-15,+60]p) → khoa Brain
  Tram 2 — Session: Asia (22-05 UTC) chi cho reversal; EU/My (06-20 UTC) cho
                    het; 21 UTC rollover → OFF
  Tram 3 — ATR SL:  SL dong = max(structure + 0.25*ATR, 1.5*ATR), cap 2.5*ATR
                    (qua cap → bo keo, khong duoc nong SL)
"""
import json, os, sys, time, logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

# Tai su dung INGESTION + BROADCASTER + primitives tu he thong chinh.
# Import forex_notifier chi chay phan setup module-level (logging/dir) —
# main() cua no co __main__ guard nen khong bi kich hoat.
import forex_notifier as fx

_ROOT      = Path(__file__).parent
STATE_FILE = str(_ROOT / 'gold_pa_state.json')
_LOG_FILE  = str(_ROOT / 'data' / 'gold_pa.log')

SYM, YF_SYM = 'XAU/USD', 'GC=F'
VN_TZ       = timezone(timedelta(hours=7))

# ── Tham so Brain ─────────────────────────────────────────────
SL_ATR_FLOOR    = 1.5    # SL toi thieu 1.5x ATR (Tram 3 — chong quet rau)
SL_ATR_CAP      = 2.5    # SL qua 2.5x ATR → bo keo
SL_BUFFER_ATR   = 0.25   # dem them sau structure
TP1_R           = 1.5    # TP1 = 1.5R
TP2_R           = 2.5    # TP2 = 2.5R (chart pattern dung measured move neu xa hon)
WICK_BODY_RATIO = 2.0    # sweep: wick >= 2x than (insight 1)
SWEEP_MIN_RANGE = 0.75   # nen sweep phai >= 0.75 ATR (loc nen ti hon)
# Anti-FOMO (12/06/2026): bot chay 15p/lan — nen sweep dong xong gia thuong
# DA bat xa khoi level truoc khi bot quet. Vao market luc do = entry xau +
# SL (tai structure) bi keo rong. Gia chay qua nguong → treo LIMIT tai retest level.
SWEEP_RETEST_ATR = 0.10  # limit dat cach level 0.10 ATR ve phia reclaim
SWEEP_CHASE_ATR  = 0.35  # gia hien tai vuot diem retest qua nguong nay → dung limit
LIMIT_EXPIRY_H   = 6     # limit khong khop sau 6h → huy (NOFILL, khong tinh WR)
# Triet ly PA (user, 12/06/2026): muc dich chinh la BAN dinh / MUA day, ke ca
# dinh/day trong khung sideway. Keo nguy hiem KHONG block — giam rui ro:
# lot 0.5% (thay 1%) + cap SL chat hon. SL van theo structure (thu ngan SL
# vao trong structure se bi noise quet truoc khi thesis sai).
DANGER_SL_CAP    = 1.8   # keo nguy hiem: structure doi SL > 1.8 ATR → bo keo
DANGER_RISK_PCT  = 0.005 # keo nguy hiem: rui ro 0.5% von thay vi 1%
RANGE_WINDOW_H   = 48    # dinh/day khung sideway: cuc tri 48h lam level sweep
RANGE_TOUCH_ATR  = 0.25  # bar cach cuc tri <= 0.25 ATR thi tinh la 1 lan cham
MOM_BODY_RATIO  = 1.5    # momentum: 3 nen than > 1.5x trung binh (insight 3)
MOM_CLOSE_PCT   = 0.30   # momentum: dong cua trong 30% cuc tri cua nen
MOM_EXPIRY_H    = 12     # momentum regime het han sau 12h khong tai xac nhan
COMPRESS_ATR    = 1.5    # nen: range 12 nen < 1.5x ATR
WALL_NEAR_ATR   = 0.5    # canh bao tuong so tron trong 0.5x ATR (insight 2)
WALL_BLOCK_ATR  = 0.30   # tuong qua sat (< 0.3 ATR truoc mat) → tru 1 sao
COOLDOWN_H      = 4      # 1 setup+huong: toi da 1 lenh / 4h
DAILY_CAP       = 3      # toi da 3 lenh / ngay (chong choppy day spam)
MIN_STARS       = 3      # chat luong toi thieu de gui
TIMEOUT_DAYS    = 5      # keo khong cham SL/TP sau 5 ngay → het han (EXP)
MIN_BARS        = 120    # du lieu H1 toi thieu

_SETUP_NAMES = {
    'sweep_reclaim': 'Sweep & Reclaim (quét thanh khoản + rút râu)',
    'momentum_pullback': 'Momentum Pullback (hồi về EMA20 trong trend mạnh)',
    'compression_breakout': 'Compression Breakout (nén rồi bùng nổ)',
    'double_top': 'Hai đỉnh (Double Top) — H4',
    'double_bottom': 'Hai đáy (Double Bottom) — H4',
    'hs_top': 'Vai-Đầu-Vai (H&S) — H4',
    'hs_inv': 'Vai-Đầu-Vai ngược (iH&S) — H4',
}

# ── Logging rieng (Quy tac An toan Code: khong dung print cho su kien) ──
log = logging.getLogger('gold_pa')
log.setLevel(logging.INFO)
log.propagate = False          # khong lan vao decisions.log cua forex
_h = logging.FileHandler(_LOG_FILE, encoding='utf-8')
_h.setFormatter(logging.Formatter('%(asctime)s %(message)s',
                                  datefmt='%Y-%m-%d %H:%M:%S UTC'))
log.addHandler(_h)
_MAX_LOG_LINES = 20000
try:
    if os.path.exists(_LOG_FILE):
        with open(_LOG_FILE, encoding='utf-8', errors='replace') as _f:
            _lines = _f.readlines()
        if len(_lines) > _MAX_LOG_LINES:
            with open(_LOG_FILE, 'w', encoding='utf-8') as _f:
                _f.writelines(_lines[-_MAX_LOG_LINES:])
except Exception:
    pass


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


# ── Tram 2: Session filter ───────────────────────────────────
def get_session(now):
    """Phien theo UTC. Returns ('ASIAN'|'EU_US'|'OFF', label_vn).
    Asia 22-05 UTC (05-13 GMT+7) thanh khoan mong → chi reversal.
    EU/My 06-20 UTC (13-04 GMT+7) dong tien lon → cho het setup.
    21 UTC = rollover spread rong → OFF."""
    h = now.hour
    if h == 21:
        return 'OFF', 'Rollover'
    if h >= 22 or h <= 5:
        return 'ASIAN', 'Á'
    return 'EU_US', 'Âu-Mỹ'


_SESSION_ALLOWED = {
    'ASIAN': {'sweep_reclaim'},                      # chi reversal
    'EU_US': {'sweep_reclaim', 'momentum_pullback',  # cho het
              'compression_breakout',
              'double_top', 'double_bottom', 'hs_top', 'hs_inv'},
    'OFF':   set(),
}


# ── INGESTION: DXY cho divergence (insight 4) ────────────────
def fetch_dxy_bars():
    """DXY H1 7 ngay tu yfinance — chi de so sanh cau truc, khong persist."""
    try:
        df = yf.Ticker('DX-Y.NYB').history(period='7d', interval='1h')
        if df is None or len(df) < 50:
            return None
        return {'h': list(df['High'].dropna()), 'l': list(df['Low'].dropna()),
                'c': list(df['Close'].dropna())}
    except Exception as e:
        log.info(f'DXY_FETCH_FAIL {e}')
        return None


def dxy_divergence(xau_h, xau_l, dxy):
    """So sanh cau truc dinh/day 24h gan nhat vs 24h truoc do (insight 4).
    DXY HH ma vang KHONG LL → phe mua vang manh (BULL). Nguoc lai BEAR."""
    if not dxy or len(dxy['h']) < 48 or len(xau_h) < 48:
        return None, ''
    dxy_hh = max(dxy['h'][-24:]) > max(dxy['h'][-48:-24])
    dxy_ll = min(dxy['l'][-24:]) < min(dxy['l'][-48:-24])
    xau_ll = min(xau_l[-24:])   < min(xau_l[-48:-24])
    xau_hh = max(xau_h[-24:])   > max(xau_h[-48:-24])
    if dxy_hh and not xau_ll:
        return 'BULL', 'DXY tạo đỉnh cao hơn nhưng vàng KHÔNG tạo đáy thấp hơn — nội lực phe mua mạnh'
    if dxy_ll and not xau_hh:
        return 'BEAR', 'DXY tạo đáy thấp hơn nhưng vàng KHÔNG tạo đỉnh cao hơn — nội lực phe bán mạnh'
    return None, ''


# ── BRAIN: cong cu chung ─────────────────────────────────────
def _avg_body(closes, n=24):
    """Than nen trung binh (open xap xi = close truoc — thi truong lien tuc)."""
    bodies = [abs(closes[i] - closes[i-1]) for i in range(len(closes)-n, len(closes))]
    return sum(bodies) / len(bodies) if bodies else 0.0


def detect_momentum(closes, highs, lows):
    """Insight 3: 3 nen H1 DONG lien tiep than > 1.5x trung binh, cung huong,
    dong cua trong 30% cuc tri → momentum regime. Returns 'BULL'|'BEAR'|None."""
    if len(closes) < 30:
        return None
    c, h, l = closes[:-1], highs[:-1], lows[:-1]   # bo nen dang hinh
    avg_b = _avg_body(c[:-3])
    if avg_b <= 0:
        return None
    dirs = []
    for i in (-3, -2, -1):
        body = c[i] - c[i-1]
        rng  = max(h[i] - l[i], 1e-9)
        if abs(body) < MOM_BODY_RATIO * avg_b:
            return None
        if body > 0 and (h[i] - c[i]) <= MOM_CLOSE_PCT * rng:
            dirs.append(1)
        elif body < 0 and (c[i] - l[i]) <= MOM_CLOSE_PCT * rng:
            dirs.append(-1)
        else:
            return None
    if all(d == 1 for d in dirs):
        return 'BULL'
    if all(d == -1 for d in dirs):
        return 'BEAR'
    return None


def _gold_levels(closes, highs, lows, atr_val=0.0):
    """Vung S/R cho sweep: H4 S/R (cluster pivot) + muc tam ly manh (insight 2)
    + dinh/day khung 48h (12/06/2026 — bat day/dinh trong sideway ma H4 S/R
    chua kip hinh thanh; strength = so lan cham nen tu dieu tiet: cuc tri
    1-cham (spike) bi grade() tru sao, bien range cham nhieu duoc cong)."""
    h4_c, h4_h, h4_l = fx.resample_to_h4(closes, highs, lows)
    levels = []
    if len(h4_c) >= 10:
        levels += fx.find_sr_levels(h4_h, h4_l, h4_c, lookback=60)
    levels += [p for p in fx.psychological_levels(closes[-1]) if p['strength'] >= 3]
    if atr_val > 0 and len(highs) >= RANGE_WINDOW_H:
        rh = max(highs[-RANGE_WINDOW_H:])
        rl = min(lows[-RANGE_WINDOW_H:])
        tol = RANGE_TOUCH_ATR * atr_val
        levels.append({'price': rh, 'is_range': True,
                       'touches': sum(1 for h in highs[-RANGE_WINDOW_H:] if rh - h <= tol)})
        levels.append({'price': rl, 'is_range': True,
                       'touches': sum(1 for l in lows[-RANGE_WINDOW_H:] if l - rl <= tol)})
    return levels


def detect_sweep_reclaim(closes, highs, lows, atr_val, levels):
    """Insight 1 — setup chu luc. Nen H1 DONG gan nhat dam xuyen level
    (quet thanh khoan) roi rut rau dong nguoc lai ben kia level.
    KHONG co logic 'close > khang cu → mua' o day — nguoc lai moi dung."""
    out = []
    if len(closes) < 5 or atr_val <= 0:
        return out
    o, c   = closes[-3], closes[-2]      # nen dong gan nhat (-1 dang hinh)
    hi, lo = highs[-2], lows[-2]
    body    = abs(c - o)
    rng     = max(hi - lo, 1e-9)
    up_wick = hi - max(o, c)
    dn_wick = min(o, c) - lo
    price   = closes[-1]
    if rng < SWEEP_MIN_RANGE * atr_val:
        return out

    for lv in levels:
        lp = lv['price']
        if abs(price - lp) > 1.2 * atr_val:
            continue
        is_psych = lv.get('is_psych', False)
        strength = lv.get('touches') or lv.get('strength', 2)
        lbl = (f'mức tâm lý {lp:,.0f}' if is_psych
               else f'đỉnh/đáy khung 48h {lp:,.1f} ({strength} chạm)' if lv.get('is_range')
               else f'S/R H4 {lp:,.1f} ({strength} chạm)')
        # BUY: quet xuong duoi level roi dong lai TREN level, rau duoi dai
        if lo < lp - 0.05 * atr_val and c > lp and \
           dn_wick >= WICK_BODY_RATIO * body:
            ideal = lp + SWEEP_RETEST_ATR * atr_val
            chase = price - ideal > SWEEP_CHASE_ATR * atr_val
            out.append({
                'setup': 'sweep_reclaim', 'dir': 'BUY',
                'entry': ideal if chase else price,
                'entry_type': 'limit' if chase else 'market',
                'structure': lo, 'level': lp, 'level_strength': strength,
                'reason': (f'Quét thanh khoản dưới {lbl} (râu dưới '
                           f'{dn_wick:,.1f}$ = {dn_wick/max(body,1e-9):.1f}× thân) '
                           f'rồi đóng cửa ngược lên trên — từ chối giá'),
            })
        # SELL: quet len tren level roi dong lai DUOI level, rau tren dai
        if hi > lp + 0.05 * atr_val and c < lp and \
           up_wick >= WICK_BODY_RATIO * body:
            ideal = lp - SWEEP_RETEST_ATR * atr_val
            chase = ideal - price > SWEEP_CHASE_ATR * atr_val
            out.append({
                'setup': 'sweep_reclaim', 'dir': 'SELL',
                'entry': ideal if chase else price,
                'entry_type': 'limit' if chase else 'market',
                'structure': hi, 'level': lp, 'level_strength': strength,
                'reason': (f'Quét thanh khoản trên {lbl} (râu trên '
                           f'{up_wick:,.1f}$ = {up_wick/max(body,1e-9):.1f}× thân) '
                           f'rồi đóng cửa ngược xuống dưới — từ chối giá'),
            })
    # 1 nen co the cham nhieu level — giu level manh nhat moi huong
    best = {}
    for p in out:
        k = p['dir']
        if k not in best or p['level_strength'] > best[k]['level_strength']:
            best[k] = p
    return list(best.values())


def detect_momentum_pullback(closes, highs, lows, atr_val, mom_dir):
    """Insight 3 (phan 2): trong momentum regime, KHONG bat dao chieu —
    cho gia hoi ve EMA20 H1 va co nen tu choi thuan huong → vao tiep trend."""
    if not mom_dir or len(closes) < 25 or atr_val <= 0:
        return []
    c, h, l = closes[:-1], highs[:-1], lows[:-1]
    e20   = fx.ema(c, 20)
    price = closes[-1]
    zone  = 0.3 * atr_val
    if mom_dir == 'BULL':
        touched  = l[-1] <= e20 + zone          # da hoi ve vung EMA20
        rejected = c[-1] > c[-2] and c[-1] > e20  # nen hoi phuc dong tren EMA
        if touched and rejected:
            return [{
                'setup': 'momentum_pullback', 'dir': 'BUY', 'entry': price,
                'structure': l[-1], 'level': e20,
                'reason': (f'Trend tăng mạnh (3 nến thân lớn) — giá hồi về '
                           f'EMA20 {e20:,.1f} và bật lên (đáy nến {l[-1]:,.1f})'),
            }]
    else:
        touched  = h[-1] >= e20 - zone
        rejected = c[-1] < c[-2] and c[-1] < e20
        if touched and rejected:
            return [{
                'setup': 'momentum_pullback', 'dir': 'SELL', 'entry': price,
                'structure': h[-1], 'level': e20,
                'reason': (f'Trend giảm mạnh (3 nến thân lớn) — giá hồi về '
                           f'EMA20 {e20:,.1f} và bị đẩy xuống (đỉnh nến {h[-1]:,.1f})'),
            }]
    return []


def detect_compression_breakout(closes, highs, lows, atr_val):
    """Insight 3 (phan 3): vang nen bien do hep roi bung no bang nen Marubozu.
    Yeu cau nen breakout DONG ngoai range voi than ap dao (khong phai wick) —
    phan biet voi pha vo gia (insight 1: wick xuyen roi rut = sweep, khong vao)."""
    if len(closes) < 20 or atr_val <= 0:
        return []
    c, h, l = closes[:-1], highs[:-1], lows[:-1]
    rng_hi = max(h[-13:-1])
    rng_lo = min(l[-13:-1])
    if (rng_hi - rng_lo) > COMPRESS_ATR * atr_val:
        return []                                  # khong co nen
    avg_b = _avg_body(c[:-1])
    body  = c[-1] - c[-2]
    full  = max(h[-1] - l[-1], 1e-9)
    if abs(body) < MOM_BODY_RATIO * avg_b or abs(body) < 0.7 * full:
        return []                                  # nen yeu / nhieu wick
    price = closes[-1]
    mid   = (rng_hi + rng_lo) / 2
    if body > 0 and c[-1] > rng_hi:
        return [{
            'setup': 'compression_breakout', 'dir': 'BUY', 'entry': price,
            'structure': mid, 'level': rng_hi,
            'reason': (f'Nén {12} nến trong {rng_hi-rng_lo:,.1f}$ rồi bùng nổ '
                       f'thân nến {abs(body):,.1f}$ (Marubozu) đóng trên {rng_hi:,.1f}'),
        }]
    if body < 0 and c[-1] < rng_lo:
        return [{
            'setup': 'compression_breakout', 'dir': 'SELL', 'entry': price,
            'structure': mid, 'level': rng_lo,
            'reason': (f'Nén {12} nến trong {rng_hi-rng_lo:,.1f}$ rồi bùng nổ '
                       f'thân nến {abs(body):,.1f}$ (Marubozu) đóng dưới {rng_lo:,.1f}'),
        }]
    return []


def detect_chart_patterns(closes, highs, lows):
    """Chart patterns H4 (Bulkowski top-tier) — tai su dung detector forex_notifier."""
    h4_c, h4_h, h4_l = fx.resample_to_h4(closes, highs, lows)
    if len(h4_c) < 30:
        return []
    out = []
    for p in (fx._detect_double_top_bottom(h4_c, h4_h, h4_l)
              + fx._detect_head_shoulders(h4_c, h4_h, h4_l)):
        out.append({
            'setup': p['code'], 'dir': p['dir'], 'entry': closes[-1],
            'structure': p['inval'], 'level': p['neckline'],
            'measured_move': p['target'],
            'reason': p['note'],
        })
    return out


# ── BRAIN: SL/TP dong (Tram 3) + tuong so tron (insight 2) ───
def build_order(p, atr_val, psych_levels):
    """Tinh SL/TP/lot. Tra ve None neu SL bat buoc qua rong (> cap) —
    thay vi nong SL, bo keo (quan ly rui ro truoc, keo sau)."""
    is_buy = p['dir'] == 'BUY'
    entry  = p['entry']

    struct_dist = abs(entry - p['structure']) + SL_BUFFER_ATR * atr_val
    sl_dist     = max(SL_ATR_FLOOR * atr_val, struct_dist)
    # Keo nguy hiem: cap SL chat hon — structure doi SL rong hon nua thi bo keo
    # (rui ro giam tu TIEN: lot 0.5%, khong thu ngan SL vao trong structure)
    sl_cap = DANGER_SL_CAP if p.get('danger') else SL_ATR_CAP
    if sl_dist > sl_cap * atr_val:
        return None
    sl  = entry - sl_dist if is_buy else entry + sl_dist
    tp1 = entry + TP1_R * sl_dist if is_buy else entry - TP1_R * sl_dist
    tp2 = entry + TP2_R * sl_dist if is_buy else entry - TP2_R * sl_dist
    # Chart pattern: TP2 = measured move neu xa hon TP1 (muc tieu cau truc)
    mm = p.get('measured_move')
    if mm and ((is_buy and mm > tp1) or (not is_buy and mm < tp1)):
        tp2 = mm

    # Insight 2 — tuong so tron giua entry va TP1: keo TP1 ve truoc tuong
    wall_warn = ''
    star_pen  = 0
    walls = [w for w in psych_levels if w.get('strength', 0) >= 4]
    for w in walls:
        wp = w['price']
        in_path = (entry < wp < tp1) if is_buy else (tp1 < wp < entry)
        if not in_path:
            continue
        dist_to_wall = abs(wp - entry)
        if dist_to_wall < WALL_BLOCK_ATR * atr_val:
            star_pen = 1
            wall_warn = (f'⚠️ Tường số tròn {wp:,.0f} chỉ cách entry '
                         f'{dist_to_wall:,.1f}$ — rủi ro tranh chấp cao')
        elif abs(wp - tp1) < WALL_NEAR_ATR * atr_val or wp < tp1:
            new_tp1 = wp - 0.15 * atr_val if is_buy else wp + 0.15 * atr_val
            if (is_buy and new_tp1 > entry) or (not is_buy and new_tp1 < entry):
                tp1 = new_tp1
                wall_warn = (f'🧲 TP1 kéo về {tp1:,.1f} — trước tường số tròn '
                             f'{wp:,.0f} (tổ chức hay đặt lệnh chờ tại đây)')
        break

    sl_pips  = fx.price_to_pips(SYM, sl_dist)
    tp1_pips = fx.price_to_pips(SYM, abs(tp1 - entry))
    tp2_pips = fx.price_to_pips(SYM, abs(tp2 - entry))
    sl_usd   = round(sl_pips  * fx.LOT_SIZE * 10, 2)
    tp1_usd  = round(tp1_pips * fx.LOT_SIZE * 10, 2)
    tp2_usd  = round(tp2_pips * fx.LOT_SIZE * 10, 2)
    rec_lot  = None
    risk_pct = DANGER_RISK_PCT if p.get('danger') else 0.01
    if fx.ACCOUNT_SIZE > 0 and sl_usd > 0:
        rec_lot = max(round((fx.ACCOUNT_SIZE * risk_pct) / (sl_usd / fx.LOT_SIZE), 2), 0.01)

    p.update({
        'sl': sl, 'tp1': tp1, 'tp2': tp2,
        'sl_dist_atr': round(sl_dist / atr_val, 2),
        'sl_usd': sl_usd, 'tp1_usd': tp1_usd, 'tp2_usd': tp2_usd,
        'rr1': round(abs(tp1 - entry) / sl_dist, 1),
        'rr2': round(abs(tp2 - entry) / sl_dist, 1),
        'rec_lot': rec_lot, 'wall_warn': wall_warn, 'star_pen': star_pen,
        'risk_pct': risk_pct,
    })
    return p


def grade(p, mom_dir, h4_dir, dxy_div, dxy_note):
    """Cham 1-5 sao theo confluence. Base 3 (setup hop le + qua het bo loc).

    Backtest 30 ngay (n=23, chi mang tinh dinh huong):
      - Sweep tai level manh (>=3 cham): 46% vs level yeu 29% → location la
        dieu kien song con, sweep level yeu phai -1 sao (can confluence khac)
      - Sweep thuan H4 20% vs nguoc H4 44% → sweep la keo MEAN-REVERSION,
        khong cong sao H4 cho sweep (chi cong cho setup trend-following)"""
    stars = 3 - p.get('star_pen', 0)
    conf  = [p['reason']]
    want  = 'BULL' if p['dir'] == 'BUY' else 'BEAR'

    if p['setup'] == 'sweep_reclaim':
        if p.get('level_strength', 0) >= 3:
            stars += 1
            conf.append(f'Level mạnh ({p["level_strength"]} lần xác nhận)')
        else:
            stars -= 1
            conf.append('Level ít xác nhận — cần thêm confluence')
    if dxy_div == want:
        stars += 1
        conf.append(f'DXY phân kỳ thuận chiều: {dxy_note}')
    if h4_dir == want and p['setup'] != 'sweep_reclaim':
        stars += 1
        conf.append('Xu hướng H4 thuận chiều')
    elif h4_dir not in (None, 'NEUTRAL') and h4_dir != want and \
            p['setup'] != 'sweep_reclaim':
        stars -= 1                      # trend setup nguoc H4 = nguy hiem
        conf.append('H4 ngược chiều — trừ điểm')
    if mom_dir == want and p['setup'] != 'momentum_pullback':
        stars += 1
        conf.append('Momentum regime thuận chiều')
    # Bat dinh/day tai vung kiet suc = dung nghe PA (12/06/2026):
    # mua day capitulation / ban dinh blow-off — phe thuan-move da kiet,
    # mean-reversion co xac suat bat cao nhat (vd day 4023 ngay 11/06 → +4%)
    if p.get('capit'):
        stars += 1
        conf.append(f'Bắt đỉnh/đáy tại vùng kiệt sức ({p.get("exh_note", "")}) '
                    f'— phe thuận-move đã kiệt, edge mean-reversion')
    if p.get('danger'):
        conf.append(f'⚠️ Kèo nguy hiểm — thuận-move trong vùng kiệt sức '
                    f'({p.get("exh_note", "")}): lot 0.5%, SL cap 1.8×ATR')

    p['stars'] = max(1, min(5, stars))
    p['confluence'] = conf
    return p


# ── Tracking ket qua (first-touch, SL truoc TP trong cung nen) ──
def resolve_signals(state, now, bars):
    res = 0
    for rec in state.get('signals', []):
        if rec.get('outcome'):
            continue
        is_buy   = rec['dir'] == 'BUY'
        is_limit = rec.get('entry_type') == 'limit'
        filled   = not is_limit or bool(rec.get('filled_ts'))
        for b in bars:
            if b.get('t', 0) <= rec['ts']:
                continue
            # Limit chua khop: cho nen cham gia entry truoc khi dem SL/TP.
            # Qua han ma chua khop → NOFILL (khong tinh thang/thua).
            if not filled:
                touched = (b['l'] <= rec['entry']) if is_buy else (b['h'] >= rec['entry'])
                if not touched:
                    if b['t'] - rec['ts'] > LIMIT_EXPIRY_H * 3600:
                        rec['outcome'] = 'NOFILL'
                        rec['correct'] = None
                        rec['pips']    = 0.0
                        break
                    continue
                rec['filled_ts'] = b['t']
                filled = True
                # nen khop lenh co the cham ca SL — kiem tra ngay nen nay (SL-first)
            hit_sl  = (b['l'] <= rec['sl'])  if is_buy else (b['h'] >= rec['sl'])
            hit_tp1 = (b['h'] >= rec['tp1']) if is_buy else (b['l'] <= rec['tp1'])
            hit_tp2 = (b['h'] >= rec['tp2']) if is_buy else (b['l'] <= rec['tp2'])
            if hit_sl:
                rec['outcome'] = 'SL'
                rec['correct'] = False
                rec['pips'] = fx.price_to_pips(SYM, (rec['sl'] - rec['entry']) * (1 if is_buy else -1))
                break
            if hit_tp1:
                rec['outcome'] = 'TP2' if hit_tp2 else 'TP1'
                rec['correct'] = True
                tp = rec['tp2'] if hit_tp2 else rec['tp1']
                rec['pips'] = fx.price_to_pips(SYM, (tp - rec['entry']) * (1 if is_buy else -1))
                break
        # Limit chua khop + qua han (ke ca khi khong co nen moi — cuoi tuan) → NOFILL
        if not rec.get('outcome') and is_limit and not filled and \
                (now.timestamp() - rec['ts']) > LIMIT_EXPIRY_H * 3600:
            rec['outcome'] = 'NOFILL'
            rec['correct'] = None
            rec['pips']    = 0.0
        if not rec.get('outcome') and (now.timestamp() - rec['ts']) > TIMEOUT_DAYS * 86400:
            last_c = bars[-1]['c'] if bars else rec['entry']
            move = (last_c - rec['entry']) * (1 if is_buy else -1)
            rec['outcome'] = 'EXP'
            rec['correct'] = move > 0
            rec['pips']    = fx.price_to_pips(SYM, move)
        if rec.get('outcome'):
            res += 1
            log.info(f"RESOLVED {rec['setup']} {rec['dir']} {rec['outcome']} pips={rec['pips']}")
    if res:
        print(f'[PA] Da chot {res} keo')
    state['signals'] = state.get('signals', [])[-200:]


# ── BROADCASTER ──────────────────────────────────────────────
def send_photo(path, caption='', reply_to=None):
    """Gui anh chart qua Telegram sendPhoto (multipart). Loi khong duoc lam
    hong flow gui tin hieu — caller phai wrap try/except."""
    url  = f'https://api.telegram.org/bot{fx.TELEGRAM_TOKEN}/sendPhoto'
    data = {'chat_id': fx.TELEGRAM_CHAT, 'caption': caption, 'parse_mode': 'HTML'}
    if reply_to:
        data['reply_to_message_id'] = reply_to
    with open(path, 'rb') as f:
        return requests.post(url, data=data, files={'photo': f}, timeout=20).json()


def send_signal(p, session_lbl, now):
    is_buy   = p['dir'] == 'BUY'
    emoji    = '🟢' if is_buy else '🔴'
    act      = 'BUY (MUA)' if is_buy else 'SELL (BÁN)'
    star_bar = '★' * p['stars'] + '☆' * (5 - p['stars'])
    name     = _SETUP_NAMES.get(p['setup'], p['setup'])
    now_vn   = now.astimezone(VN_TZ)

    lines = [
        f'🥇 <b>TÍN HIỆU GOLD PA — XAU/USD</b>',
        f'⏱ Khung: H1 | Phiên: {session_lbl} | {star_bar} ({p["stars"]}/5)',
        f'🧩 Setup: <b>{name}</b>',
        '━━━━━━━━━━━━━━━━━━━━',
        '',
        (f'{emoji} Lệnh: <b>{act} LIMIT</b> @ {fx.fmt_price(SYM, p["entry"])} — '
         f'giá đã bật khỏi level, KHÔNG đuổi; chờ retest, hủy sau {LIMIT_EXPIRY_H}h nếu chưa khớp'
         if p.get('entry_type') == 'limit'
         else f'{emoji} Lệnh: <b>{act}</b> quanh {fx.fmt_price(SYM, p["entry"])}'),
        f'🛑 SL:  {fx.fmt_price(SYM, p["sl"])}  ({p["sl_dist_atr"]}×ATR / -${p["sl_usd"]:.2f})',
        f'🎯 TP1: {fx.fmt_price(SYM, p["tp1"])}  (R:R 1:{p["rr1"]} / +${p["tp1_usd"]:.2f})',
        f'🎯 TP2: {fx.fmt_price(SYM, p["tp2"])}  (R:R 1:{p["rr2"]} / +${p["tp2_usd"]:.2f})',
    ]
    if p.get('rec_lot'):
        _rp = p.get('risk_pct', 0.01) * 100
        _danger_tag = ' — kèo nguy hiểm, giảm nửa' if p.get('danger') else ''
        lines.append(f'📐 Lot đề xuất: {p["rec_lot"]} lot ({_rp:g}% rủi ro{_danger_tag} / ${fx.ACCOUNT_SIZE:.0f} vốn)')
    lines.append('📌 Chạm TP1 → dời SL về entry (phần còn lại rủi ro 0)')
    lines += ['', '📝 Lý do:']
    lines += [f'  • {c}' for c in p['confluence']]
    if p.get('wall_warn'):
        lines += ['', p['wall_warn']]
    lines += [
        '',
        f'⏰ {now_vn.strftime("%H:%M %d/%m/%Y")}',
        '🧪 <i>Hệ Price Action độc lập — thống kê tách biệt với vote system</i>',
    ]
    return fx.send_telegram('\n'.join(lines))


def send_weekly(state, now):
    # NOFILL = limit khong khop, khong phai thang/thua — loai khoi thong ke WR
    done = [x for x in state.get('signals', [])
            if x.get('outcome') and x['outcome'] != 'NOFILL']
    if len(done) < 3:
        return
    wins = sum(1 for x in done if x.get('correct'))
    wr   = wins / len(done) * 100
    pips = sum(x.get('pips', 0) for x in done)
    usd  = round(pips * fx.LOT_SIZE * 10, 2)
    sign = '+' if usd >= 0 else ''
    icon = '🔥' if wr >= 65 else ('⚠️' if wr < 45 else '📊')
    lines = [
        f'🥇 <b>Báo cáo tuần — Gold PA Bot</b>',
        f'{icon} Tổng: {wins}/{len(done)} = <b>{wr:.0f}%</b>  |  💰 {sign}{usd:.2f} USD ({fx.LOT_SIZE} lot)',
        '',
    ]
    by_setup = {}
    for x in done:
        st = by_setup.setdefault(x['setup'], {'n': 0, 'w': 0, 'pips': 0.0})
        st['n'] += 1
        st['pips'] += x.get('pips', 0)
        if x.get('correct'):
            st['w'] += 1
    for code, st in sorted(by_setup.items(), key=lambda kv: -kv[1]['n']):
        name = _SETUP_NAMES.get(code, code)
        lines.append(f'  {name}: {st["w"]}/{st["n"]} ({st["pips"]:+.0f}p)')
    fx.send_telegram('\n'.join(lines))
    state['last_weekly'] = now.timestamp()
    log.info(f'WEEKLY sent n={len(done)} wr={wr:.0f}%')


# ── Main ─────────────────────────────────────────────────────
def main():
    if not fx.TELEGRAM_TOKEN or not fx.TELEGRAM_CHAT:
        print('[PA] TELEGRAM_TOKEN/TELEGRAM_CHAT chua dat — thoat')
        return

    now = datetime.now(timezone.utc)
    state = load_state()
    print(f'=== Gold PA Bot — {now.astimezone(VN_TZ).strftime("%Y-%m-%d %H:%M")} VN ===')

    # [INGESTION] price history dung chung voi forex (da fresh neu chay sau no)
    fx.load_price_history()
    closes, highs, lows, timestamps = None, None, None, None
    try:
        closes, highs, lows, timestamps = fx.fetch_ohlcv(SYM, YF_SYM)
    except Exception as e:
        log.info(f'FETCH_FAIL {e}')
    if closes and len(closes) >= 60:
        new_bars = [{'t': t, 'c': c, 'h': h, 'l': l}
                    for t, c, h, l in zip(timestamps, closes, highs, lows)]
        if SYM not in fx._price_history:
            fx._price_history[SYM] = {'bars': []}
        fx._price_history[SYM]['bars'] = fx._merge_bars(
            fx._price_history[SYM].get('bars', []), new_bars)
        fx.save_price_history()
    bars = fx._price_history.get(SYM, {}).get('bars', [])
    print(f'[PA] {len(bars)} bars H1')

    # Chot ket qua keo cu (chay moi run, ke ca ngoai phien)
    resolve_signals(state, now, bars)

    # Bao cao tuan rieng cua he PA
    if (now.timestamp() - state.get('last_weekly', 0)) >= 7 * 86400:
        try:
            send_weekly(state, now)
        except Exception as e:
            log.info(f'WEEKLY_FAIL {e}')

    if len(bars) < MIN_BARS:
        print('[PA] Chua du du lieu — thoat')
        save_state(state)
        return

    # Thi truong dong (cuoi tuan/holiday): nen cuoi dong bang → cung 1 nen
    # sweep cu se duoc detect lai sau khi cooldown het han = lenh ao lap lai.
    # Du lieu cu hon 2.5h = khong co nen moi → chi resolve, khong quet keo.
    bar_age_h = (now.timestamp() - bars[-1].get('t', 0)) / 3600
    if bar_age_h > 2.5:
        print(f'[PA] Du lieu cu {bar_age_h:.1f}h (thi truong dong?) — khong quet keo moi')
        log.info(f'MARKET_STALE age={bar_age_h:.1f}h')
        save_state(state)
        return

    closes = [b['c'] for b in bars]
    highs  = [b['h'] for b in bars]
    lows   = [b['l'] for b in bars]
    price  = closes[-1]
    lo, hi = fx.PRICE_SANITY[SYM]
    if not (lo <= price <= hi):
        log.info(f'SANITY_FAIL price={price}')
        save_state(state)
        return

    # [TRAM 2] Session
    session, session_lbl = get_session(now)
    allowed = _SESSION_ALLOWED[session]
    if not allowed:
        print(f'[PA] Phien {session} — khong quet keo moi')
        save_state(state)
        return

    # [TRAM 1] News filter — high-impact USD → khoa Brain hoan toan
    try:
        fund = fx.fetch_fundamental(now)
        cal_status, cal_reason = fx.check_calendar(fund, SYM, now)
        if cal_status == 'HARD':
            print(f'[PA] News block: {cal_reason}')
            log.info(f'NEWS_BLOCK {cal_reason}')
            save_state(state)
            return
    except Exception as e:
        log.info(f'NEWS_CHECK_FAIL {e}')

    atr_val = fx.atr(highs, lows, closes)
    if not atr_val or atr_val <= 0:
        save_state(state)
        return

    # [BRAIN] momentum regime (insight 3) — luu state de pullback dung lai
    mom_now = detect_momentum(closes, highs, lows)
    mom_st  = state.get('momentum', {})
    if mom_now:
        mom_st = {'dir': mom_now, 'ts': now.timestamp()}
        state['momentum'] = mom_st
        print(f'[PA] Momentum regime: {mom_now}')
    mom_dir = mom_st.get('dir') if \
        (now.timestamp() - mom_st.get('ts', 0)) < MOM_EXPIRY_H * 3600 else None

    levels = _gold_levels(closes, highs, lows, atr_val)
    psych  = fx.psychological_levels(price)

    cands = []
    cands += detect_sweep_reclaim(closes, highs, lows, atr_val, levels)
    cands += detect_momentum_pullback(closes, highs, lows, atr_val, mom_dir)
    cands += detect_compression_breakout(closes, highs, lows, atr_val)
    cands += detect_chart_patterns(closes, highs, lows)

    # [EXHAUSTION GUARD 12/06/2026] dung chung helper voi forex_notifier:
    # lenh thuan-move trong vung kiet (D1 RSI cuc doan + move > pctl 85) chi
    # duoc phep khi gia dang pha day/dinh moi. Vu 11/06: sweep SELL @4074 phat
    # ra khi D1 RSI=28, gia each day 5 ngay $57 — ban bounce dung day capitulation.
    exh = None
    try:
        exh = fx.exhaustion_state(bars)
    except Exception as e:
        log.info(f'EXH_CHECK_FAIL {e}')

    # [CROSS-SYSTEM GUARD 12/06/2026] TA (forex_notifier, chay truoc trong cung
    # workflow) da gui lenh XAU cung huong trong 8h → PA nhuong, tranh x2-x3
    # exposure cung 1 y tuong (11/06: 3 lenh SELL XAU trong 4h, ca 3 cung SL).
    ta_recent = {}
    try:
        if os.path.exists(fx.STATE_FILE):
            with open(fx.STATE_FILE, encoding='utf-8') as f:
                _ta_state = json.load(f)
            for _d in ('BUY', 'SELL'):
                _ts = _ta_state.get(f'{SYM}|{_d}', 0)
                if (now.timestamp() - _ts) < 8 * 3600:
                    ta_recent[_d] = _ts
    except Exception as e:
        log.info(f'CROSS_CHECK_FAIL {e}')

    # Session filter per setup + momentum khoa reversal nguoc chieu (insight 3)
    filtered = []
    for p in cands:
        if p['setup'] not in allowed:
            log.info(f"SESSION_SKIP {p['setup']} {p['dir']} session={session}")
            continue
        if mom_dir and p['setup'] != 'momentum_pullback':
            want = 'BULL' if p['dir'] == 'BUY' else 'BEAR'
            if want != mom_dir:
                print(f"[PA] {p['setup']} {p['dir']} nguoc momentum {mom_dir} — khoa")
                log.info(f"MOMENTUM_BLOCK {p['setup']} {p['dir']} regime={mom_dir}")
                continue
        if p['dir'] in ta_recent:
            _age = (now.timestamp() - ta_recent[p['dir']]) / 3600
            print(f"[PA] {p['setup']} {p['dir']} — TA da gui XAU {p['dir']} {_age:.1f}h truoc, nhuong")
            log.info(f"CROSS_DUP {p['setup']} {p['dir']} ta_sent={_age:.1f}h")
            continue
        if exh and exh.get('dir'):
            into = (p['dir'] == 'SELL' and exh['dir'] == 'DOWN') or \
                   (p['dir'] == 'BUY'  and exh['dir'] == 'UP')
            # Triet ly PA (12/06): KHONG block keo nguy hiem — giam rui ro.
            # Thuan-move khi gia da roi extreme (vd ban bounce sau capitulation)
            # = nguy hiem: lot 0.5% + cap SL 1.8 ATR (build_order/grade xu ly).
            # Nguoc-move (mua day capitulation / ban dinh blow-off) = dung nghe
            # cua PA → cong sao trong grade().
            if into and not exh['at_extreme']:
                p['danger'] = True
                p['exh_note'] = f"D1 RSI {exh['d1_rsi']}, move 5 phiên > {exh['pctl']:.0f}% lịch sử"
                log.info(f"EXHAUSTION_RISK {p['setup']} {p['dir']} "
                         f"d1_rsi={exh['d1_rsi']} pctl={exh['pctl']:.0f}")
            elif not into:
                p['capit'] = True
                p['exh_note'] = f"D1 RSI {exh['d1_rsi']}, move 5 phiên > {exh['pctl']:.0f}% lịch sử"
        filtered.append(p)
    if not filtered:
        print('[PA] Khong co setup hop le')
        log.info('NO_SETUP')
        save_state(state)
        return

    # [TRAM 3] SL/TP dong + tuong so tron
    h4_dir, _, _ = fx.h4_trend(closes, highs, lows)
    dxy = fetch_dxy_bars()
    dxy_div, dxy_note = dxy_divergence(highs, lows, dxy)

    ready = []
    for p in filtered:
        p = build_order(p, atr_val, psych)
        if p is None:
            log.info('SL_TOO_WIDE skip')
            continue
        ready.append(grade(p, mom_dir, h4_dir, dxy_div, dxy_note))

    # Cooldown + daily cap + chat luong
    today = now.strftime('%Y-%m-%d')
    day   = state.get('day_count', {})
    n_today = day.get('n', 0) if day.get('date') == today else 0
    cds   = state.get('cooldowns', {})
    final = []
    for p in ready:
        key = f"{p['setup']}|{p['dir']}"
        if (now.timestamp() - cds.get(key, 0)) / 3600 < COOLDOWN_H:
            log.info(f'COOLDOWN {key}')
            continue
        if p['stars'] < MIN_STARS:
            log.info(f"LOW_QUALITY {p['setup']} stars={p['stars']}")
            continue
        final.append(p)
    if not final:
        print('[PA] Het keo sau cooldown/chat luong')
        save_state(state)
        return
    if n_today >= DAILY_CAP:
        print(f'[PA] Da du {DAILY_CAP} keo hom nay — dung')
        log.info(f'DAILY_CAP {n_today}')
        save_state(state)
        return

    best = max(final, key=lambda p: (p['stars'], p['setup'] == 'sweep_reclaim'))
    try:
        result = send_signal(best, session_lbl, now)
    except Exception as e:
        log.info(f'TELEGRAM_FAIL {e}')
        save_state(state)
        return
    if result.get('ok'):
        cds[f"{best['setup']}|{best['dir']}"] = now.timestamp()
        state['cooldowns'] = cds
        state['day_count'] = {'date': today, 'n': n_today + 1}
        state.setdefault('signals', []).append({
            'ts': now.timestamp(), 'date': today, 'session': session,
            'setup': best['setup'], 'dir': best['dir'],
            'entry': best['entry'], 'sl': best['sl'],
            'tp1': best['tp1'], 'tp2': best['tp2'], 'stars': best['stars'],
            'entry_type': best.get('entry_type', 'market'),
            'danger': best.get('danger', False), 'capit': best.get('capit', False),
        })
        log.info(f"SENT {best['setup']} {best['dir']} stars={best['stars']} "
                 f"type={best.get('entry_type', 'market')} "
                 f"entry={best['entry']:.2f} sl={best['sl']:.2f} "
                 f"tp1={best['tp1']:.2f} tp2={best['tp2']:.2f}")
        print(f"[PA] Da gui {best['setup']} {best['dir']} {best['stars']}/5 sao")
        # Chart kem tin hieu (12/06/2026): ve nen + vung S/R + level + setup
        # de user kiem tra PA bang mat truoc khi vao lenh. Loi ve chart khong
        # anh huong tin hieu da gui (lazy import — local thieu matplotlib van chay).
        try:
            import chart_render
            overlay = [{'ts': now.timestamp(), 'dir': best['dir'],
                        'entry': best['entry'], 'sl': best['sl'],
                        'tp1': best['tp1'], 'tp2': best['tp2'],
                        'setup': best['setup'],
                        'entry_type': best.get('entry_type')}]
            cpath = chart_render.render_chart(
                bars, levels=levels, signals=overlay, n_bars=110,
                atr_val=atr_val,
                note=f"{best['setup']} {best['dir']} {best['stars']}/5 sao | phien {session}")
            if cpath:
                pr = send_photo(cpath,
                                caption=f"🥇 Chart: {best['setup']} {best['dir']} — "
                                        f"vùng cản/KC + setup đánh dấu trên nến H1",
                                reply_to=result.get('result', {}).get('message_id'))
                log.info('CHART_SENT' if pr.get('ok') else f'CHART_TG_FAIL {pr}')
        except Exception as e:
            log.info(f'CHART_FAIL {e}')
    else:
        print(f'[PA] Loi Telegram: {result}')
    save_state(state)


if __name__ == '__main__':
    main()
