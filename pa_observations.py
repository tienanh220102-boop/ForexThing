"""First-seen candidate evidence and a separate, non-actionable shadow ledger."""
import copy
from collections import Counter
import hashlib
import json
import pa_ledger as ledger

MAX_OBSERVATIONS=10000


def identity(order, namespace=''):
    fields={k:order.get(k) for k in ('setup','dir','signal_bar_t','level')}
    fields['namespace']=namespace
    return hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()[:20]


def observe(state, order, reason, now_ts, source, context):
    observations=state.setdefault('observations',[])
    key=identity(order,context.get('study_id','')+'|'+source)
    found=next((o for o in observations if o['id']==key),None)
    if found:
        # First snapshot and shadow are immutable; later admission is a separate event.
        if reason not in {e['reason'] for e in found['decisions']}:
            found['decisions'].append({'ts':now_ts,'reason':reason})
        return key
    if len(observations)>=MAX_OBSERVATIONS:
        state['audit_capacity_reached']=True
        return None
    # Do not publish legacy lot/account-size-derived fields from build_order.
    keys=('setup','dir','signal_bar_t','level','structure','entry','sl','tp1','tp2',
          'entry_type','session','regime','stars','probe','align','reason','rule_version',
          'net_reward_risk','atr_at_signal','target_wall','quote_t','quote_price')
    snapshot=copy.deepcopy({k:order[k] for k in keys if k in order})
    shadow=None
    if all(k in order for k in ('entry','sl','tp1','tp2')) and ledger.valid_geometry(order):
        raw={k:v for k,v in order.items() if k in ('setup','dir','entry','sl','tp1','tp2','entry_type','session','signal_bar_t')}
        shadow=ledger.new_record(raw,now_ts,source,policy='tp1_full',cost_price=.4)
        shadow['purpose']='counterfactual from first_seen; baseline 6h limit; ignores admission filters; never sent'
    observations.append({'id':key,'first_seen':now_ts,'first_reason':reason,
                         'order':snapshot,'context':copy.deepcopy(context),
                         'decisions':[{'ts':now_ts,'reason':reason}],
                         'shadow':shadow,'evaluation':'PENDING' if shadow else 'NOT_EVALUABLE'})
    if len(observations)>=MAX_OBSERVATIONS:state['audit_capacity_reached']=True
    return key


def resolve(state,bars,now_ts,source):
    for row in state.get('observations',[]):
        r=row.get('shadow')
        if r and not r.get('outcome'):
            ledger.advance(r,bars,now_ts,source)
            row['evaluation']=r.get('outcome',r['status'])
    summarize(state)


def summarize(state):
    rows=state.get('observations',[]); groups={}
    for reason in sorted({r['first_reason'] for r in rows}):
        rs=[r for r in rows if r['first_reason']==reason]
        scored=[r['shadow'] for r in rs if r.get('shadow',{} ) and r['shadow'].get('status')=='CLOSED']
        groups[reason]={'candidates':len(rs),'shadow_resolved':len(scored),
                        'shadow_net_r':sum(r['net_r'] for r in scored),
                        'shadow_wins':sum(r['net_r']>0 for r in scored),
                        'evaluations':dict(Counter(r['evaluation'] for r in rs)),
                        'later_accepted':sum(any(e['reason']=='ACCEPTED' for e in r['decisions']) for r in rs)}
    state['observation_summary']={'count':len(rows),'groups':groups,
        'basis':'first blocking stage, not isolated causal effect; hypothetical baseline separate from sent PAPER trades',
        'capacity_reached':state.get('audit_capacity_reached',False)}
