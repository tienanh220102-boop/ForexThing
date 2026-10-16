#!/usr/bin/env python3
"""Bang kich ban PA — chay trong cron sau gold_pa_bot. CHI phan tich, khong tao lenh.

- 06:30 UTC (13:30 VN) dang bang phien Au, 12:30 UTC (19:30 VN) bang phien My.
- Moi lan chay (toi da 2 lan/gio khi co bang dang mo): tai M15, tu cham kich ban.
- Het han bang -> tra loi vao tin bang tren Telegram: kich ban nao dung/sai + do trung luy ke.
State: scenario_state.json (dashboard doc). Loi o day KHONG duoc lam hong cron bot.
"""
import json, os, time
from datetime import datetime, timezone
from pathlib import Path

import requests

import forex_notifier as fx
import pa_scenarios as scn
from market_data import parse_twelve, DataQualityError

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'scenario_state.json'
SYM = 'XAU/USD'
KEEP = 120


def load(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def fetch_m15():
    params = {'symbol': SYM, 'interval': '15min', 'outputsize': 200, 'apikey': fx.TWELVE_DATA_KEY,
              'timezone': 'UTC', 'order': 'asc'}
    try:
        data = requests.get('https://api.twelvedata.com/time_series', params=params, timeout=30).json()
    except (requests.RequestException, ValueError):
        raise DataQualityError('M15 transport failure') from None   # khong in URL: co API key
    if 'values' not in data:
        raise DataQualityError(f"M15 no candles (code={data.get('code')})")
    return [b for b in parse_twelve(data, time.time(), interval=900, requested_timezone='UTC') if b['closed']]


def send(text, reply_to=None):
    r = fx.send_telegram(text, reply_to=reply_to)
    if not r.get('ok') and reply_to:
        r = fx.send_telegram(text)
    return r


def main():
    if not fx.TELEGRAM_TOKEN or not fx.TELEGRAM_CHAT or not fx.TWELVE_DATA_KEY:
        print('[SCN] thieu TELEGRAM/TWELVE key — bo qua'); return
    now = time.time()
    state = load(STATE, {'boards': []})
    due = scn.due_board(state, now)
    open_boards = [b for b in state['boards'] if b['status'] == 'OPEN']
    if not due and not open_boards:
        print('[SCN] khong co bang can dang/cham'); return
    if not due and now - state.get('last_fetch', 0) < 1700:
        print('[SCN] cham lai sau (toi da 2 lan/gio)'); return
    try:
        m15 = fetch_m15()
    except DataQualityError as e:
        print(f'[SCN] M15 loi: {e}'); return
    state['last_fetch'] = now
    if due:
        ph = load(ROOT / 'price_history.json', {}).get(SYM, {})
        h1 = ph.get('bars', [])
        cal = load(ROOT / 'gold_pa_state.json', {}).get('calendar_snapshot')
        fresh = h1 and now - h1[-1]['t'] < 1.5 * 3600 and m15 and now - m15[-1]['t'] < 1800
        board = scn.build_board(due, h1, m15, now, cal) if fresh else None
        if board:
            # 'prior' = thong ke backtest (hyp35) da gieo san; cong voi ket qua live
            scn.rank(board, scn.merge(state.get('prior'), state.get('stats')))
        if board and board['scenarios']:
            r = send(scn.board_message(board, scn.merge(state.get('prior'), state.get('stats'))))
            if r.get('ok'):
                board['message_id'] = r.get('result', {}).get('message_id')
                state['boards'].append(board)
                print(f"[SCN] da dang bang {due}: {len(board['scenarios'])} kich ban, nghieng={board['pick']}")
        else:
            # danh dau da xu ly hom nay de khong thu lai lien tuc khi du lieu khong dung duoc
            state['boards'].append({'name': due, 'day': scn._day_start(now), 'status': 'SKIPPED',
                                    'reason': 'stale_data' if not fresh else 'no_scenario',
                                    'created_at': now, 'scenarios': [], 'ctx': {}})
            print(f'[SCN] bo qua bang {due}: du lieu cu hoac khong co kich ban')
    for b in [b for b in state['boards'] if b['status'] == 'OPEN']:
        if scn.resolve(b, m15, now):
            state['stats'] = scn.stats(state)
            r = send(scn.result_message(b, state['stats']), reply_to=b.get('message_id'))
            b['result_sent'] = bool(r.get('ok'))
            print(f"[SCN] da cham bang {b['name']}: {[x['status'] for x in b['scenarios']]}")
    state['boards'] = state['boards'][-KEEP:]
    state['stats'] = scn.stats(state)
    state['updated_at'] = now
    with open(STATE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1, allow_nan=False)


if __name__ == '__main__':
    main()
