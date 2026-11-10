"""Small JSON workbook: independent circuit pages and self-contained result snapshots."""
import copy
import hashlib
import json
import math
import re
import uuid
from .model import new_project
from .registers import upgrade_project


def page(name,project=None):
    return {'id':uuid.uuid4().hex[:12],'name':name,'project':upgrade_project(project) if project else new_project(),'result':None}


def workbook(data=None):
    if data and data.get('kind')=='caplab-workbook':
        book=copy.deepcopy(data)
        if book.get('version')!=3 or not isinstance(book.get('pages'),list) or not book['pages']:
            raise ValueError('Invalid workbook format')
        ids=set()
        for p in book['pages']:
            if not p.get('id') or p['id'] in ids or not str(p.get('name','')).strip():raise ValueError('Invalid page identifier or name')
            ids.add(p['id']);p['project']=upgrade_project(p['project'])
            builtin=re.fullmatch(r'页面\s*(\d+)',p['name'])
            names={'单阵列':'Single array','多阵列':'Multiple arrays','前仿':'Pre-layout','后仿':'Post-layout'}
            if builtin:p['name']='Page '+builtin[1]
            elif p['name'] in names:p['name']=names[p['name']]
            if isinstance(p.get('filters'),dict):
                p['filters']={key:value for key,value in p['filters'].items() if value not in ('全部','All')}
            result=p.get('result')
            if result and isinstance(result.get('rows'),list) and all(isinstance(r,dict) for r in result['rows']) and result.get('sha256')==rows_hash(result.get('rows')):
                result['rows']=english_rows(result['rows']);result['sha256']=rows_hash(result['rows'])
        if book.get('active_page') not in ids:book['active_page']=book['pages'][0]['id']
        return book
    first=page('Page 1',data)
    return {'kind':'caplab-workbook','version':3,'active_page':first['id'],'pages':[first]}


def current(book):
    return next(p for p in book['pages'] if p['id']==book['active_page'])


def add_page(book,name=None,source=None):
    names={p['name'] for p in book['pages']};stem=name or 'Page';index=1
    if name is None:
        while 'Page '+str(index) in names:index+=1
        name='Page '+str(index)
    else:name=stem
    while name in names or (not name.strip()):
        index+=1;name=stem+' '+str(index)
    p=page(name,copy.deepcopy(source['project']) if source else None)
    if not source:
        for key in ('workspace','library','reference','extra_libraries','maestro_runtime'):
            if key in current(book)['project']:p['project'][key]=copy.deepcopy(current(book)['project'][key])
    if source:
        p['project']['private']=None
        p['result']=copy.deepcopy(source.get('result'))
    book['pages'].append(p);book['active_page']=p['id'];return p


def rename_page(book,ident,name):
    name=name.strip()
    if not name:raise ValueError('Page name cannot be empty')
    if any(p['name']==name and p['id']!=ident for p in book['pages']):raise ValueError('Duplicate page name')
    next(p for p in book['pages'] if p['id']==ident)['name']=name


def delete_page(book,ident):
    if len(book['pages'])==1:raise ValueError('Keep at least one page')
    index=next(i for i,p in enumerate(book['pages']) if p['id']==ident)
    book['pages'].pop(index)
    if book['active_page']==ident:book['active_page']=book['pages'][min(index,len(book['pages'])-1)]['id']


def rows_hash(rows):
    return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


def cache(rows,identity,mode):
    if not rows or not identity:return None
    rows=english_rows(rows)
    return {'rows':copy.deepcopy(rows),'digest':identity,'mode':mode,'sha256':rows_hash(rows)}


def cached_rows(result):
    if not result or not result.get('digest') or result.get('sha256')!=rows_hash(result.get('rows')):return []
    rows=result['rows']
    try:
        for r in rows:
            for key in ('frequency_Hz','C_F','G_S','B_S'):
                if not math.isfinite(r[key]):return []
            if r['frequency_Hz']<=0 or (r['Q'] is not None and not math.isfinite(r['Q'])):return []
            if not isinstance(r.get('point',r.get('code')),int):return []
            split=r.get('decomposition')
            if split is not None:
                if not isinstance(split,dict) or split.get('schema')!=1:return []
                for component in ('coupling','ground','total','p_ground','n_ground','p_input','n_input'):
                    metric=split.get(component)
                    if metric is None:continue
                    for key in ('C_F','G_S','B_S'):
                        if not math.isfinite(metric[key]):return []
                    if metric['Q'] is not None and not math.isfinite(metric['Q']):return []
                    if metric.get('Q_signed') is not None and not math.isfinite(metric['Q_signed']):return []
                if split.get('total') is not None:
                    for key in ('C_F','G_S','B_S'):
                        if not math.isclose(split['total'][key],r[key],rel_tol=1e-8,abs_tol=1e-30):return []
                approximate=split.get('approximate')
                if approximate is not None:
                    if not isinstance(approximate,dict):return []
                    for component in ('coupling','ground','total','p_ground','n_ground','correction'):
                        metric=approximate.get(component)
                        if not isinstance(metric,dict):return []
                        for key in ('C_F','G_S','B_S'):
                            if not math.isfinite(metric[key]):return []
                        for key in ('Q','Q_signed'):
                            if metric.get(key) is not None and not math.isfinite(metric[key]):return []
                    for key in ('reconstruction_error','c_sum_residual_F'):
                        if not math.isfinite(approximate[key]):return []
                    if approximate['reconstruction_error']<0 or approximate['reconstruction_error']>1e-6:return []
                    residual=approximate['coupling']['C_F']+approximate['ground']['C_F']-split['total']['C_F']
                    if not math.isclose(residual,approximate['c_sum_residual_F'],rel_tol=1e-8,abs_tol=1e-28):return []
                if split.get('valid'):
                    if any(split.get(k) is None for k in ('coupling','ground','total')):return []
                    total=split['total']['C_F'];parts=split['coupling']['C_F']+split['ground']['C_F']
                    if not math.isclose(total,parts,rel_tol=1e-8,abs_tol=1e-30):return []
                    if not math.isclose(total,r['C_F'],rel_tol=1e-8,abs_tol=1e-30):return []
    except (TypeError,KeyError,ValueError):return []
    return english_rows(rows)


def english_rows(rows):
    result=copy.deepcopy(rows)
    labels={'电容区':'Capacitive','非电容区':'Non-capacitive','负电导':'Negative conductance'}
    for row in result:
        if row.get('status') in labels:row['status']=labels[row['status']]
    return result
