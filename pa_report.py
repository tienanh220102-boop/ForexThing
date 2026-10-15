"""Telegram text for PA PAPER results: per-order reply + weekly/monthly summary.

Pure formatting/statistics over the ledger (no network, no clock) so the
numbers can be unit-tested and stay identical for the same state.
Week = Saturday 00:00 UTC -> next Saturday 00:00 UTC (gold is closed then).
Month = calendar month in Vietnam time (UTC+7).
"""
from datetime import datetime, timezone, timedelta
import pa_ledger as ledger

VN_TZ = timezone(timedelta(hours=7))
WEEK = 7 * 86400

TIER_LABELS = {1: 'Cấp 1 (mạnh)', 2: 'Cấp 2 (trung bình)', 3: 'Cấp 3 (dò đường)'}
TIER_NOTES = {1: 'mốc giá mạnh (≥3 lần chạm) + phiên Á',
              2: 'đủ điều kiện, chưa có điểm cộng đặc biệt',
              3: 'trend chưa được cấu trúc xác nhận - lệnh dò, có thể là lúc bật trend'}
ALL = object()


def tier_of(p):
    """Same precedence as build_order sizing: probe/danger first, then high-conviction."""
    if p.get('probe') or p.get('danger'):
        return 3
    return 1 if p.get('high_conviction') else 2


def tier_label(rec):
    return TIER_LABELS.get(rec.get('tier'), 'Chưa phân cấp')


def tier_line(p):
    t = p.get('tier')
    return f"<b>{tier_label(p)}</b>" + (f" - {TIER_NOTES[t]}" if t in TIER_NOTES else '')


WIN_LABELS = {'TP1': 'CHẠM TP1', 'TP2': 'CHẠM TP2', 'SL': 'DÍNH SL', 'BE': 'VỀ HÒA VỐN',
              'TIMEOUT': 'HẾT 5 NGÀY - ĐÓNG THEO GIÁ', 'TRAIL': 'CHẠM SL KÉO THEO',
              'TIME_STOP': 'DỪNG THEO THỜI GIAN'}
CANCEL_LABELS = {
    'NOFILL': 'Giá không về tới giá limit trước khi lệnh hết hạn.',
    'CANCELLED_STRUCTURE': 'Giá phá điểm vô hiệu của setup trước khi khớp.',
    'CANCELLED_TARGET': 'Giá chạy tới TP1 trước khi khớp, bỏ lệnh để không đuổi giá.',
    'CANCELLED_CONTEXT': 'Khung H4 đổi hướng ngược lệnh trước khi khớp.',
    'CANCELLED_LEVEL_LOST': 'Mốc giá của setup đã mất hiệu lực lúc khớp.',
    'CANCELLED_PRICE_MOVED': 'Giá lúc khớp lệch quá xa giá phân tích.',
    'CANCELLED_LOW_RR': 'Tỉ lệ lời/lỗ sau chi phí không còn đủ lúc khớp.',
    'CANCELLED_STALE_SETUP': 'Setup đã quá cũ lúc giá về tới.',
}
UNSCORED_LABELS = {
    'AMBIGUOUS': 'SL và TP cùng bị chạm trong một nến M1, không xác định được cái nào trước.',
    'AMBIGUOUS_CANCEL_FILL': 'Khớp và điều kiện huỷ xảy ra trong cùng một nến M1.',
    'DATA_GAP': 'Thiếu dữ liệu nến M1 nên không đối soát được.',
    'SOURCE_CHANGED': 'Nguồn giá bị đổi giữa chừng.',
    'UNKNOWN_EXPIRY': 'Giá chạm limit đúng nến hết hạn, không chắc đã khớp kịp.',
    'INVALID_FILL': 'Giá khớp làm SL/TP sai vị trí.',
}


def _vn(ts, fmt='%d/%m %H:%M'):
    return datetime.fromtimestamp(ts, VN_TZ).strftime(fmt)


def _duration(seconds):
    m = int(seconds // 60)
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    return (f'{d} ngày ' if d else '') + (f'{h} giờ ' if h else '') + f'{m} phút'


def _exit_event(rec):
    for e in reversed(rec.get('events', [])):
        if e.get('type') == rec.get('outcome') and 'price' in e:
            return e
    return None


def month_start(ts):
    d = datetime.fromtimestamp(ts, VN_TZ)
    return datetime(d.year, d.month, 1, tzinfo=VN_TZ).timestamp()


def period_stats(state, start, end, tier=ALL):
    """Orders issued in [start, end) and results closed in [start, end)."""
    recs = [r for r in state.get('signals', []) if r.get('ledger_version') == ledger.LEDGER_VERSION
            and (tier is ALL or r.get('tier') == tier)]
    closed = [r for r in recs if r.get('outcome') and start <= r.get('closed_ts', -1) < end]
    scored = {id(r) for r in ledger.scored(state)}
    rows = sorted((r for r in closed if id(r) in scored), key=lambda r: (r['closed_ts'], r['ts']))
    rs = [r['net_r'] for r in rows]
    equity = peak = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    wins = sum(r > 1e-9 for r in rs)
    losses = sum(r < -1e-9 for r in rs)
    return {'issued': sum(start <= r['ts'] < end for r in recs),
            'filled': sum(start <= r.get('filled_ts', -1) < end for r in recs),
            'n': len(rs), 'wins': wins, 'losses': losses, 'flat': len(rs) - wins - losses,
            'tp': sum(r['outcome'] in ('TP1', 'TP2') for r in rows),
            'sl': sum(r['outcome'] == 'SL' for r in rows),
            'no_trade': sum(r['outcome'] in ledger.NON_TRADES for r in closed),
            'unscored': sum(r['outcome'] not in ledger.NON_TRADES | ledger.SCORED for r in closed),
            'total_r': sum(rs), 'avg_r': sum(rs) / len(rs) if rs else 0.0,
            'win_rate': wins / len(rs) if rs else None, 'max_dd_r': dd,
            'best_r': max(rs) if rs else None, 'worst_r': min(rs) if rs else None}


def _tally(s):
    return f"{s['wins']}W/{s['losses']}L" + (f"/{s['flat']} hòa" if s['flat'] else '') + f" · {s['total_r']:+.2f}R"


def result_message(rec, state):
    out = rec['outcome']
    head = f"{rec['setup']} | {rec['dir']} | {tier_label(rec)}"
    lines = []
    if out in ledger.SCORED:
        net = rec['net_r']
        icon = '✅' if net > 1e-9 else '❌' if net < -1e-9 else '⚪'
        ex = _exit_event(rec)
        lines += [f"{icon} <b>KẾT QUẢ: {WIN_LABELS.get(out, out)}</b> | {head}",
                  f"Khớp {rec['entry']:.2f} lúc {_vn(rec['filled_ts'])} (giờ VN)"]
        if ex:
            lines.append(f"Thoát {ex['price']:.2f} lúc {_vn(ex['ts'])}")
        lines += [f"Kết quả: <b>{net:+.2f}R</b> sau chi phí ({rec['pips']:+.1f} pip)",
                  f"Giữ lệnh {_duration(rec['closed_ts'] - rec['filled_ts'])}"]
    elif out in ledger.NON_TRADES:
        title = 'KHÔNG KHỚP' if out == 'NOFILL' else 'HUỶ LỆNH'
        lines += [f"⚪ <b>KẾT QUẢ: {title}</b> | {head}",
                  CANCEL_LABELS.get(out, out),
                  f"Limit {rec['entry_ref']:.2f} · đóng lúc {_vn(rec['closed_ts'])} (giờ VN)",
                  'Không tính vào thắng/thua.']
    else:
        lines += [f"⚠️ <b>KẾT QUẢ: KHÔNG CHẤM ĐIỂM</b> | {head}",
                  UNSCORED_LABELS.get(out, out),
                  f"Đóng lúc {_vn(rec['closed_ts'])} (giờ VN) · không tính vào thắng/thua."]
    ms = month_start(rec['closed_ts'])
    lines.append(f"Luỹ kế tháng {_vn(ms, '%m/%Y')}: {_tally(period_stats(state, ms, rec['closed_ts'] + 1))}")
    lines.append('<i>PAPER - mô phỏng, không phải lệnh tiền thật.</i>')
    return '\n'.join(lines)


def week_end(now_ts):
    """Most recent Saturday 00:00 UTC at or before now."""
    d = datetime.fromtimestamp(now_ts, timezone.utc)
    sat = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - timedelta(days=(d.weekday() - 5) % 7)
    return sat.timestamp()


def weekly_due(last_weekly, now_ts):
    end = week_end(now_ts)
    return end if (last_weekly or 0) < end else None


def _tier_lines(state, start, end):
    tiers = [1, 2, 3] + ([None] if any(r.get('tier') not in TIER_LABELS
                                       for r in state.get('signals', [])) else [])
    lines = []
    for t in tiers:
        s = period_stats(state, start, end, t)
        if not s['issued'] and not s['n'] and not s['no_trade'] and not s['unscored']:
            continue
        wr = f" ({s['win_rate']:.0%})" if s['n'] else ''
        lines.append(f"• {TIER_LABELS.get(t, 'Chưa phân cấp')}: phát {s['issued']} · khớp {s['filled']}"
                     f" · {_tally(s)}{wr}")
    return ['Theo cấp:'] + lines if lines else []


def _block(title, s):
    if not s['issued'] and not s['n'] and not s['no_trade'] and not s['unscored']:
        return [f'<b>{title}</b>', 'Không có lệnh nào.']
    lines = [f'<b>{title}</b>',
             f"Phát {s['issued']} lệnh · khớp {s['filled']} · không khớp/huỷ {s['no_trade']}"
             + (f" · không chấm {s['unscored']}" if s['unscored'] else ''),
             f"Đã chốt {s['n']}: {s['tp']} TP · {s['sl']} SL" + (f" · {s['n']-s['tp']-s['sl']} khác" if s['n']-s['tp']-s['sl'] else '')]
    if s['n']:
        lines += [f"Thắng {s['wins']}/{s['n']} ({s['win_rate']:.0%}) · tổng <b>{s['total_r']:+.2f}R</b> · TB {s['avg_r']:+.2f}R/lệnh",
                  f"Tốt nhất {s['best_r']:+.2f}R · tệ nhất {s['worst_r']:+.2f}R · sụt giảm tối đa {s['max_dd_r']:.2f}R"]
    return lines


def weekly_message(state, end):
    start = end - WEEK
    ms = month_start(end - 1)
    lines = ['📊 <b>PA PAPER - BÁO CÁO TUẦN</b>', '']
    periods = [(f"Tuần {_vn(start, '%d/%m')} - {_vn(end - 86400, '%d/%m/%Y')}", start, end)]
    if ms > start:  # week crosses a month boundary: close the previous month too
        pms = month_start(ms - 1)
        periods.append((f"Tháng {_vn(pms, '%m/%Y')} (chốt cả tháng)", pms, ms))
    periods.append((f"Tháng {_vn(ms, '%m/%Y')} (từ đầu tháng đến nay)", ms, end))
    for i, (title, a, b) in enumerate(periods):
        lines += ([''] if i else []) + _block(title, period_stats(state, a, b)) + _tier_lines(state, a, b)
    lines += ['', '<i>R = bội số rủi ro, đã trừ chi phí 0.40 USD/oz. Mô phỏng, không phải P&L broker.</i>']
    return '\n'.join(lines)
