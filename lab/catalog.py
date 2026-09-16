"""Streaming, content-bound catalog for flat or nested annual CSV archives."""
from pathlib import Path
import csv, time,re
from .common import plain, sha, atomic_json, read_json,validate_quality_row,validate_csv_header

CATALOG_VERSION='FSL_csv_catalog_v7'
# Catalogs built before duplicate-folder detection must be rescanned before jobs are created from them.
CONFLICT_CHECK='sid_directory_v1'
REQUIRED={'sId','联赛','公司','盘口类型','状态','比赛分钟','当时比分','盘口数值','上水/大球','下水/小球','变化时间','封盘','日期'}

class InspectionCancelled(Exception):pass

def quality_files(paths,roots=()):
    """Discover the named handoff beside the selected data tree (never above its named boundary) or inside a selected folder or archive."""
    found=[]
    for value in paths:
        p=Path(value).resolve();base=p if p.is_dir() else p.parent
        for ancestor in (base,*base.parents):
            q=ancestor/'_build'/'handoff_current'/'exclude_sids.csv'
            if q.is_file():found.append(q);break
            # A named data-tree boundary must not fall through into another archive.
            if ancestor.name=='按国家分类数据':break
    # A selected parent folder or an extracted archive can contain the data-tree root and its handoff.
    for root in roots:
        root=Path(root).resolve()
        if not root.is_dir():continue
        inside=sorted({q.resolve() for q in root.rglob('exclude_sids.csv') if q.is_file() and q.parent.name=='handoff_current' and q.parent.parent.name=='_build'
                       and not any(part.startswith('_') for part in q.relative_to(root).parts[:-3])})
        if len(inside)>1:raise ValueError('所选输入含多份质量交割清单，无法确定数据树；请只选择其中一个数据树：'+'；'.join(str(q) for q in inside[:3]))
        found.extend(inside)
    return list(dict.fromkeys(found))

def country_map(path):
    p=path.parent/'_COPY_MANIFEST.csv'
    if not p.is_file():return {}
    with p.open(encoding='utf-8-sig',newline='') as f:
        return {plain(r.get('league_short')):plain(r.get('country')) for r in csv.DictReader(f)}

def scan_file(path,callback,cancel):
    with path.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.reader(f);names=next(reader,[]);header=set(names)
        # A recognizable quote table must not degrade to an index/ignored file
        # when a mandatory field is absent. Old catalog caches are versioned out.
        if {'sId','联赛','公司','盘口类型'}<=header and not REQUIRED<=header:
            raise ValueError(f'{path.name}: 完整指数CSV缺少必要列: '+','.join(sorted(REQUIRED-header)))
        if path.name.endswith('exclude_sids.csv') and not {'sId','reason','scope','source'}<=header:raise ValueError(f'{path.name}: 质量清单缺少必要列')
        kind='quotes' if REQUIRED<=header else 'index' if {'sId','全场比分','状态','联赛'}<=header else 'quality' if {'sId','reason','scope','source'}<=header else None
        if kind is None:return None
        validate_csv_header(names,path.name)
        if kind=='quality':
            count=0
            for rowno,row in enumerate(reader,2):
                if not row:continue
                if len(row)!=len(names):raise ValueError(f'{path.name}:{rowno}: 质量清单列数不符')
                validate_quality_row(dict(zip(names,row)),f'{path.name}:{rowno}');count+=1
                if count%1000==0 and cancel():raise InspectionCancelled('质量清单扫描已取消')
            return {'kind':kind,'columns':len(names),'joined_results':False,'groups':[],'rows':count}
        at={name:i for i,name in enumerate(names)};groups={};last=time.monotonic()
        for rowno,row in enumerate(reader,2):
            if not row:continue
            if len(row)!=len(names):raise ValueError(f'{path.name}:{rowno} 列数不符，不能猜测字段位置')
            league=plain(row[at['联赛']]);company=plain(row[at['公司']]) if kind=='quotes' else ''
            if not league or (kind=='quotes' and not company):raise ValueError(f'{path.name}:{rowno} 联赛或公司缺失')
            key=(league,company)
            if key not in groups:groups[key]={'league':league,'company':company,'rows':0,'sids':set(),'years':set(),'date_min':None,'date_max':None}
            g=groups[key];g['rows']+=1;sid=plain(row[at['sId']])
            if sid:g['sids'].add(sid)
            date=plain(row[at['日期']]) if '日期' in at else ''
            if kind=='quotes':
                from .common import validate_quote_fields,timestamp,MISSING
                validate_quote_fields(date,row[at['封盘']],f'{path.name}:{rowno}')
                # A supported market row is research input; an unreadable identity must stop here instead of vanishing at load.
                if plain(row[at['盘口类型']]) in ('大小球','让球') and plain(row[at['状态']]) in ('早','即','滚') and (not sid or timestamp(row[at['变化时间']])==MISSING):
                    raise ValueError(f'{path.name}:{rowno}: sId缺失或变化时间不是 YYYY-MM-DD HH:MM')
            else:
                from .common import validate_date,timestamp,MISSING
                if date:validate_date(date,f'{path.name}:{rowno}')
                if '开球时间' in at and plain(row[at['开球时间']]) and timestamp(row[at['开球时间']])==MISSING:raise ValueError(f'{path.name}:{rowno}: 索引开球时间无效')
            if date:
                g['years'].add(date[:4]);g['date_min']=min(g['date_min'] or date,date);g['date_max']=max(g['date_max'] or date,date)
            if rowno%25000==0:
                if cancel():raise InspectionCancelled('输入扫描已取消，已完成文件缓存保留')
                if time.monotonic()-last>=.3:callback(file_rows=rowno-1);last=time.monotonic()
        for g in groups.values():g['sids']=sorted(g['sids']);g['years']=sorted(g['years'])
        return {'kind':kind,'columns':len(names),'joined_results':'比赛状态' in header,'groups':list(groups.values())}

def build_catalog(paths,work,original_paths,progress=None,should_cancel=None,input_roots=()):
    update=progress or (lambda **kw:None);cancel=should_cancel or (lambda:False)
    cache=Path(work)/'catalog_cache';cache.mkdir(parents=True,exist_ok=True)
    files=[];groups={};seen=set();ignored=[];owners={};metadata={};total_bytes=sum(p.stat().st_size for p in paths);done_bytes=0;rows_total=0;cached=0
    for i,p in enumerate(paths):
        if cancel():raise InspectionCancelled('输入扫描已取消，已完成文件缓存保留')
        update(completed_files=i,total_files=len(paths),current_file=p.name,processed_bytes=done_bytes,total_bytes=total_bytes,rows=rows_total,file_rows=0)
        before=p.stat();h=sha(p)
        if h in seen:ignored.append({'file':p.name,'reason':'相同内容SHA-256重复'});done_bytes+=before.st_size;continue
        seen.add(h);cache_path=cache/(h+'.json');saved=read_json(cache_path)
        if saved and saved.get('version')==CATALOG_VERSION:info=saved['info'];cached+=1
        else:
            info=scan_file(p,update,cancel)
            after=p.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('扫描期间文件改变，请重新检查: '+str(p))
            atomic_json(cache_path,{'version':CATALOG_VERSION,'info':info})
        after=p.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('扫描期间文件改变，请重新检查: '+str(p))
        done_bytes+=before.st_size
        if info is None:ignored.append({'file':p.name,'reason':'辅助表或不支持的表头'});continue
        rec={'path':str(p),'sha256':h,'kind':info['kind'],'columns':info['columns'],'size_bytes':before.st_size,'joined_results':info['joined_results']}
        if info['kind']=='quotes':rec['targets']=[[g['league'],g['company']] for g in info['groups']]
        elif info['kind']=='index':rec['leagues']=sorted({g['league'] for g in info['groups']})
        else:rec['policy']='source_hard_and_quarantine_v1'
        files.append(rec)
        if info['kind']!='quotes':continue
        if p.parent not in metadata:metadata[p.parent]=country_map(p)
        short=p.stem[len('完整指数_'):].rsplit('_',1)[0] if p.stem.startswith('完整指数_') else ''
        if re.fullmatch(r'\d{4}(?:-\d{2})?',short):
            leagues={g['league'] for g in info['groups']};short=next(iter(leagues)) if len(leagues)==1 else ''
        country=metadata[p.parent].get(short,'')
        for part in info['groups']:
            key=(part['league'],part['company'])
            if key not in groups:groups[key]={'league':key[0],'company':key[1],'rows':0,'sids':set(),'years':set(),'countries':set(),'file_groups':set(),'files':0,'date_min':None,'date_max':None}
            g=groups[key];g['rows']+=part['rows'];rows_total+=part['rows'];g['sids'].update(part['sids']);g['years'].update(part['years']);g['files']+=1
            # The same match stored under two different folders means duplicate copies of one league.
            folders=owners.setdefault(key,{})
            for sid in part['sids']:folders.setdefault(sid,set()).add(str(p.parent))
            if country:g['countries'].add(country)
            if short:g['file_groups'].add(short)
            for name,fn in (('date_min',min),('date_max',max)):
                if part[name]:g[name]=fn(g[name] or part[name],part[name])
    if not groups:raise ValueError('没有找到完整指数CSV；需要数据说明中的中文表头')
    quality=[r['path'] for r in files if r['kind']=='quality']
    for q in quality_files(original_paths,input_roots):
        h=sha(q)
        if h not in seen:
            info=scan_file(q,update,cancel)
            if info is None or info['kind']!='quality':raise ValueError('质量交割清单表头不符合契约: '+str(q))
            if sha(q)!=h:raise ValueError('扫描期间质量交割清单改变')
            files.append({'path':str(q),'sha256':h,'kind':'quality','policy':'source_hard_and_quarantine_v1','columns':info['columns'],'size_bytes':q.stat().st_size,'joined_results':False});seen.add(h)
            quality.append(str(q))
    rows=[]
    for key,g in sorted(groups.items()):
        folders=owners.get(key,{})
        duplicates=[sid for sid,where in folders.items() if len(where)>1]
        # A boundary match may legitimately repeat; copies of a league repeat many matches.
        if len(duplicates)>=max(3,-(-len(g['sids'])//100)):
            g['source_conflict']={'duplicate_matches':len(duplicates),'directories':sorted({f for sid in duplicates for f in folders[sid]})[:8]}
        g['matches']=len(g.pop('sids'))
        for key in ('years','countries','file_groups'):g[key]=sorted(g[key])
        rows.append(g)
    update(completed_files=len(paths),total_files=len(paths),current_file='',processed_bytes=total_bytes,total_bytes=total_bytes,rows=rows_total,file_rows=0)
    return {'files':files,'leagues':rows,'catalog_version':CATALOG_VERSION,'input_paths':[str(Path(p).expanduser().resolve()) for p in original_paths],
        'scan':{'files':len(paths),'quote_files':sum(r['kind']=='quotes' for r in files),'quote_rows':rows_total,'total_bytes':total_bytes,'cached_files':cached,'ignored':ignored},'conflict_check':CONFLICT_CHECK,
        'quality':{'files':quality,'policy':'source_hard_and_quarantine_v1','note':'按清单范围排除硬问题及隔离行相关场次；remaining_suspicious仅记录。' if quality else '未发现同树质量交割清单，结果仍为上传标签试算。'}}
