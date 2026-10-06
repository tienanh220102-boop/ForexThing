"""PAPER portfolio reservations and predeclared prospective evaluation."""
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import pa_ledger as ledger

CONTROL_VERSION='2026-10-06-controls-v1'
MAX_ACTIVE=2
MAX_SAME_DIRECTION=1
MAX_RISK_UNITS=2.0
PLAN={'min_calendar_days':60,'min_resolved':100,'min_per_setup':25,
      'max_unknown_fraction':0.05,'min_mean_net_r':0,'min_profit_factor':1.2,
      'max_drawdown_r':5,'purpose':'review only; never automatic live approval'}


def reserved(rec):
    return rec.get('ledger_version')==ledger.LEDGER_VERSION and not (
        rec.get('status')=='CLOSED' and rec.get('outcome') in ledger.SCORED or
        rec.get('outcome') in ledger.NON_TRADES)


def portfolio(state, direction=None):
    active=[r for r in state.get('signals',[]) if reserved(r)]
    unknown=sum(not isinstance(r.get('risk_units'),(int,float)) or
                not math.isfinite(r.get('risk_units',math.nan)) or r.get('risk_units',0)<=0 for r in active)
    risk=sum(r.get('risk_units',0) for r in active if isinstance(r.get('risk_units'),(int,float)) and math.isfinite(r['risk_units']))
    return {'active':len(active),'same_direction':sum(r.get('dir')==direction for r in active),
            'reserved_risk_units':risk,'unknown_reservations':unknown,
            'max_active':MAX_ACTIVE,'max_same_direction':MAX_SAME_DIRECTION,'max_risk_units':MAX_RISK_UNITS,
            'scope':'PA PAPER only; broker/manual/other systems not known',
            'unit_definition':'one fixed PAPER all-in stop budget per accepted signal; not account %/lot'}


def admission(state, order):
    if state.get('audit_capacity_reached'):return 'AUDIT_CAPACITY'
    if state.get('forward_review',{}).get('readiness')=='REVIEW_DUE':return 'STUDY_REVIEW_DUE'
    p=portfolio(state,order['dir'])
    if p['unknown_reservations']:return 'UNKNOWN_RISK_RESERVATION'
    if p['active']>=MAX_ACTIVE:return 'MAX_ACTIVE_ORDERS'
    if p['same_direction']>=MAX_SAME_DIRECTION:return 'SAME_DIRECTION_EXPOSURE'
    if p['reserved_risk_units']+1>MAX_RISK_UNITS+1e-9:return 'TOTAL_RISK_BUDGET'
    return None


def configuration_hash(root):
    names=('gold_pa_bot.py','forex_notifier.py','market_data.py','execution_rules.py',
           'pa_ledger.py','pa_controls.py','news_calendar.py','pa_observations.py','requirements.txt')
    runtime={'BIAS':os.environ.get('BIAS','OFF'),'PA_MODE':os.environ.get('PA_MODE','paper')}
    return hashlib.sha256(b''.join((Path(root)/name).read_bytes() for name in names)+
                          json.dumps(runtime,sort_keys=True).encode()).hexdigest()


def study(state, now_ts, config_hash):
    studies=state.setdefault('forward_studies',[])
    if not studies or studies[-1]['configuration_hash']!=config_hash:
        studies.append({'id':f'{CONTROL_VERSION}:{config_hash[:12]}:{int(now_ts)}',
                        'started_at':now_ts,'ends_at':now_ts+60*86400,
                        'configuration_hash':config_hash,'plan':dict(PLAN),
                        'runtime':{'BIAS':os.environ.get('BIAS','OFF'),'PA_MODE':os.environ.get('PA_MODE','paper')},
                        'execution_contract':'UTC spot M1; full TP1; aggregate cost 0.40 USD/oz; no broker fills'})
    current=studies[-1]
    cutoff=min(now_ts,current['ends_at'])
    rows=[r for r in state.get('signals',[]) if r.get('study_id')==current['id'] and r['ts']<=cutoff]
    resolved=[r for r in rows if r.get('status')=='CLOSED' and r.get('outcome') in ledger.SCORED and r['closed_ts']<=cutoff]
    unknown=[r for r in rows if r.get('outcome') and r.get('closed_ts',math.inf)<=cutoff and r.get('outcome') not in ledger.SCORED|ledger.NON_TRADES]
    pending=sum(not r.get('outcome') or r.get('closed_ts',math.inf)>cutoff for r in rows)
    def stats(rs):
        rs=sorted(rs,key=lambda r:r['closed_ts'])
        values=[r['net_r'] for r in rs]
        loss=-sum(v for v in values if v<0); win=sum(v for v in values if v>0)
        eq=peak=dd=0
        for v in values:eq+=v;peak=max(peak,eq);dd=max(dd,peak-eq)
        return {'n':len(values),'wins':sum(v>0 for v in values),'net_r':sum(values),
                'mean_r':statistics.mean(values) if values else None,
                'profit_factor':win/loss if loss else None,'max_dd_r':dd}
    metrics=stats(resolved); days=max(0,(cutoff-current['started_at'])/86400)
    groups={}
    for field in ('setup','session'):
        for key in sorted({r.get(field) or 'unknown' for r in resolved}):
            groups[f'{field}:{key}']=stats([r for r in resolved if (r.get(field) or 'unknown')==key])
    issues=[]; plan=current['plan']
    if days<plan['min_calendar_days']:issues.append('INSUFFICIENT_DAYS')
    if metrics['n']<plan['min_resolved']:issues.append('INSUFFICIENT_RESOLVED')
    for setup in ('sweep_reclaim','breakout'):
        if groups.get('setup:'+setup,{}).get('n',0)<plan['min_per_setup']:issues.append('INSUFFICIENT_'+setup.upper())
    if len(unknown)/max(1,len(rows))>plan['max_unknown_fraction']:issues.append('TOO_MUCH_UNKNOWN_DATA')
    state['forward_review']={'study_id':current['id'],'days':round(days,2),'plan':plan,
        'metrics':metrics,'groups':groups,'signals':len(rows),'unknown':len(unknown),'pending_at_cutoff':pending,
        'cutoff':cutoff,'ends_at':current['ends_at'],
        'readiness':'REVIEW_DUE' if now_ts>=current['ends_at'] else 'COLLECTING','issues':issues,
        'performance_checks':{'positive_mean':metrics['mean_r'] is not None and metrics['mean_r']>0,
                              'profit_factor':metrics['profit_factor'] is not None and metrics['profit_factor']>=plan['min_profit_factor'],
                              'drawdown':metrics['max_dd_r']<=plan['max_drawdown_r']},
        'live_allowed':False}
    return current['id']
