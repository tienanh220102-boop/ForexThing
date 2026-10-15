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
SPRT_MIN_N = 5   # trung voi gold_pa_bot._sprt_verdict (duoi 5 lenh -> CHUA_DU)


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


def signal_message(p, session_lbl, now, cost_price):
    """Noi dung tin lenh PAPER (chuyen tu gold_pa_bot.send_signal, giu nguyen chu).
    Nam o day de doi chu khong lam doi configuration_hash cua nghien cuu forward."""
    lines = ['🧪 <b>PA PAPER - chi theo doi, khong vao lenh tien that</b>',
             tier_line(p),
             f'{p["setup"]} | {p["dir"]} | phien {session_lbl}',
             f'Gia tham chieu {p["entry"]:.2f} | SL {p["sl"]:.2f} | TP1 {p["tp1"]:.2f}',
            (f'Limit gia lap; het han sau {p.get("limit_ttl", 21600)/3600:g} gio, huy khi setup mat hieu luc.' if p.get('entry_type') == 'limit'
              else 'Market gia lap khop tai OPEN phut day du tiep theo; gia khop co the khac.'),
             'Chot 100% tai TP1. TP2 chi de nghien cuu; khong co runner/nhoi lenh.',
             f'Chi phi gia lap: {cost_price:.2f} USD/oz/lenh. Khong phai P&L broker.',
             f'UTC: {now.isoformat()}']
    ttl_end = now.timestamp() + p.get('limit_ttl', 21600)
    if p.get('entry_type') == 'limit' and p.get('calendar_event') and p.get('calendar_valid_until', ttl_end) < ttl_end:
        lines.insert(-1, f"Huỷ sớm lúc {_vn(p['calendar_valid_until'], '%H:%M %d/%m')} (giờ VN), 1 giờ trước tin mạnh: {p['calendar_event']}.")
    if p.get('rule_version'):
        lines.insert(-1, f'RR sau chi phi: {p["net_reward_risk"]:.2f}; gia thay doi/khong du RR thi huy khop PAPER.')
    return '\n'.join(lines)


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
        news_cut = (out == 'NOFILL' and rec.get('calendar_event')
                    and rec['closed_ts'] == rec.get('calendar_valid_until')
                    and rec['closed_ts'] < rec['ts'] + rec.get('limit_ttl', 21600))
        title = 'HUỶ LỆNH' if news_cut or out != 'NOFILL' else 'KHÔNG KHỚP'
        lines += [f"⚪ <b>KẾT QUẢ: {title}</b> | {head}",
                  f"Huỷ lệnh chờ 1 giờ trước tin mạnh: {rec['calendar_event']}." if news_cut
                  else CANCEL_LABELS.get(out, out),
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


def sprt_line(sprt):
    """1 dong tieng Viet cho ket qua SPRT (Wald) tren toan bo lenh v2 da cham diem."""
    if not sprt:
        return None
    n, llr, up, lo = sprt['n'], sprt['llr'], sprt['upper'], sprt['lower']
    if sprt['verdict'] == 'CONFIRM_EDGE':
        return (f"🔬 Kiểm định SPRT: <b>có bằng chứng hệ thống có lợi thế</b> "
                f"(n={n}, LLR {llr:+.2f} ≥ ngưỡng {up:+.2f}). Vẫn chỉ là mô phỏng PAPER.")
    if sprt['verdict'] == 'REJECT_EDGE':
        return (f"🔬 Kiểm định SPRT: <b>có bằng chứng hệ thống KHÔNG có lợi thế</b> "
                f"(n={n}, LLR {llr:+.2f} ≤ ngưỡng {lo:+.2f}).")
    if n < SPRT_MIN_N:
        return f"🔬 Kiểm định SPRT: chưa đủ dữ liệu (mới {n} lệnh đã chấm, cần tối thiểu {SPRT_MIN_N})."
    return (f"🔬 Kiểm định SPRT: chưa đủ dữ liệu để kết luận (n={n}, LLR {llr:+.2f}; "
            f"cần ≥ {up:+.2f} để xác nhận có lợi thế, ≤ {lo:+.2f} để bác bỏ).")


def weekly_message(state, end, sprt=None):
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
    sprt_text = sprt_line(sprt)
    if sprt_text:
        lines += ['', sprt_text]
    lines += ['', '<i>R = bội số rủi ro, đã trừ chi phí 0.40 USD/oz. Mô phỏng, không phải P&L broker.</i>']
    return '\n'.join(lines)


# ── Snapshot cho dashboard: state['pa_report'] (hop dong BOT ghi, DASH doc) ──
def _period(state, label, start, end):
    # end = thoi diem chup; +1s de lenh dong dung giay chup van duoc tinh (cua so nua mo)
    stop = end + 1
    tiers = {str(t): period_stats(state, start, stop, t) for t in (1, 2, 3)}
    tiers['none'] = period_stats(state, start, stop, None)
    return {'label': label, 'start': start, 'end': end,
            'stats': period_stats(state, start, stop), 'tiers': tiers}


def all_period(state, now_ts):
    """Toan bo so lenh v2 (start=0)."""
    return _period(state, 'Từ khi bắt đầu sổ lệnh v2', 0, now_ts)


def snapshot(state, now_ts, sprt):
    """Ham thuan: tuan (tu T7 00:00 UTC gan nhat), thang (theo gio VN), toan bo so v2 + SPRT."""
    ws, ms = week_end(now_ts), month_start(now_ts)
    return {'generated_at': now_ts,
            'week': _period(state, f"Tuần {_vn(ws, '%d/%m')} - {_vn(now_ts, '%d/%m/%Y')}", ws, now_ts),
            'month': _period(state, f"Tháng {_vn(ms, '%m/%Y')}", ms, now_ts),
            'all': all_period(state, now_ts),
            'sprt': dict(sprt)}
