# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-data boundary phase operation for LatestPhaseJobs; no bpy/RNA/GPU."""
import numpy as np
from .phase_adapter import plan_sources
MAX_SOURCES = 100_000

def compute_boundary_phases(payload, checkpoint, *, include_arc=False):
    if type(include_arc) is not bool:
        raise ValueError('include_arc must be a boolean')
    if type(payload) is not dict or set(payload) != {'sources', 'domain_rules'}:
        raise ValueError('owned sources and explicit domain_rules required')
    sources=payload['sources'];rules=payload['domain_rules']
    if type(sources) is not list or len(sources)>MAX_SOURCES or type(rules) is not dict:
        raise ValueError('boundary phase source/type budget')
    checkpoint()
    domains={};known=set()
    for index,source in enumerate(sources):
        if index%256==0:checkpoint()
        if type(source) is not dict or set(source)!={'source_id','endpoint_ids','xy','w','domain'}:
            raise ValueError('five explicit source fields required')
        sid=source['source_id'];domain=source['domain']
        if type(sid) is not str or sid in known or type(domain) is not str:
            raise ValueError('global unique source ID and explicit domain required')
        known.add(sid);domains.setdefault(domain,[]).append(index)
    if set(domains)!=set(rules):
        raise ValueError('domain rule set must exactly match emitted source domains')
    phase=np.zeros((len(sources),2),np.float64);active=np.zeros(len(sources),bool);reports=[]
    if include_arc:arc=np.empty((len(sources),2),np.float64)
    for domain,indices in domains.items():
        checkpoint();rule=rules[domain]
        if type(rule) is not dict or set(rule) not in ({'policy','max_gap_px'},{'policy','max_gap_px','seam_anchor'}):
            raise ValueError('domain rule requires explicit policy and independent gap')
        rows=[sources[i] for i in indices]
        result=plan_sources(rows,policy=rule['policy'],max_gap_px=rule['max_gap_px'],checkpoint=checkpoint,include_arc=include_arc,seam_anchor=rule.get('seam_anchor'))
        phase[indices]=result['parameters'];active[indices]=result['active']
        if include_arc:arc[indices]=result['arc_px']
        reports.append(dict(domain=domain,linker=result['linker']['evidence'],
            counts=result['policy_plan']['evidence']['counts'],
            policy=result['policy_plan']['evidence']['policy']))
    checkpoint()
    result=dict(source_ids=tuple(row['source_id'] for row in sources),parameters=phase,active=active,
        reports=reports,qualification='autonomous boundary source phase only; authored curves/maps remain on caller thread')
    if include_arc:result['arc_px']=arc
    return result
