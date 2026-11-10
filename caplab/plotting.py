"""Group multidimensional results without merging independent scan slices."""
def curve_groups(rows,axis,names,mode,filters=None,include_frequency=True):
    filters=filters or {}
    data=[r for r in rows if all(str(r.get(k))==str(v) for k,v in filters.items() if v!='全部')]
    others=[n for n in names if n!=axis] if mode=='cartesian' and axis not in ('point','code') else []
    varying={n for n in others if len({r[n] for r in rows})>1}
    groups={}
    for r in data:
        key=((r['frequency_Hz'],) if include_frequency else ())+tuple(r[n] for n in others)
        groups.setdefault(key,[]).append(r)
    result=[]
    for key,values in sorted(groups.items()):
        label='{:g}G'.format(key[0]/1e9) if include_frequency else ''
        for name,value in zip(others,key[1:] if include_frequency else key):
            if name in varying:label+=(' · ' if label else '')+'{}={}'.format(name,value)
        label=label or 'First resonance'
        result.append((label,sorted(values,key=lambda r:(r[axis],r.get('point',r.get('code',0))))))
    return result
