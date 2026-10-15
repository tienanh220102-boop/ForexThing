"""Gui tin Telegram cho PA PAPER (ket qua lenh, gia han nghien cuu, canh bao mu du lieu).

Tach khoi gold_pa_bot.py de doi chu/thong bao KHONG lam doi configuration_hash
(pa_controls.HASHED_FILES) -> khong reset nghien cuu forward 60 ngay.
Moi ham nhan `send` (mac dinh forex_notifier.send_telegram o phia goi) de test duoc.
"""
from datetime import datetime, timezone, timedelta
import logging

import pa_ledger as ledger
import pa_report

log = logging.getLogger('gold_pa')
VN_TZ = timezone(timedelta(hours=7))

# Canh bao mu du lieu: 4 run lien tiep (~1 gio) trong gio thi truong mo
WATCHDOG_ALERT_AFTER = 4
# Luoi thu 2: tu choi NGAT QUANG (vd chi run :00 bi loi nhu 06-12/10) khong bao gio
# tao chuoi lien tiep -> dem so run bi tu choi trong 24h gio mo cua.
WATCHDOG_RATE_WINDOW = 86400
WATCHDOG_RATE_ALERT = 6

DATA_REASONS = {
    'H1_FETCH_FAILED': 'không tải được nến H1',
    'NOT_ENOUGH_BARS': 'không đủ số nến H1 để phân tích',
    'MARKET_STALE': 'nến H1 mới nhất quá cũ',
    'SANITY_FAIL': 'giá bất thường, nằm ngoài khoảng hợp lệ',
    'ATR_INVALID': 'không tính được ATR từ dữ liệu',
    'EXECUTION_DATA_REJECTED': 'dữ liệu nến M1 bị trễ hoặc không dùng được',
}


def _vn(ts, fmt='%H:%M %d/%m'):
    return datetime.fromtimestamp(ts, VN_TZ).strftime(fmt)


def _safe(send, text, **kw):
    try:
        return send(text, **kw) or {}
    except Exception as e:  # loi mang khong duoc lam hong run
        log.info('TG_SEND_FAIL %s', e)
        return {'ok': False}


# ── Ket qua lenh (chuyen tu gold_pa_bot.notify_results, giu nguyen hanh vi) ──
def notify_results(state, send, now_ts=None):
    """Reply ket qua vao dung tin lenh goc; that bai thi thu lai o run sau."""
    sent = 0
    for rec in state.get('signals', []):
        if not rec.get('outcome') or rec.get('result_sent_at') or rec.get('ledger_version') != ledger.LEDGER_VERSION:
            continue
        text = pa_report.result_message(rec, state)
        result = send(text, reply_to=rec.get('message_id'))
        # Tin goc bi xoa / khong reply duoc -> van gui ket qua, khong reply
        if not result.get('ok') and rec.get('message_id'):
            result = send(text)
        if result.get('ok'):
            rec['result_sent_at'] = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
            rec['result_message_id'] = result.get('result', {}).get('message_id')
            sent += 1
        else:
            log.info('RESULT_TG_FAIL %s %s', rec.get('trade_id'), result)
    return sent


# ── Gia han nghien cuu forward (pa_controls.study ghi study['extensions']) ──
def extension_message(study, ext):
    plan = study['plan']
    per = ext.get('per_setup', {})
    setups = ' · '.join(f"{k} {per.get(k, 0)}/{plan['min_per_setup']}" for k in sorted(per))
    lines = ['🗓️ <b>PA PAPER - GIA HẠN NGHIÊN CỨU FORWARD</b>',
             f"Hạn đánh giá dời từ {_vn(ext['old_ends_at'], '%d/%m/%Y')} sang "
             f"<b>{_vn(ext['new_ends_at'], '%d/%m/%Y')}</b> (giờ VN).",
             f"Lý do: chưa đủ mẫu - mới {ext.get('resolved', 0)}/{plan['min_resolved']} lệnh đã chấm"
             + (f" ({setups})" if setups else '') + '.',
             'Không có vấn đề về hiệu suất hay chất lượng dữ liệu, nên bot vẫn phát lệnh PAPER bình thường.',
             f"Tối đa 365 ngày: hạn chót cuối cùng {_vn(study['started_at'] + 365 * 86400, '%d/%m/%Y')}.",
             '<i>Chỉ để đánh giá, không bao giờ tự động chuyển sang tiền thật.</i>']
    return '\n'.join(lines)


def notify_extensions(state, send, now_ts=None):
    """Moi lan gia han gui dung 1 tin; gui loi thi thu lai run sau (danh dau notified_at)."""
    studies = state.get('forward_studies') or []
    if not studies:
        return 0
    study = studies[-1]
    sent = 0
    for ext in study.get('extensions', []):
        if ext.get('notified_at'):
            continue
        if _safe(send, extension_message(study, ext)).get('ok'):
            ext['notified_at'] = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
            sent += 1
    return sent


# ── Canh bao bot "mu" vi du lieu ──
def market_closed(ts):
    """Cung quy uoc voi pa_ledger._weekend_gap: T6 >=22:00, ca T7, CN <21:00 UTC."""
    return ledger._weekend_gap(ts, ts + 1)


def _reason_text(code, detail=''):
    return DATA_REASONS.get(code, code) + (f' ({detail})' if detail else '')


def data_watchdog(state, now_ts, reason, send, detail=''):
    """reason=None: run co du lieu tot. Nguoc lai: ma ly do du lieu bi tu choi.
    Chi dem trong gio thi truong mo; ngoai gio khong dem va khong reset."""
    wd = state.setdefault('data_watchdog', {'consecutive': 0, 'since': None,
                                            'last_reason': None, 'alerted': False})
    if market_closed(now_ts):
        return None
    recent = [x for x in wd.get('recent', []) if now_ts - x[0] < WATCHDOG_RATE_WINDOW]
    recent.append([now_ts, 0 if reason is None else 1])
    wd['recent'] = recent
    if reason is None:
        sent = None
        if wd.get('alerted') and wd.get('since') is not None:
            blind = now_ts - wd['since']
            text = ('✅ <b>PA PAPER đã có dữ liệu trở lại</b>\n'
                    f"Thời gian mù dữ liệu: {pa_report._duration(blind)} "
                    f"(từ {_vn(wd['since'])} đến {_vn(now_ts)} giờ VN), "
                    f"{wd.get('consecutive', 0)} lần chạy bị bỏ.\n"
                    f"Lý do cuối cùng: {_reason_text(wd.get('last_reason') or '')}.")
            sent = _safe(send, text)
        wd.update(consecutive=0, since=None, alerted=False)
        return sent
    if not wd.get('consecutive'):
        wd['since'] = now_ts
    wd['consecutive'] = wd.get('consecutive', 0) + 1
    wd['last_reason'] = reason + (f' {detail}' if detail else '')
    if wd['consecutive'] >= WATCHDOG_ALERT_AFTER and not wd.get('alerted'):
        # Vua bao 'tu choi lap lai' cho cung su co -> chi danh dau, khong bao trung
        if now_ts - wd.get('rate_alerted_at', -WATCHDOG_RATE_WINDOW) < 6 * 3600:
            wd['alerted'] = True
            return None
        text = ('⚠️ <b>PA PAPER đang "mù" vì dữ liệu</b>\n'
                f"Lý do: {_reason_text(reason, detail)}.\n"
                f"Từ {_vn(wd['since'])} (giờ VN), {wd['consecutive']} lần chạy liên tiếp bị từ chối dữ liệu.\n"
                'Trong lúc này bot không quét lệnh mới và có thể chưa chốt được kết quả lệnh đang mở.')
        result = _safe(send, text)
        if result.get('ok'):
            wd['alerted'] = True
        return result
    rejected = sum(x[1] for x in recent)
    if (not wd.get('alerted') and rejected >= WATCHDOG_RATE_ALERT
            and now_ts - wd.get('rate_alerted_at', 0) >= WATCHDOG_RATE_WINDOW):
        text = ('⚠️ <b>PA PAPER: dữ liệu bị từ chối lặp lại</b>\n'
                f"{rejected}/{len(recent)} lần chạy trong 24 giờ qua (giờ thị trường mở) bị từ chối dữ liệu, "
                'xen kẽ với lần chạy bình thường.\n'
                f"Lý do gần nhất: {_reason_text(reason, detail)}.\n"
                'Bot bỏ lỡ một phần các lần quét lệnh; nên kiểm tra nguồn dữ liệu.')
        result = _safe(send, text)
        if result.get('ok'):
            wd['rate_alerted_at'] = now_ts
        return result
    return None
