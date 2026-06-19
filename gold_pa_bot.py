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

Nang cap 13/06/2026 (user feedback "PA khong don gian nhu the"):
  - market_structure(): trend doc bang swing + BOS/CHoCH (SMC/Al Brooks) —
    nen DONG pha swing moi tinh, CHoCH = canh bao chua phai xac nhan
  - Probe & Pyramid: keo chua duoc cau truc xac nhan → SL BE do duong (0.6-1.2
    ATR, 0.5% von) + check_addons() nhac NHOI khi H1 dong pha can xac nhan
  - update_knowledge(): learning loop 1 lan/ngay — gom keo da chot theo bucket,
    dieu chinh sao adaptive (Laplace, min n=8, kep ±1) → moi ngay thong minh hon
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
TP1_R           = 1.5    # TP1 = 1.5R (mac dinh cac setup)
TP2_R           = 2.5    # TP2 = 2.5R (chart pattern dung measured move neu xa hon)
# Sweep_reclaim — toi uu OUT-OF-SAMPLE (workshop/hyp08, train60/test40 tren 5.8
# nam): TP1_R=1.0 cho test +0.143R significant (WR ~60%), trong khi 1.5 thi test
# KHONG significant; sweep la mean-reversion -> chot nhanh. TP2 runner xa (2.5R)
# LAM GIAM edge (nua runner hay bi quet ve BE) -> keo TP2 sweep ve gan (1.5R).
SWEEP_TP1_R     = 1.0
SWEEP_TP2_R     = 1.5
WICK_BODY_RATIO = 1.5    # sweep: wick >= 1.5x than. Ha tu 2.0 -> 1.5 (hyp08: nhieu
                         # lenh hon + test van significant; 2.0 thi test khong sig).
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

# ── Validated-edge tier (18/06/2026) ─────────────────────────
# Backtest TRUNG THUC 5.8 nam vang sach (n=2899, workshop/hyp06+hyp07, import
# chinh code nay): CHI 'sweep_reclaim' co edge that — exp +0.10R sau spread,
# CI95>0, walk-forward on dinh (duong dau/giua/cuoi), random-entry p=0.000
# (timing hon han vao ngau nhien cung SL/TP = KY NANG, khong phai cau truc R:R).
# Cac setup khac chua chung minh edge (momentum_pullback -0.09R, compression
# -0.06R, patterns mau nho CI om 0) -> tru sao de can confluence manh hon moi
# phat. Khi setup nao validate duoc thi them vao VALIDATED_SETUPS.
VALIDATED_SETUPS    = {'sweep_reclaim', 'breakout'}
UNVALIDATED_PENALTY = 1

# ── REGIME-SWITCH (18/06/2026) ───────────────────────────────
# Phat hien gia tri nhat ca phien (workshop/hyp17-23): sweep (mean-reversion) AN
# trong thi truong RANGE, CHET trong thang trend manh (vang chay >10%); con
# breakout (trend-following) nguoc lai. ML (RandomForest) xac nhan: yeu to quyet
# dinh la DO-TREND (momentum/EMA-dist/daily-ER), khong phai level strength.
# -> Cong tac theo Efficiency Ratio KHUNG NGAY (20 ngay): ngay TREND -> chi danh
# breakout; ngay RANGE -> chi danh sweep. Edge ket hop MONG nhung nhat quan hon
# (vá nam lo). REGIME_ER tu nen ngay DA DONG (khong look-ahead).
REGIME_ER_N      = 20     # Efficiency Ratio tren 20 nen NGAY
REGIME_ER_TREND  = 0.40   # dailyER >= 0.40 -> coi la TREND day
# Breakout (trend tool): Donchian N + loc EMA50, SL/TP co dinh theo ATR (R:R 2)
BREAKOUT_N       = 20     # pha dinh/day 20 nen H1 TRUOC
BREAKOUT_SL_ATR  = 1.0    # SL = 1.0 ATR
BREAKOUT_TP_R    = 2.0    # TP = 2.0 R (R:R 2:1) — trend can R:R lon
TIMEOUT_DAYS    = 5      # keo khong cham SL/TP sau 5 ngay → het han (EXP)
MIN_BARS        = 120    # du lieu H1 toi thieu

# ── Probe & Pyramid (13/06/2026 — user: "SL bé dò đường, phá cản mới đánh đuổi") ──
# Lenh chua duoc CAU TRUC xac nhan (BOS — close pha swing) = lenh PHAN DOAN →
# KHONG dat SL rong tai structure nua: danh SL BE de do duong (chap nhan bi
# quet som, mat it tien), kem ke hoach NHOI (add-on) khi nen H1 DONG pha
# can/swing xac nhan trend. Ghi de mot phan triet ly 12/06 ("SL theo structure")
# — ap dung cho lenh CHUA xac nhan; lenh da xac nhan van giu SL structure.
SWING_K          = 3      # pivot fractal H1: cuc tri so voi 3 nen moi ben
SWING_K_H4       = 2      # H4 it nen hon → 2 nen moi ben
PROBE_SL_FLOOR   = 0.6    # SL do duong toi thieu 0.6 ATR (chong noise tic/spread)
PROBE_SL_BUFFER  = 0.20   # dem sau structure gan (vd rau nen sweep)
PROBE_SL_CAP     = 1.2    # SL do duong toi da 1.2 ATR — cat trong structure neu
                          # can: do duong CHAP NHAN bi quet, bu lai vao lai khi pha can
PROBE_RISK_PCT   = 0.005  # lenh do: rui ro 0.5% von
# Phan tang sizing (18/06): sweep "high-conviction" = Level manh >=3 cham + GIO
# VANG (00-07 UTC) -> risk 3% thay vi 1%. Backtest train/test (workshop/hyp13+15):
# nhom nay exp +0.148R (gap 2.4x), WR 61%, BEN tren held-out (test>train); danh 3x
# rieng nhom nay -> tong lai gap ~3.4x baseline, loi/DD tot hon (5.2->8.1) =
# HIEU QUA, khong chi don bay. Probe/danger VAN giu 0.5% (an toan truoc).
# REVERT 18/06: hạ 3% -> 1% (= bằng thường) vì backtest theo tháng cho thấy năm
# gần nhất LỖ — sweep bị regime trend mạnh của vàng 2025-26 hại; đánh 3× lúc edge
# đang không chạy = nhân lỗ. Giữ cờ high_conviction + nhãn ⭐ (vô hại); bật lại 0.03
# khi hệ KẾT HỢP (sweep+trend theo regime) được validate cho lời đều hàng tháng.
HIGH_CONV_RISK_PCT = 0.01
ADDON_SL_ATR     = 0.5    # lenh nhoi: SL sau level vua pha 0.5 ATR (can pha = ho tro moi)

# ── Learning loop (13/06/2026 — "moi ngay thong minh hon") ──
# Lance Beggs (YTC): thong ke → diem manh/yeu → dieu chinh. Moi ngay 1 lan gom
# keo da chot theo bucket (setup, setup|session, mode, align); bucket du mau
# → dieu chinh sao adaptive ap vao grade() (kep ±1, Laplace smoothing — Gap 1
# sample size, khong overfit theo chuoi ngan).
LEARN_MIN_N      = 8      # bucket >= 8 keo moi duoc dieu chinh
LEARN_WR_GOOD    = 0.55   # WR Laplace >= 55% + ky vong R duong → +1 sao
LEARN_WR_BAD     = 0.30   # WR <= 30% hoac ky vong <= -0.30R → -1 sao

_SETUP_NAMES = {
    'sweep_reclaim': 'Sweep & Reclaim (quét thanh khoản + rút râu)',
    'breakout': 'Breakout (phá đỉnh/đáy theo trend — chỉ ngày TREND)',
    'momentum_pullback': 'Momentum Pullback (hồi về EMA20 trong trend mạnh)',
    'compression_breakout': 'Compression Breakout (nén rồi bùng nổ)',
    'double_top': 'Hai đỉnh (Double Top) — H4',
    'double_bottom': 'Hai đáy (Double Bottom) — H4',
    'hs_top': 'Vai-Đầu-Vai (H&S) — H4',
    'hs_inv': 'Vai-Đầu-Vai ngược (iH&S) — H4',
    'addon': 'Nhồi lệnh (đánh đuổi sau phá cản)',
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
    'EU_US': {'sweep_reclaim', 'breakout', 'momentum_pullback',  # cho het
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


# ── BRAIN: Market Structure (BOS/CHoCH) — 13/06/2026 ────────
# Thay the loi so sanh "max 10 nen truoc vs max 10 nen sau" (qua don gian —
# user feedback 13/06). Chuan SMC/Al Brooks:
#   - Swing = pivot fractal DA XAC NHAN (k nen dong moi ben)
#   - BOS  (Break of Structure): nen DONG pha swing THUAN trend → xac nhan tiep dien
#   - CHoCH (Change of Character): nen DONG pha swing NGUOC trend → canh bao dao
#     chieu, trend doi huong nhung confirmed=False cho den khi co BOS theo huong moi
#   - Pha bang rau (wick) khong tinh — chi close moi tinh
def find_swings(highs, lows, k=SWING_K):
    """Pivot fractal da xac nhan: cuc tri so voi k nen moi ben.
    Tra ve list [bar_index, price, 'H'|'L'] theo thoi gian; swing cung loai
    lien tiep giu cai cuc doan hon (loc nhieu pivot trong 1 song)."""
    sw = []
    n = len(highs)
    for i in range(k, n - k):
        if highs[i] >= max(highs[i-k:i+k+1]):
            sw.append([i, highs[i], 'H'])
        if lows[i] <= min(lows[i-k:i+k+1]):
            sw.append([i, lows[i], 'L'])
    out = []
    for s in sw:
        if out and out[-1][2] == s[2]:
            if (s[2] == 'H' and s[1] >= out[-1][1]) or \
               (s[2] == 'L' and s[1] <= out[-1][1]):
                out[-1] = s
        else:
            out.append(s)
    return out


def market_structure(closes, highs, lows, k=SWING_K):
    """Trend theo cau truc swing + BOS/CHoCH, walk tung nen de khong look-ahead
    (swing chi 'ton tai' sau khi k nen ben phai da dong).

    Returns dict:
      trend      'UP'|'DOWN'|'RANGE'
      confirmed  True chi khi co BOS thuan huong (CHoCH/pha range lan dau = False
                 — Al Brooks: phan lon breakout fail, can follow-through)
      last_event 'BOS_UP'|'CHOCH_UP'|'BOS_DOWN'|'CHOCH_DOWN'|''
      swing_hi/swing_lo  swing gan nhat CHUA bi pha (None = vua pha xong)
      hi_list/lo_list    cac swing gan day (de tim can lam add-trigger)
      note       mo ta tieng Viet cho confluence/message
    """
    res = {'trend': 'RANGE', 'confirmed': False, 'last_event': '',
           'swing_hi': None, 'swing_lo': None, 'hi_list': [], 'lo_list': [],
           'note': 'chưa đủ swing để đọc cấu trúc'}
    swings = find_swings(highs, lows, k)
    if len(swings) < 2:
        return res
    trend, confirmed, last_event = 'RANGE', False, ''
    sh = sl_ = None
    j = 0
    for i in range(swings[0][0] + k + 1, len(closes)):
        while j < len(swings) and swings[j][0] + k < i:
            _, sp, typ = swings[j]
            if typ == 'H':
                sh = sp
                res['hi_list'].append(sp)
            else:
                sl_ = sp
                res['lo_list'].append(sp)
            j += 1
        c = closes[i]
        if sh is not None and c > sh:
            if trend == 'DOWN':
                trend, confirmed, last_event = 'UP', False, 'CHOCH_UP'
            elif trend == 'UP':
                confirmed, last_event = True, 'BOS_UP'
            else:                       # RANGE: pha lan dau, cho follow-through
                trend, confirmed, last_event = 'UP', False, 'BOS_UP'
            sh = None
        elif sl_ is not None and c < sl_:
            if trend == 'UP':
                trend, confirmed, last_event = 'DOWN', False, 'CHOCH_DOWN'
            elif trend == 'DOWN':
                confirmed, last_event = True, 'BOS_DOWN'
            else:
                trend, confirmed, last_event = 'DOWN', False, 'BOS_DOWN'
            sl_ = None
    res.update({'trend': trend, 'confirmed': confirmed, 'last_event': last_event,
                'swing_hi': sh, 'swing_lo': sl_,
                'hi_list': res['hi_list'][-6:], 'lo_list': res['lo_list'][-6:]})
    vn = {'UP': 'TĂNG', 'DOWN': 'GIẢM', 'RANGE': 'SIDEWAY'}[trend]
    ev = {'BOS_UP': 'BOS lên', 'CHOCH_UP': 'CHoCH lên',
          'BOS_DOWN': 'BOS xuống', 'CHOCH_DOWN': 'CHoCH xuống', '': ''}[last_event]
    res['note'] = (f'{vn} đã xác nhận ({ev})' if confirmed
                   else f'{vn} CHƯA xác nhận' + (f' (mới {ev} — chờ BOS)' if ev else ''))
    return res


def add_trigger_for(p, s1):
    """Can/swing ma neu nen H1 DONG pha qua thi trend xac nhan theo huong lenh
    do → diem 'danh duoi'. BUY: swing high gan nhat TREN entry; SELL: swing low
    gan nhat DUOI entry."""
    entry = p['entry']
    if p['dir'] == 'BUY':
        cands = [h for h in (s1['hi_list'] + ([s1['swing_hi']] if s1['swing_hi'] else []))
                 if h > entry]
        return min(cands) if cands else None
    cands = [l for l in (s1['lo_list'] + ([s1['swing_lo']] if s1['swing_lo'] else []))
             if l < entry]
    return max(cands) if cands else None


def classify_probe(p, s1, s4):
    """Lenh DO (probe) vs lenh DAY DU. User 13/06: 'giao dich nguy hiem va
    phan doan thi danh SL be do duong, pha can moi danh duoi theo'.
    - sweep_reclaim = keo mean-reversion → do theo cau truc H1 (dao chieu chi
      'that' khi H1 da CHoCH + BOS theo huong lenh)
    - setup trend-following → do theo cau truc H4
    - keo danger (thuan-move vung kiet) luon la probe"""
    if p.get('danger'):
        return True
    want = 'UP' if p['dir'] == 'BUY' else 'DOWN'
    st = s1 if p['setup'] == 'sweep_reclaim' else s4
    return not (st['trend'] == want and st['confirmed'])


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


def detect_breakout(closes, highs, lows, atr_val):
    """Trend tool (validated workshop/hyp18-21): nen H1 DONG gan nhat pha dinh/day
    BREAKOUT_N nen TRUOC + thuan EMA50 -> vao theo huong pha. SL/TP co dinh ATR
    (build_order xu ly rieng setup='breakout'). Chi danh trong NGAY TREND (main gate)."""
    n = BREAKOUT_N
    if len(closes) < n + 55 or atr_val <= 0:
        return []
    c = closes[-2]                              # nen H1 da DONG gan nhat (-1 dang hinh)
    dhi = max(highs[-n-2:-2])                   # dinh N nen TRUOC nen -2 (KHONG gom -2/-1)
    dlo = min(lows[-n-2:-2])
    e50 = fx.ema(closes[:-1], 50)
    price = closes[-1]
    out = []
    if c > dhi and c > e50:
        out.append({'setup': 'breakout', 'dir': 'BUY', 'entry': price,
                    'structure': price - BREAKOUT_SL_ATR * atr_val, 'level': dhi,
                    'reason': f'H1 đóng cửa phá đỉnh {n} nến {dhi:,.1f} + trên EMA50 — trend tiếp diễn'})
    elif c < dlo and c < e50:
        out.append({'setup': 'breakout', 'dir': 'SELL', 'entry': price,
                    'structure': price + BREAKOUT_SL_ATR * atr_val, 'level': dlo,
                    'reason': f'H1 đóng cửa phá đáy {n} nến {dlo:,.1f} + dưới EMA50 — trend tiếp diễn'})
    return out


def daily_er_regime(bars):
    """Efficiency Ratio tren NEN NGAY (gop H1->ngay), CHI dung ngay DA DONG ->
    no look-ahead. Tra ve (er, is_trend). er cao = vang dang trend nhieu tuan."""
    by_day = {}
    for b in bars:
        d = datetime.fromtimestamp(b.get('t', 0), timezone.utc).strftime('%Y-%m-%d')
        by_day[d] = b['c']                      # close cuoi cung trong ngay
    ks = sorted(by_day)
    if len(ks) < REGIME_ER_N + 2:
        return 0.0, False
    cl = [by_day[k] for k in ks[:-1]]           # BO ngay hom nay (chua dong)
    if len(cl) < REGIME_ER_N + 1:
        return 0.0, False
    change = abs(cl[-1] - cl[-1 - REGIME_ER_N])
    vol = sum(abs(cl[j] - cl[j - 1]) for j in range(len(cl) - REGIME_ER_N, len(cl)))
    er = change / vol if vol > 0 else 0.0
    return round(er, 3), er >= REGIME_ER_TREND


# ── BRAIN: SL/TP dong (Tram 3) + tuong so tron (insight 2) ───
def build_order(p, atr_val, psych_levels):
    """Tinh SL/TP/lot. Tra ve None neu SL bat buoc qua rong (> cap) —
    thay vi nong SL, bo keo (quan ly rui ro truoc, keo sau).

    Probe (13/06/2026): lenh chua duoc cau truc xac nhan → SL BE do duong
    (floor 0.6 ATR, cap cung 1.2 ATR — cat trong structure neu can, chap nhan
    bi quet som) + rui ro 0.5%; bu lai co ke hoach nhoi khi pha can."""
    is_buy = p['dir'] == 'BUY'
    entry  = p['entry']

    if p['setup'] == 'breakout':
        sl_dist = BREAKOUT_SL_ATR * atr_val          # SL co dinh 1 ATR (validated)
    elif p.get('probe'):
        struct_dist = abs(entry - p['structure']) + PROBE_SL_BUFFER * atr_val
        sl_dist = max(PROBE_SL_FLOOR * atr_val,
                      min(struct_dist, PROBE_SL_CAP * atr_val))
    else:
        struct_dist = abs(entry - p['structure']) + SL_BUFFER_ATR * atr_val
        sl_dist     = max(SL_ATR_FLOOR * atr_val, struct_dist)
        # Keo nguy hiem (da la probe o tren — nhanh nay chi con phong thu)
        sl_cap = DANGER_SL_CAP if p.get('danger') else SL_ATR_CAP
        if sl_dist > sl_cap * atr_val:
            return None
    sl  = entry - sl_dist if is_buy else entry + sl_dist
    # TP per-setup: breakout R:R 2 (trend); sweep chốt nhanh 1.0/1.5 (validated); còn lại 1.5/2.5
    if p['setup'] == 'breakout':
        _tp1_r, _tp2_r = BREAKOUT_TP_R, BREAKOUT_TP_R + 1.0
    elif p['setup'] == 'sweep_reclaim':
        _tp1_r, _tp2_r = SWEEP_TP1_R, SWEEP_TP2_R
    else:
        _tp1_r, _tp2_r = TP1_R, TP2_R
    tp1 = entry + _tp1_r * sl_dist if is_buy else entry - _tp1_r * sl_dist
    tp2 = entry + _tp2_r * sl_dist if is_buy else entry - _tp2_r * sl_dist
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
    if p.get('probe') or p.get('danger'):
        risk_pct = PROBE_RISK_PCT                  # 0.5% — an toan truoc
    elif p.get('high_conviction'):
        risk_pct = HIGH_CONV_RISK_PCT              # 3% — sweep Level>=3 + gio vang
    else:
        risk_pct = 0.01                            # 1% mac dinh
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


def grade(p, mom_dir, s1, s4, dxy_div, dxy_note, session, knowledge=None):
    """Cham 1-5 sao theo confluence. Base 3 (setup hop le + qua het bo loc).

    13/06/2026: xu huong doc bang market_structure (BOS/CHoCH) thay vi
    h4_trend max-cua-so; them dieu chinh adaptive tu learning loop.

    Backtest 30 ngay (n=23, chi mang tinh dinh huong):
      - Sweep tai level manh (>=3 cham): 46% vs level yeu 29% → location la
        dieu kien song con, sweep level yeu phai -1 sao (can confluence khac)
      - Sweep thuan H4 20% vs nguoc H4 44% → sweep la keo MEAN-REVERSION,
        khong cong sao H4 cho sweep (chi cong cho setup trend-following)"""
    stars = 3 - p.get('star_pen', 0)
    conf  = [p['reason']]
    want  = 'BULL' if p['dir'] == 'BUY' else 'BEAR'
    want_t = 'UP' if p['dir'] == 'BUY' else 'DOWN'

    if p['setup'] == 'sweep_reclaim':
        if p.get('level_strength', 0) >= 3:
            stars += 1
            conf.append(f'Level mạnh ({p["level_strength"]} lần xác nhận)')
        else:
            stars -= 1
            conf.append('Level ít xác nhận — cần thêm confluence')
        conf.append(f'Cấu trúc H1: {s1["note"]}')
    if dxy_div == want:
        stars += 1
        conf.append(f'DXY phân kỳ thuận chiều: {dxy_note}')
    if p['setup'] != 'sweep_reclaim':
        if s4['trend'] == want_t and s4['confirmed']:
            stars += 1
            conf.append(f'Cấu trúc H4 thuận chiều: {s4["note"]}')
        elif s4['trend'] not in ('RANGE', want_t) and s4['confirmed']:
            stars -= 1                  # trend setup nguoc cau truc da xac nhan
            conf.append(f'Ngược cấu trúc H4 đã xác nhận ({s4["note"]}) — trừ điểm')
        else:
            conf.append(f'Cấu trúc H4: {s4["note"]} — chưa đứng về phe nào')
    if mom_dir == want and p['setup'] != 'momentum_pullback':
        stars += 1
        conf.append('Momentum regime thuận chiều')
    # Dieu chinh adaptive tu learning loop (kep ±1 tong — chong overfit)
    kn = (knowledge or {}).get('adjust', {})
    adj = sum(kn.get(k, 0) for k in
              (f"setup:{p['setup']}", f"setup:{p['setup']}|sess:{session}"))
    adj = max(-1, min(1, adj))
    if adj:
        stars += adj
        conf.append(f'📚 Học từ lịch sử kèo đã chốt: {adj:+d} sao')
    # Bat dinh/day tai vung kiet suc = dung nghe PA (12/06/2026):
    # mua day capitulation / ban dinh blow-off — phe thuan-move da kiet,
    # mean-reversion co xac suat bat cao nhat (vd day 4023 ngay 11/06 → +4%)
    if p.get('capit'):
        stars += 1
        conf.append(f'Bắt đỉnh/đáy tại vùng kiệt sức ({p.get("exh_note", "")}) '
                    f'— phe thuận-move đã kiệt, edge mean-reversion')
    if p.get('danger'):
        conf.append(f'⚠️ Kèo nguy hiểm — thuận-move trong vùng kiệt sức '
                    f'({p.get("exh_note", "")})')
    if p.get('probe'):
        conf.append('🔎 Kèo PHÁN ĐOÁN (trend chưa được cấu trúc xác nhận) — '
                    'đánh SL bé dò đường, phá cản mới nhồi thêm')

    if p.get('high_conviction'):
        conf.append('⭐ HIGH-CONVICTION: Level mạnh + giờ vàng (phiên Á) — '
                    f'rủi ro {HIGH_CONV_RISK_PCT*100:.0f}% (edge dày nhất, đã kiểm chứng OOS)')

    # Validated-edge tier: setup chưa chứng minh edge (backtest 5.8 năm) bị trừ
    # sao → cần confluence mạnh hơn mới đạt MIN_STARS. Sweep (đã validate) giữ nguyên.
    if p['setup'] not in VALIDATED_SETUPS:
        stars -= UNVALIDATED_PENALTY
        conf.append(f'⚖️ Setup chưa chứng minh edge trên backtest 5.8 năm '
                    f'(−{UNVALIDATED_PENALTY} sao — cần confluence mạnh để phát)')

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


# ── Probe add-on: "đánh đuổi" sau khi phá cản (13/06/2026) ───
def check_addons(state, now, bars, atr_val):
    """Lenh do (probe) dang mo + nen H1 DONG pha qua add_trigger (swing/can
    xac nhan trend) → goi y NHOI 0.5% von, SL sau can vua pha (can pha = ho
    tro/khang cu moi). Moi probe nhoi toi da 1 lan; lenh nhoi tracking rieng
    (setup='addon') de learning loop do duoc danh duoi co an khong."""
    for rec in state.get('signals', []):
        if not rec.get('probe') or rec.get('add_sent') or rec.get('outcome'):
            continue
        trig = rec.get('add_trigger')
        if not trig:
            continue
        if rec.get('entry_type') == 'limit' and not rec.get('filled_ts'):
            continue                          # probe chua khop thi chua nhoi
        is_buy = rec['dir'] == 'BUY'
        for b in bars[:-1]:                   # chi xet nen DA DONG
            if b.get('t', 0) <= rec['ts']:
                continue
            if not (b['c'] > trig if is_buy else b['c'] < trig):
                continue
            price = bars[-1]['c']
            tp    = rec['tp2']
            # gia da chay qua/gan het duong den TP2 → nhoi vo nghia
            if (is_buy and price >= tp - 0.3 * atr_val) or \
               (not is_buy and price <= tp + 0.3 * atr_val):
                rec['add_sent'] = -1          # danh dau bo qua, khong xet lai
                log.info(f"ADDON_SKIP_LATE {rec['setup']} {rec['dir']} trig={trig:.1f}")
                break
            sl  = trig - ADDON_SL_ATR * atr_val if is_buy else trig + ADDON_SL_ATR * atr_val
            sl_pips = fx.price_to_pips(SYM, abs(price - sl))
            sl_usd  = round(sl_pips * fx.LOT_SIZE * 10, 2)
            lot = None
            if fx.ACCOUNT_SIZE > 0 and sl_usd > 0:
                lot = max(round((fx.ACCOUNT_SIZE * PROBE_RISK_PCT) / (sl_usd / fx.LOT_SIZE), 2), 0.01)
            emoji = '🟢' if is_buy else '🔴'
            act   = 'BUY (MUA)' if is_buy else 'SELL (BÁN)'
            lines = [
                f'🔼 <b>NHỒI LỆNH — XAU/USD (đánh đuổi sau phá cản)</b>',
                f'Lệnh dò {rec["dir"]} @ {fx.fmt_price(SYM, rec["entry"])} '
                f'({datetime.fromtimestamp(rec["ts"], VN_TZ).strftime("%d/%m %H:%M")}) '
                f'đã được xác nhận:',
                f'✅ H1 đóng cửa phá {"trên cản" if is_buy else "dưới hỗ trợ"} '
                f'{fx.fmt_price(SYM, trig)} — trend xác nhận theo hướng lệnh dò',
                '',
                f'{emoji} Lệnh: <b>{act}</b> quanh {fx.fmt_price(SYM, price)}',
                f'🛑 SL:  {fx.fmt_price(SYM, sl)}  (sau cản vừa phá / -${sl_usd:.2f})',
                f'🎯 TP:  {fx.fmt_price(SYM, tp)}  (TP2 của lệnh dò)',
            ]
            if lot:
                lines.append(f'📐 Lot đề xuất: {lot} lot (0.5% rủi ro / ${fx.ACCOUNT_SIZE:.0f} vốn)')
            lines += ['', '📌 Lệnh dò gốc: dời SL về entry (hòa vốn) — tổng vị thế giờ rủi ro tối thiểu']
            try:
                r = fx.send_telegram('\n'.join(lines))
            except Exception as e:
                log.info(f'ADDON_TG_FAIL {e}')
                break
            if r.get('ok'):
                rec['add_sent'] = now.timestamp()
                state.setdefault('signals', []).append({
                    'ts': now.timestamp(), 'date': now.strftime('%Y-%m-%d'),
                    'session': rec.get('session'), 'setup': 'addon',
                    'dir': rec['dir'], 'entry': price, 'sl': sl,
                    'tp1': tp, 'tp2': tp, 'stars': rec.get('stars', 3),
                    'entry_type': 'market', 'probe': False,
                    'parent_ts': rec['ts'],
                })
                log.info(f"ADDON_SENT {rec['dir']} trig={trig:.1f} entry={price:.2f} sl={sl:.2f}")
                print(f"[PA] Nhoi lenh {rec['dir']} sau pha can {trig:.1f}")
            break


# ── Learning loop: moi ngay thong minh hon (13/06/2026) ─────
def _bucket_keys(rec):
    keys = [f"setup:{rec.get('setup')}",
            f"setup:{rec.get('setup')}|sess:{rec.get('session', '?')}"]
    if rec.get('probe'):
        keys.append('mode:probe')
    if rec.get('danger'):
        keys.append('mode:danger')
    if rec.get('capit'):
        keys.append('mode:capit')
    if rec.get('align'):
        keys.append(f"align:{rec['align']}")
    return keys


def update_knowledge(state, now):
    """Vong lap hoc 1 lan/ngay (Lance Beggs YTC: thong ke → manh/yeu → dieu
    chinh). Gom keo da chot theo bucket; bucket >= LEARN_MIN_N keo: WR Laplace
    + ky vong R quyet dinh dieu chinh sao (chi ap dung bucket setup:*, cac
    bucket khac chi ghi bai hoc). Khac biet vs hardcode: nguong tu data chinh
    he nay, co floor (min n) va cap (±1 sao) — [[feedback-adaptive-over-fixed]].
    Thay doi dieu chinh → bao Telegram ngan de user biet bot vua 'hoc' gi."""
    done = [x for x in state.get('signals', [])
            if x.get('outcome') and x['outcome'] != 'NOFILL']
    stats = {}
    for rec in done:
        sl_pips = fx.price_to_pips(SYM, abs(rec.get('entry', 0) - rec.get('sl', 0)))
        for k in _bucket_keys(rec):
            s = stats.setdefault(k, {'n': 0, 'w': 0, 'r': 0.0})
            s['n'] += 1
            s['w'] += 1 if rec.get('correct') else 0
            if sl_pips:
                s['r'] += (rec.get('pips') or 0.0) / sl_pips
    adjust, lessons = {}, []
    for k in sorted(stats):
        s = stats[k]
        if s['n'] < LEARN_MIN_N:
            continue
        wr    = (s['w'] + 1) / (s['n'] + 2)          # Laplace smoothing
        exp_r = s['r'] / s['n']
        verdict = ''
        if k.startswith('setup:'):
            if wr >= LEARN_WR_GOOD and exp_r > 0:
                adjust[k] = 1
                verdict = ' → +1 sao'
            elif wr <= LEARN_WR_BAD or exp_r <= -0.30:
                adjust[k] = -1
                verdict = ' → -1 sao'
        lessons.append(f"{k}: {s['w']}/{s['n']} thắng, kỳ vọng {exp_r:+.2f}R{verdict}")
    old = state.get('pa_knowledge', {}).get('adjust', {})
    state['pa_knowledge'] = {'adjust': adjust, 'updated': now.timestamp(),
                             'n_resolved': len(done), 'lessons': lessons[-40:]}
    state['last_learn'] = now.timestamp()
    log.info(f'LEARN n={len(done)} adjust={adjust}')
    if adjust != old:
        diff = []
        for k in sorted(set(adjust) | set(old)):
            if adjust.get(k, 0) != old.get(k, 0):
                diff.append(f'  {k}: {old.get(k, 0):+d} → {adjust.get(k, 0):+d} sao')
        try:
            fx.send_telegram('\n'.join(
                ['📚 <b>Gold PA Bot — học từ dữ liệu mới</b>',
                 f'(dựa trên {len(done)} kèo đã chốt, bucket ≥{LEARN_MIN_N} kèo mới được điều chỉnh)']
                + diff))
        except Exception as e:
            log.info(f'LEARN_TG_FAIL {e}')


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

    mode = ' | 🔎 LỆNH DÒ' if p.get('probe') else ''
    lines = [
        f'🥇 <b>TÍN HIỆU GOLD PA — XAU/USD</b>',
        f'⏱ Khung: H1 | Phiên: {session_lbl} | {star_bar} ({p["stars"]}/5){mode}',
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
        _tag = (' — lệnh dò, đánh nhỏ' if p.get('probe')
                else ' — kèo nguy hiểm, giảm nửa' if p.get('danger') else '')
        lines.append(f'📐 Lot đề xuất: {p["rec_lot"]} lot ({_rp:g}% rủi ro{_tag} / ${fx.ACCOUNT_SIZE:.0f} vốn)')
    lines.append('📌 Chạm TP1 → dời SL về entry (phần còn lại rủi ro 0)')
    if p.get('probe'):
        trig = p.get('add_trigger')
        side = 'trên cản' if is_buy else 'dưới hỗ trợ'
        lines.append(
            f'🔼 Kế hoạch nhồi: nến H1 đóng {side} {fx.fmt_price(SYM, trig)} '
            f'→ trend xác nhận, vào thêm 0.5% (bot sẽ nhắn khi phá)'
            if trig else
            '🔎 Lệnh dò: SL bé chấp nhận bị quét sớm — sai thì mất ít, đúng thì giữ kèo')
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

    # Learning loop 1 lan/ngay: gom keo da chot → dieu chinh sao adaptive
    if (now.timestamp() - state.get('last_learn', 0)) >= 86400:
        try:
            update_knowledge(state, now)
        except Exception as e:
            log.info(f'LEARN_FAIL {e}')

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

    atr_val = fx.atr(highs, lows, closes)
    if not atr_val or atr_val <= 0:
        save_state(state)
        return

    # Danh duoi (13/06/2026): probe da khop + H1 dong pha can xac nhan → goi y
    # nhoi. Chay TRUOC session gate — pha can co the xay ra o bat ky phien nao.
    try:
        check_addons(state, now, bars, atr_val)
    except Exception as e:
        log.info(f'ADDON_FAIL {e}')

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

    # [REGIME-SWITCH 18/06] Efficiency Ratio khung NGAY quyet dinh che do:
    #   TREND day  -> chi danh BREAKOUT (trend tool); sweep chet trong trend.
    #   RANGE day  -> chi danh SWEEP (mean-reversion); + cac setup phu (downweight).
    er_day, is_trend = daily_er_regime(bars)
    regime_lbl = f"TREND (dailyER={er_day})" if is_trend else f"RANGE (dailyER={er_day})"
    print(f'[PA] Regime ngay: {regime_lbl}')
    log.info(f'REGIME er={er_day} trend={is_trend}')

    cands = []
    if is_trend:
        cands += detect_breakout(closes, highs, lows, atr_val)
    else:
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

    # [TRAM 3] Cau truc BOS/CHoCH (13/06/2026 — thay fx.h4_trend max-cua-so):
    # H1 cho sweep/mean-reversion, H4 cho trend setup. SL/TP dong + tuong so tron.
    s1 = market_structure(closes[:-1], highs[:-1], lows[:-1], k=SWING_K)
    h4_c, h4_h, h4_l = fx.resample_to_h4(closes, highs, lows)
    s4 = market_structure(h4_c, h4_h, h4_l, k=SWING_K_H4)
    print(f"[PA] Cau truc H1: {s1['trend']} conf={s1['confirmed']} | "
          f"H4: {s4['trend']} conf={s4['confirmed']}")
    dxy = fetch_dxy_bars()
    dxy_div, dxy_note = dxy_divergence(highs, lows, dxy)

    ready = []
    knowledge = state.get('pa_knowledge')
    for p in filtered:
        want_t = 'UP' if p['dir'] == 'BUY' else 'DOWN'
        st = s1 if p['setup'] == 'sweep_reclaim' else s4
        p['align'] = ('with' if (st['trend'] == want_t and st['confirmed'])
                      else 'against' if st['trend'] not in ('RANGE', want_t)
                      else 'neutral')
        p['probe'] = False if p['setup'] == 'breakout' else classify_probe(p, s1, s4)
        if p['probe']:
            p['add_trigger'] = add_trigger_for(p, s1)
        # Phan tang sizing: sweep Level>=3 + gio vang (00-07 UTC) = high-conviction
        # -> risk 3% (build_order doc co nay). Khong ap cho lenh do/nguy hiem.
        p['high_conviction'] = (p['setup'] == 'sweep_reclaim'
                                and p.get('level_strength', 0) >= 3
                                and 0 <= now.hour < 7
                                and not p.get('probe') and not p.get('danger'))
        p = build_order(p, atr_val, psych)
        if p is None:
            log.info('SL_TOO_WIDE skip')
            continue
        ready.append(grade(p, mom_dir, s1, s4, dxy_div, dxy_note, session, knowledge))

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
            'probe': best.get('probe', False), 'align': best.get('align'),
            'add_trigger': best.get('add_trigger'),
            'sl_dist_atr': best.get('sl_dist_atr'),
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
