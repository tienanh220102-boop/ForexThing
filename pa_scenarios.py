"""Bang kich ban PA (chi PHAN TICH, khong tao/doi lenh PAPER).

Truoc phien Au (06:30 UTC) va phien My (12:30 UTC): dung cac "lang kinh" PA
(range phien truoc, cau truc H1/H4, BOS-retest, QML, nen manh/order block, lich tin)
de dung 2-4 kich ban CANH TRANH, moi kich ban co: dieu kien kich hoat, diem vo hieu,
muc tieu, SL tham chieu. Sau do tu cham tren nen M15: kich ban nao kich hoat, cham
muc tieu hay vo hieu -> thong ke do trung theo loai kich ban + boi canh.

Ham thuan (khong mang, khong dong ho) -> cung input = cung output, test duoc.
Nam NGOAI configuration_hash: doi file nay khong reset nghien cuu forward.
"""
from datetime import datetime, timezone, timedelta
import gold_pa_bot as pa

fx = pa.fx
VN_TZ = timezone(timedelta(hours=7))
BOARDS = {   # ten -> (gio UTC dang bang, gio bat dau range tham chieu, gio het han)
    'EU': {'at': (6, 30), 'range_from': (0, 0), 'expire': (12, 30), 'label': 'phiên Âu', 'range_label': 'range phiên Á'},
    'US': {'at': (12, 30), 'range_from': (7, 0), 'expire': (21, 0), 'label': 'phiên Mỹ', 'range_label': 'range phiên Âu'},
}
TYPES = {
    'quet_dao': 'Quét đầu range rồi đảo chiều',
    'pha_that': 'Phá thật range, chạy theo',
    'retest': 'Retest mốc vừa phá (BOS)',
    'qml': 'Quasimodo (QML) sau CHoCH',
    'ob': 'Retest vùng nến mạnh (order block)',
}
REVERSAL = {'quet_dao', 'qml'}
CONTINUATION = {'pha_that', 'retest', 'ob'}


def _day_start(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def due_board(state, now_ts):
    """Ten bang can dang (EU/US) neu da toi gio va hom nay chua dang; None neu khong."""
    if pa.ledger._weekend_gap(now_ts, now_ts + 1):
        return None
    day = _day_start(now_ts)
    done = {(b['name'], b['day']) for b in state.get('boards', [])}
    for name, cfg in BOARDS.items():
        at = day + cfg['at'][0] * 3600 + cfg['at'][1] * 60
        exp = day + cfg['expire'][0] * 3600 + cfg['expire'][1] * 60
        if at <= now_ts < at + 2 * 3600 and now_ts < exp and (name, day) not in done:
            return name
    return None


def _strong_candle(bars, atr, lookback=24):
    """Nen H1 manh gan nhat: than >= 1.5 ATR, dong o 25% cuc tri; vung = open..50% than.
    Chi giu neu CHUA bi cham lai sau do."""
    n = len(bars)
    for i in range(n - 2, max(0, n - 2 - lookback), -1):
        b = bars[i]
        body, rng = abs(b['c'] - b['o']), max(b['h'] - b['l'], 1e-9)
        if body < 1.5 * atr:
            continue
        up = b['c'] > b['o']
        if up and (b['h'] - b['c']) > 0.25 * rng or not up and (b['c'] - b['l']) > 0.25 * rng:
            continue
        lo, hi = (b['o'], b['o'] + body / 2) if up else (b['o'] - body / 2, b['o'])
        later = bars[i + 1:-1]
        touched = any(x['l'] <= hi for x in later) if up else any(x['h'] >= lo for x in later)
        if not touched:
            return {'dir': 'BUY' if up else 'SELL', 'zone': (lo, hi), 'open': b['o'],
                    'extreme': b['h'] if up else b['l'], 't': b['t']}
        return None
    return None


def _recent_break(swings, closes, k, lookback=12):
    """Swing H/L gan nhat bi nen DONG pha trong lookback nen gan day -> (dir, level)."""
    n = len(closes)
    best = None
    for idx, price, typ in swings:
        if idx + k >= n - 1:
            continue
        for j in range(idx + k + 1, n - 1):
            if typ == 'H' and closes[j] > price or typ == 'L' and closes[j] < price:
                if j >= n - 1 - lookback and (best is None or j > best[2]):
                    best = ('BUY' if typ == 'H' else 'SELL', price, j)
                break
    return best


def _qml(swings, closes):
    """Chuoi H, L, HH, roi close < L (CHoCH xuong) -> SELL tai QML = H. Doi xung cho BUY."""
    s = [x for x in swings][-5:]
    for a in range(len(s) - 3):
        h, l, hh = s[a], s[a + 1], s[a + 2]
        if h[2] == 'H' and l[2] == 'L' and hh[2] == 'H' and hh[1] > h[1]:
            if any(c < l[1] for c in closes[hh[0]:-1]):
                return {'dir': 'SELL', 'qml': h[1], 'stop': hh[1], 'target': min(closes[hh[0]:-1])}
        if h[2] == 'L' and l[2] == 'H' and hh[2] == 'L' and hh[1] < h[1]:
            if any(c > l[1] for c in closes[hh[0]:-1]):
                return {'dir': 'BUY', 'qml': h[1], 'stop': hh[1], 'target': max(closes[hh[0]:-1])}
    return None


def build_board(name, h1, m15, now_ts, calendar=None):
    """Dung bang kich ban tu H1 (bars, nen cuoi co the dang hinh thanh) + M15 da dong."""
    cfg = BOARDS[name]
    day = _day_start(now_ts)
    r0 = day + cfg['range_from'][0] * 3600 + cfg['range_from'][1] * 60
    at = day + cfg['at'][0] * 3600 + cfg['at'][1] * 60
    expire = day + cfg['expire'][0] * 3600 + cfg['expire'][1] * 60
    rng = [b for b in m15 if r0 <= b['t'] and b['t'] + 900 <= max(at, now_ts)]
    closes, highs, lows = ([b[k] for b in h1] for k in ('c', 'h', 'l'))
    atr = fx.atr(highs[:-1], lows[:-1], closes[:-1]) or 0
    if len(rng) < 8 or atr <= 0:
        return None
    hi, lo = max(b['h'] for b in rng), min(b['l'] for b in rng)
    mid = (hi + lo) / 2
    price = m15[-1]['c']
    s1 = pa.market_structure(closes[:-1], highs[:-1], lows[:-1], k=pa.SWING_K)
    h4 = pa.closed_h4(h1, now_ts)
    s4 = pa.market_structure([b['c'] for b in h4], [b['h'] for b in h4], [b['l'] for b in h4], k=pa.SWING_K_H4)
    _, is_trend = pa.daily_er_regime(h1)
    swings = pa.find_swings(highs[:-1], lows[:-1], pa.SWING_K)
    news = [e for e in (calendar or {}).get('events', [])
            if e.get('impact') == 'high' and e.get('currency') in ('USD', 'ALL') and at <= e['ts'] <= expire]
    ctx = {'h1': s1['trend'], 'h1_ok': s1['confirmed'], 'h4': s4['trend'], 'h4_ok': s4['confirmed'],
           'regime': 'TREND' if is_trend else 'RANGE', 'news': [e['title'] for e in news][:3],
           'news_ts': news[0]['ts'] if news else None}
    sc = []
    sc.append(dict(type='quet_dao', dir='BUY', trigger='sweep_low', level=lo, target=hi, stop=lo - 0.5 * atr,
                   text=f"Quét đáy {cfg['range_label']} {lo:,.1f} (râu M15 xuống dưới) rồi đóng M15 lại trên → mua, mục tiêu đỉnh range {hi:,.1f}"))
    sc.append(dict(type='quet_dao', dir='SELL', trigger='sweep_high', level=hi, target=lo, stop=hi + 0.5 * atr,
                   text=f"Quét đỉnh {cfg['range_label']} {hi:,.1f} (râu M15 lên trên) rồi đóng M15 lại dưới → bán, mục tiêu đáy range {lo:,.1f}"))
    sc.append(dict(type='pha_that', dir='BUY', trigger='break_up', level=hi, target=hi + atr, stop=mid,
                   text=f"Phá thật đỉnh {hi:,.1f}: 2 nến M15 đóng trên, không quay lại → theo mua, mục tiêu {hi + atr:,.1f}"))
    sc.append(dict(type='pha_that', dir='SELL', trigger='break_down', level=lo, target=lo - atr, stop=mid,
                   text=f"Phá thật đáy {lo:,.1f}: 2 nến M15 đóng dưới, không quay lại → theo bán, mục tiêu {lo - atr:,.1f}"))
    br = _recent_break(swings, closes, pa.SWING_K)
    if br:
        d, lv, _ = br
        s = 1 if d == 'BUY' else -1
        ext = max(highs[-13:-1]) if d == 'BUY' else min(lows[-13:-1])
        if (ext - lv) * s > 0.5 * atr:
            sc.append(dict(type='retest', dir=d, trigger='touch_hold', level=lv, zone=(lv - 0.1 * atr, lv + 0.1 * atr),
                           target=ext, stop=lv - s * 0.5 * atr,
                           text=f"Retest mốc vừa phá {lv:,.1f} (BOS H1) giữ được → {'mua' if d == 'BUY' else 'bán'} theo, mục tiêu {ext:,.1f}"))
    q = _qml(swings, closes)
    if q:
        sc.append(dict(type='qml', dir=q['dir'], trigger='touch_hold', level=q['qml'],
                       zone=(q['qml'] - 0.1 * atr, q['qml'] + 0.1 * atr), target=q['target'],
                       stop=q['stop'] + (0.1 * atr if q['dir'] == 'SELL' else -0.1 * atr),
                       text=f"Giá hồi về QML {q['qml']:,.1f} sau CHoCH → {'bán' if q['dir'] == 'SELL' else 'mua'}, mục tiêu {q['target']:,.1f}"))
    ob = _strong_candle(h1, atr)
    if ob:
        s = 1 if ob['dir'] == 'BUY' else -1
        sc.append(dict(type='ob', dir=ob['dir'], trigger='touch_hold', level=ob['open'], zone=ob['zone'],
                       target=ob['extreme'], stop=ob['open'] - s * 0.5 * atr,
                       text=f"Giá về vùng nến mạnh {ob['zone'][0]:,.1f}–{ob['zone'][1]:,.1f} lần đầu, giữ được → {'mua' if s == 1 else 'bán'}, mục tiêu {ob['extreme']:,.1f}"))
    # Bo kich ban vo nghia (muc tieu nam sai phia, hoac gia da vuot muc tieu)
    sc = [x for x in sc if (x['target'] - x['level']) * (1 if x['dir'] == 'BUY' else -1) > 0.3 * atr
          and (x['target'] - price) * (1 if x['dir'] == 'BUY' else -1) > 0]
    for x in sc:
        want = 'UP' if x['dir'] == 'BUY' else 'DOWN'
        why = []
        if ctx['h4'] == want:
            why.append('H4 thuận' + (' (đã xác nhận)' if ctx['h4_ok'] else ''))
        if ctx['h1'] == want:
            why.append('H1 thuận')
        if x['type'] in REVERSAL and ctx['regime'] == 'RANGE':
            why.append('ngày đi ngang hợp đảo chiều')
        if x['type'] in CONTINUATION and ctx['regime'] == 'TREND':
            why.append('ngày có trend hợp đi theo')
        if x['type'] == 'pha_that' and news:
            why.append('có tin mạnh — phá thật hay trùng tin')
        if x['type'] == 'quet_dao' and not news:
            why.append('không có tin mạnh — dễ là phá giả')
        x['score'] = len(why) + (1 if ctx['h4'] == want and ctx['h4_ok'] else 0)
        x['why'] = why
        x.update(status='WAITING', id=f"{name}-{int(day)}-{len(sc)}-{x['type']}-{x['dir']}")
    sc.sort(key=lambda x: (-x['score'], x['type']))
    sc = sc[:5]
    return {'name': name, 'day': day, 'created_at': now_ts, 'expire': expire, 'atr': atr,
            'range': {'hi': hi, 'lo': lo, 'mid': mid, 'from': r0, 'label': cfg['range_label']},
            'price': price, 'ctx': ctx, 'scenarios': sc, 'status': 'OPEN', 'pick': None}


def _step(x, b, board):
    """Cap nhat 1 kich ban voi 1 nen M15 DA DONG."""
    s = 1 if x['dir'] == 'BUY' else -1
    pad = 0.05 * board['atr']
    if x['status'] == 'WAITING':
        hit = False
        if x['trigger'] == 'sweep_low':
            x['swept'] = x.get('swept') or b['l'] < x['level'] - pad
            hit = x['swept'] and b['c'] > x['level']
        elif x['trigger'] == 'sweep_high':
            x['swept'] = x.get('swept') or b['h'] > x['level'] + pad
            hit = x['swept'] and b['c'] < x['level']
        elif x['trigger'] in ('break_up', 'break_down'):
            out = b['c'] > x['level'] if x['trigger'] == 'break_up' else b['c'] < x['level']
            x['run'] = x.get('run', 0) + 1 if out else 0
            hit = x['run'] >= 2
        else:   # touch_hold: cham vung roi dong nen giu phia dung
            lo, hi = x['zone']
            touched = b['l'] <= hi if s == 1 else b['h'] >= lo
            hit = touched and (b['c'] >= lo if s == 1 else b['c'] <= hi)
            if touched and not hit:
                x.update(status='INVALIDATED', closed_at=b['t'] + 900); return
        if hit:
            x.update(status='TRIGGERED', triggered_at=b['t'] + 900, entry=b['c'])
            if (x['stop'] - b['c']) * s >= 0:          # kich hoat ma da qua SL tham chieu
                x.update(status='INVALIDATED', closed_at=b['t'] + 900)
            return
        # vo hieu truoc khi kich hoat: 2 nen dong qua SL tham chieu
        beyond = (x['stop'] - b['c']) * s >= 0
        x['beyond'] = x.get('beyond', 0) + 1 if beyond else 0
        if x['beyond'] >= 2:
            x.update(status='INVALIDATED', closed_at=b['t'] + 900)
        return
    if x['status'] == 'TRIGGERED':
        hit_t = b['h'] >= x['target'] if s == 1 else b['l'] <= x['target']
        hit_s = b['l'] <= x['stop'] if s == 1 else b['h'] >= x['stop']
        risk = max(abs(x['entry'] - x['stop']), 1e-9)
        if hit_t and hit_s:
            x.update(status='AMBIGUOUS', closed_at=b['t'] + 900)
        elif hit_t:
            x.update(status='HIT', closed_at=b['t'] + 900, r=abs(x['target'] - x['entry']) / risk)
        elif hit_s:
            x.update(status='FAILED', closed_at=b['t'] + 900, r=-1.0)


def resolve(board, m15, now_ts):
    """Chay cac nen M15 da dong tu luc dang bang toi het han. Tra True neu bang vua dong."""
    if board['status'] != 'OPEN':
        return False
    last = board.get('last_bar_t', board['created_at'] - 900)
    for b in m15:
        if b['t'] <= board['created_at'] - 900 or b['t'] <= last or b['t'] + 900 > min(now_ts, board['expire']):
            continue
        for x in board['scenarios']:
            if x['status'] in ('WAITING', 'TRIGGERED'):
                _step(x, b, board)
        board['last_bar_t'] = b['t']
        board['last_price'] = b['c']
    if now_ts >= board['expire']:
        for x in board['scenarios']:
            if x['status'] == 'WAITING':
                x['status'] = 'NOT_TRIGGERED'
            elif x['status'] == 'TRIGGERED':
                px = board.get('last_price', x['entry'])
                x['status'] = 'EXPIRED_OPEN'
                x['r_at_expiry'] = (px - x['entry']) * (1 if x['dir'] == 'BUY' else -1) / max(abs(x['entry'] - x['stop']), 1e-9)
                x['r'] = x['r_at_expiry']
        move = board.get('last_price', board['price']) - board['price']
        board['actual_dir'] = 'BUY' if move > 0.25 * board['atr'] else 'SELL' if move < -0.25 * board['atr'] else 'FLAT'
        board['status'] = 'CLOSED'
        return True
    return False


def stats(state):
    """Do trung theo loai kich ban (chi tinh kich ban DA kich hoat) + do trung cua 'nghieng'."""
    out = {}
    picks = {'n': 0, 'right': 0, 'flat': 0}
    for bd in state.get('boards', []):
        if bd['status'] != 'CLOSED':
            continue
        if bd.get('pick'):
            picks['n'] += 1
            picks['right'] += bd.get('actual_dir') == bd['pick']
            picks['flat'] += bd.get('actual_dir') == 'FLAT'
        for x in bd['scenarios']:
            k = f"{x['type']}|{bd['ctx']['regime']}"
            s = out.setdefault(k, {'type': x['type'], 'regime': bd['ctx']['regime'], 'n': 0,
                                   'triggered': 0, 'hit': 0, 'failed': 0, 'open': 0, 'n_r': 0, 'sum_r': 0.0})
            if isinstance(x.get('r'), (int, float)):
                s['n_r'] += 1; s['sum_r'] += x['r']
            s['n'] += 1
            if x['status'] in ('HIT', 'FAILED', 'EXPIRED_OPEN', 'AMBIGUOUS'):
                s['triggered'] += 1
            s['hit'] += x['status'] == 'HIT'
            s['failed'] += x['status'] == 'FAILED'
            s['open'] += x['status'] == 'EXPIRED_OPEN'
    for s in out.values():
        s['avg_r'] = round(s['sum_r'] / s['n_r'], 4) if s['n_r'] else None
        s['sum_r'] = round(s['sum_r'], 4)
    return {'by_type': sorted(out.values(), key=lambda s: (s['type'], s['regime'])), 'pick': picks}


MIN_HIST = 30


def merge(*stats_list):
    """Cong don thong ke (vd backtest 'prior' + live) theo loai x boi canh."""
    acc = {}
    for st in stats_list:
        for s in (st or {}).get('by_type', []):
            k = (s['type'], s['regime'])
            t = acc.setdefault(k, {'type': s['type'], 'regime': s['regime'], 'n': 0, 'triggered': 0,
                                   'hit': 0, 'failed': 0, 'open': 0, 'n_r': 0, 'sum_r': 0.0})
            for f in ('n', 'triggered', 'hit', 'failed', 'open', 'n_r', 'sum_r'):
                t[f] += s.get(f, 0) or 0
    for t in acc.values():
        t['avg_r'] = round(t['sum_r'] / t['n_r'], 4) if t['n_r'] else None
    out = {'by_type': sorted(acc.values(), key=lambda s: (s['type'], s['regime']))}
    for st in stats_list:
        if (st or {}).get('pick_skill'):
            out['pick_skill'] = st['pick_skill']
    return out


def hist_of(st, typ, regime):
    for s in (st or {}).get('by_type', []):
        if s['type'] == typ and s['regime'] == regime:
            return s
    return None


def rank(board, prior=None):
    """Xep kich ban: uu tien KY VONG R lich su (>= MIN_HIST lan da phan dinh) cua dung loai x
    boi canh; chua du lich su thi dung diem bang chung. 'Nghieng' = kich ban dau neu ky vong
    lich su > 0, hoac (khong co lich su) diem bang chung hon han kich ban thu hai."""
    reg = board['ctx']['regime']
    for x in board['scenarios']:
        h = hist_of(prior, x['type'], reg)
        x['hist'] = ({'n': h['n_r'], 'avg_r': h['avg_r'], 'hit': h['hit'], 'done': h['hit'] + h['failed']}
                     if h and h['n_r'] >= MIN_HIST else None)
    key = lambda x: (x['hist']['avg_r'] if x['hist'] else -9, x['score'])
    board['scenarios'].sort(key=key, reverse=True)
    sc = board['scenarios']
    if sc and sc[0]['hist']:
        board['pick'] = sc[0]['dir'] if sc[0]['hist']['avg_r'] > 0 else None
        board['pick_basis'] = 'history'
    else:
        board['pick'] = sc[0]['dir'] if sc and (len(sc) == 1 or sc[0]['score'] > sc[1]['score']) else None
        board['pick_basis'] = 'evidence'
    for i, x in enumerate(sc):
        x['id'] = f"{board['name']}-{datetime.fromtimestamp(board['day'], timezone.utc):%Y%m%d}-{i}"
    return board


def _vn(ts, fmt='%H:%M'):
    return datetime.fromtimestamp(ts, VN_TZ).strftime(fmt)


def _hist(st, x, regime):
    h = x.get('hist')
    if h:
        return f" · lịch sử: đúng {h['hit']}/{h['done']}, kỳ vọng {h['avg_r']:+.2f}R/lần"
    return ' · chưa đủ lịch sử'


def board_message(board, st=None):
    st = st or {'by_type': []}
    c, r = board['ctx'], board['range']
    trend = {'UP': 'tăng', 'DOWN': 'giảm', 'RANGE': 'đi ngang'}
    lines = [f"🧭 <b>BẢNG KỊCH BẢN {BOARDS[board['name']]['label'].upper()}</b> — {_vn(board['created_at'], '%H:%M %d/%m')} (giờ VN)",
             f"Giá {board['price']:,.1f} · {r['label']} {r['lo']:,.1f}–{r['hi']:,.1f} (giữa {r['mid']:,.1f})",
             f"Bối cảnh: H4 {trend[c['h4']]}{' (xác nhận)' if c['h4_ok'] else ''} · H1 {trend[c['h1']]} · ngày {'có trend' if c['regime'] == 'TREND' else 'đi ngang'}"
             + (f" · ⚠ tin mạnh: {', '.join(c['news'])} lúc {_vn(c['news_ts'])}" if c['news'] else ' · không có tin mạnh USD'),
             '']
    for i, x in enumerate(board['scenarios']):
        tag = '⭐ ' if i == 0 and board.get('pick') else ''
        lines.append(f"{tag}<b>{chr(65 + i)}. {TYPES[x['type']]}</b> ({'MUA' if x['dir'] == 'BUY' else 'BÁN'}) — bằng chứng {x['score']}{_hist(st, x, c['regime'])}")
        lines.append(f"   {x['text']}. Vô hiệu nếu qua {x['stop']:,.1f}.")
        if x['why']:
            lines.append(f"   Vì: {'; '.join(x['why'])}.")
    lines.append('')
    basis = 'theo kỳ vọng lịch sử' if board.get('pick_basis') == 'history' else 'theo bằng chứng (chưa đủ lịch sử)'
    lines.append(f"Nghiêng (thử nghiệm): <b>{'MUA' if board['pick'] == 'BUY' else 'BÁN' if board['pick'] == 'SELL' else 'chưa rõ — chờ giá chọn kịch bản'}</b>"
                 f"{' (' + basis + ')' if board['pick'] else ''}. Hết hiệu lực {_vn(board['expire'])}.")
    skill = (st or {}).get('pick_skill')
    if skill and skill.get('n'):
        lines.append(f"⚠ Backtest {skill['period']}: 'nghiêng' đúng hướng {skill['right']}/{skill['n']} "
                     f"({skill['right'] / skill['n']:.0%}) — chưa hơn may rủi. Hãy để giá xác nhận kịch bản trước.")
    lines.append('<i>Chỉ là phân tích để tham khảo, không phải lệnh. Bot sẽ tự chấm kịch bản nào đúng.</i>')
    return '\n'.join(lines)


def result_message(board, st):
    icon = {'HIT': '✅ chạm mục tiêu', 'FAILED': '❌ chạm điểm vô hiệu sau khi kích hoạt',
            'INVALIDATED': '⚪ bị vô hiệu trước khi kích hoạt', 'NOT_TRIGGERED': '⚪ không xảy ra',
            'EXPIRED_OPEN': '⏱ kích hoạt nhưng hết giờ chưa tới đích', 'AMBIGUOUS': '❔ không rõ (cùng nến M15)'}
    lines = [f"📋 <b>CHẤM BẢNG KỊCH BẢN {BOARDS[board['name']]['label'].upper()}</b>"]
    for i, x in enumerate(board['scenarios']):
        extra = f" ({x['r_at_expiry']:+.2f}R)" if x['status'] == 'EXPIRED_OPEN' else ''
        lines.append(f"{chr(65 + i)}. {TYPES[x['type']]} {'MUA' if x['dir'] == 'BUY' else 'BÁN'}: {icon[x['status']]}{extra}")
    if board.get('pick'):
        ok = board['actual_dir'] == board['pick']
        lines.append(f"Nghiêng {'MUA' if board['pick'] == 'BUY' else 'BÁN'} → giá thực tế "
                     f"{ {'BUY': 'tăng', 'SELL': 'giảm', 'FLAT': 'đi ngang'}[board['actual_dir']] }: {'✅ đúng' if ok else '❌ sai'}")
    p = st['pick']
    if p['n']:
        lines.append(f"Luỹ kế độ đúng của 'nghiêng': {p['right']}/{p['n']} ({p['right'] / p['n']:.0%}).")
    return '\n'.join(lines)
