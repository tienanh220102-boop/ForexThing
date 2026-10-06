"""Macro calendar: explicit offset, weekly coverage, fail closed on uncertainty."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo
import requests

URL = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
TTL = 900


def parse_week(payload, now_ts):
    if not isinstance(payload, list) or not payload:
        raise ValueError('empty_or_invalid_weekly_export')
    local = datetime.fromtimestamp(now_ts, timezone.utc).astimezone(ZoneInfo('America/New_York'))
    start = (local-timedelta(days=(local.weekday()+1)%7)).replace(hour=0,minute=0,second=0,microsecond=0)
    end = start+timedelta(days=7)
    events=[]
    for row in payload:
        stamp=datetime.fromisoformat(row['date'])
        if stamp.tzinfo is None or not start.timestamp() <= stamp.timestamp() < end.timestamp():
            raise ValueError('naive_time_or_wrong_week')
        impact=row['impact'].lower(); ccy=row['country'].upper()
        if impact not in ('high','medium','low','holiday') or ccy not in ('USD','EUR','GBP','JPY','CHF','CAD','AUD','NZD','CNY','ALL'):
            raise ValueError('unknown_event_schema')
        if not isinstance(row['title'],str) or not row['title'].strip():
            raise ValueError('missing_event_title')
        events.append({'title':row['title'][:300], 'currency':ccy, 'ts':stamp.timestamp(), 'impact':impact})
    return {'status':'VERIFIED', 'source':URL, 'fetched_at':now_ts,
            'coverage_start':start.timestamp(), 'coverage_end':end.timestamp(),
            'coverage_basis':'thisweek export contract; America/New_York week, not event min/max',
            'sha256':hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),
            'events':sorted(events,key=lambda e:e['ts'])}


def health(snapshot, now_ts):
    if not snapshot or snapshot.get('status')!='VERIFIED':return 'CALENDAR_UNVERIFIED'
    if not 0 <= now_ts-snapshot.get('fetched_at',0) <= TTL:return 'CALENDAR_STALE'
    if not snapshot['coverage_start'] <= now_ts-900 or now_ts+3600 >= snapshot['coverage_end']:
        return 'CALENDAR_COVERAGE_GAP'
    return None


def fetch(now_ts, previous=None):
    if previous and health(previous,now_ts) is None:
        return previous
    try:
        response=requests.get(URL,timeout=15,headers={'Accept':'application/json'})
        if response.status_code!=200:raise ValueError('http_'+str(response.status_code))
        if int(response.headers.get('Age','0')) > TTL:raise ValueError('stale_http_cache')
        return parse_week(response.json(),now_ts)
    except Exception as exc:
        # Never include request URLs/credentials/raw payloads in error output.
        return {'status':'UNKNOWN','source':URL,'fetched_at':now_ts,
                'reason':str(exc) if isinstance(exc,ValueError) and str(exc).startswith(('http_','stale_http','empty_','naive_','unknown_','missing_')) else type(exc).__name__}


def evaluate(snapshot, now_ts, symbol='XAU/USD'):
    problem=health(snapshot,now_ts)
    if problem:return 'UNKNOWN',problem
    relevant=set(symbol.split('/'))|{'ALL'}
    hard=[]; soft=[]
    for ev in snapshot['events']:
        if ev['currency'] not in relevant:continue
        seconds=ev['ts']-now_ts
        if ev['impact']=='high' and -900 <= seconds <= 3600:hard.append(ev['title'])
        elif ev['impact']=='medium' and 0 <= seconds <= 1800:soft.append(ev['title'])
    if hard:return 'HARD','; '.join(hard)
    if soft:return 'SOFT','; '.join(soft)
    return 'PASS','verified_no_blocking_event'
