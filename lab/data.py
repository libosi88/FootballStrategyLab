"""CSV adapter: state events are separate from post-match labels."""
from pathlib import Path
from collections import defaultdict,Counter
import csv,zipfile,stat,uuid,io,hashlib
from contextlib import contextmanager
from .common import *
from .contracts import make_contract, bind_event

REQUIRED={'sId','联赛','公司','盘口类型','状态','比赛分钟','当时比分','盘口数值','上水/大球','下水/小球','变化时间','封盘','日期'}

@contextmanager
def verified_csv(rec):
 """Hash bytes actually consumed by the parser, not a separate before/after read."""
 h=hashlib.sha256();p=Path(rec['path'])
 with p.open('rb') as source:
  class HashReader(io.RawIOBase):
   def readable(self):return True
   def readinto(self,buffer):
    count=source.readinto(buffer)
    if count:h.update(memoryview(buffer)[:count])
    return count
  with io.TextIOWrapper(io.BufferedReader(HashReader()),encoding='utf-8-sig',newline='') as text:
   yield text
   while text.read(1024*1024):pass
   if h.hexdigest()!=rec['sha256']:raise ValueError('实际读取内容与冻结输入哈希不一致: '+str(p))

def safe_extract(zip_path,dest,max_bytes=2_000_000_000):
 dest=Path(dest).resolve();dest.mkdir(parents=True,exist_ok=True)
 with zipfile.ZipFile(zip_path) as z:
  infos=z.infolist()
  if len(infos)>15000 or sum(i.file_size for i in infos)>max_bytes:raise ValueError('ZIP超过文件数或解压体积限制')
  names=set()
  for i in infos:
   p=Path(i.filename.replace('\\','/'))
   # A repeated member, or one differing only by case on Windows, would silently overwrite an earlier input file.
   if not i.is_dir():
    name=p.as_posix().casefold()
    if name in names:raise ValueError('ZIP含重复或仅大小写不同的同名文件: '+i.filename)
    names.add(name)
   if p.is_absolute() or '..' in p.parts or ':' in i.filename or stat.S_ISLNK(i.external_attr>>16):raise ValueError('拒绝不安全ZIP路径')
   if i.file_size>max(1,i.compress_size)*3000:raise ValueError('拒绝异常压缩比')
   target=(dest/p).resolve()
   if dest not in target.parents and target!=dest:raise ValueError('ZIP越界')
  for i in infos:z.extract(i,dest)
 return dest

def input_paths(paths,work,roots=None):
 out=[]
 for text in paths:
  p=Path(text).expanduser().resolve()
  if not p.exists():raise ValueError('输入不存在: '+str(p))
  if p.suffix.lower()=='.zip':p=verified_extract_cache(p,work)
  # A selected folder or extracted archive can hold the data-tree root with its quality handoff below it.
  if roots is not None and p.is_dir():roots.append(p)
  if p.is_dir():
   # Folders starting with "_" (such as _build with pre-repair backups) are tooling, not research inputs.
   out.extend(sorted(x for x in p.rglob('*.csv') if not any(part.startswith('_') for part in x.relative_to(p).parts[:-1])))
  elif p.suffix.lower()=='.csv':out.append(p)
  else:raise ValueError('仅支持CSV、目录、ZIP；请勿上传可执行程序')
 return list(dict.fromkeys(out))

def verified_extract_cache(archive,work):
 """Publish a complete content-addressed extraction; retain invalid old caches."""
 archive=Path(archive);archive_hash=sha(archive);base=(Path(work)/'unpacked').resolve();base.mkdir(parents=True,exist_ok=True)
 target=base/archive_hash;marker_name='.extraction_manifest.json'
 def inventory(folder):
  result={}
  for p in sorted(folder.rglob('*')):
   if p.name==marker_name:continue
   if p.resolve()!=p or (p.resolve()!=folder and folder not in p.resolve().parents):raise ValueError('解压缓存包含链接或越界文件')
   if p.is_file():result[p.relative_to(folder).as_posix()]=sha(p)
  return result
 if target.exists() and target.resolve()!=target:raise ValueError('解压缓存根不能是链接')
 try:saved=read_json(target/marker_name,{}) if target.exists() else {}
 except (ValueError,TypeError):saved={}
 if saved.get('archive_sha256')==archive_hash and saved.get('files')==inventory(target):return target
 with tempfile.TemporaryDirectory(prefix='extract_',dir=base) as td:
  fresh=Path(td).resolve()
  if fresh.parent!=base or target.parent!=base:raise ValueError('解压缓存发布路径不在指定工作区')
  safe_extract(archive,fresh)
  if sha(archive)!=archive_hash:raise ValueError('解压过程中原ZIP改变')
  atomic_json(fresh/marker_name,{'archive_sha256':archive_hash,'files':inventory(fresh)})
  previous=None
  if target.exists():
   previous=base/(archive_hash+'.invalid_'+uuid.uuid4().hex)
   if target.resolve().parent!=base or previous.parent!=base:raise ValueError('旧缓存保留路径越界')
   atomic_replace(target,previous)
  try:atomic_replace(fresh,target)
  except BaseException:
   if previous is not None and not target.exists():atomic_replace(previous,target)
   raise
 return target

def inspect(paths,work,progress=None,should_cancel=None):
 from .catalog import build_catalog
 roots=[];files=input_paths(paths,work,roots)
 return build_catalog(files,work,paths,progress,should_cancel,input_roots=roots)


def verify_prepared_inputs(root,config):
 """Require complete, in-workspace checksums before reusing prepared data."""
 root=Path(root).resolve();manifest=read_json(root/'prepared_manifest.json')
 base={'events.jsonl.gz','labels.json','data_audit.json'}
 holdout={'holdout/events.jsonl.gz','holdout/labels.json'}
 required=base if config.get('research_partition') else base|{'holdout_plan.json'}
 allowed=base|holdout|{'holdout_plan.json'}
 if not isinstance(manifest,dict) or not required<=set(manifest) or set(manifest)-allowed:
  raise ValueError('预处理缓存证据范围不完整或含未知文件')
 for name,expected in manifest.items():
  path=(root/name).resolve()
  if root not in path.parents or not path.is_file() or sha(path)!=expected:
   raise ValueError('预处理缓存证据缺失或内容改变: '+name)
 plan=read_json(root/'holdout_plan.json',{}) if 'holdout_plan.json' in manifest else {}
 if not isinstance(plan,dict) or plan.get('status')=='SPLIT' and not holdout<=set(manifest):
  raise ValueError('预处理缓存缺少完整的留出数据证据')
 return manifest

def load_inputs(manifest,league,company,progress=None,included_sids=None,should_pause=None):
 def checkpoint():
  if should_pause and should_pause():
   from .mining import Paused
   raise Paused()
 index={};labels={};groups=defaultdict(list);audit=Counter();sources=[];max_dp=2;excluded=defaultdict(set);quality_notes=defaultdict(set);label_conflicts=Counter()
 # Read raw strings first to preserve all actual odds digits.
 for rec in manifest['files']:
  checkpoint()
  p=Path(rec['path'])
  if sha(p)!=rec['sha256']:raise ValueError('输入在运行期间改变: '+str(p))
  sources.append(rec)
  if rec['kind']=='quality':
   with verified_csv(rec) as f:
    reader=csv.DictReader(f);validate_csv_header(reader.fieldnames,p.name)
    for rn,a in enumerate(reader,2):
     if rn%2048==0:checkpoint()
     validate_quality_row(a,f'{p.name}:{rn}')
     sid=plain(a.get('sId'));scope=plain(a.get('scope'));reason=plain(a.get('reason'))
     applies=scope=='all' or (scope=='all_crown' and company=='皇冠') or (scope=='csl_crown' and company=='皇冠' and league=='中超') or (scope in ('pinbo','all_pinbo') and company=='平博')
     if not applies:
      audit['quality_scope_not_applied']+=1;audit['quality_scope_not_applied:'+scope]+=1;continue
     if not sid:continue
     hard=reason in ('pending_score_verify','remaining_CE','quarantine_rows_isolated') or reason.startswith(('csl_quality_C','csl_quality_D','csl_remaining_C','csl_remaining_D'))
     # Only explicitly registered observation reasons are notes; an unknown upstream reason excludes by default.
     note=reason=='remaining_suspicious'
     if not hard and not note:audit['quality_unknown_reason_excluded']+=1
     (quality_notes if note else excluded)[sid].add(reason or 'unknown_quality_reason')
   continue
  if rec['kind']!='index':continue
  with verified_csv(rec) as f:
   reader=csv.DictReader(f);validate_csv_header(reader.fieldnames,p.name)
   for rn,a in enumerate(reader,2):
    if rn%2048==0:checkpoint()
    if plain(a.get('联赛'))!=league:continue
    if included_sids is not None and plain(a['sId']) not in included_sids:continue
    sid=plain(a['sId']);new={'final':score(a['全场比分']),'result_status':plain(a['状态'])}
    # A nonempty but unreadable index result isolates the match, exactly like a malformed repeated quote label.
    if plain(a['全场比分']) not in ('','-','--') and MISSING in new['final']:
     new['conflict']=True;new['malformed_index_label_fields']=['final'];audit['malformed_index_label_rows']+=1;label_conflicts.update(['final'])
    if plain(a.get('日期')):new['date']=validate_date(a['日期'],f'{p.name}:{rn}')
    if plain(a.get('开球时间')):
     new['kickoff']=timestamp(a['开球时间'])
     if new['kickoff']==MISSING:raise ValueError(f'{p.name}:{rn}: 索引开球时间无效')
    if new['result_status'] in ('完','完场','已结束'):new['result_status']='完'
    if sid not in index:index[sid]=new
    else:
     old=index[sid];missing_values=('',MISSING,[MISSING,MISSING])
     conflicts={k:{'previous':old[k],'current':v,'source':rec['sha256'],'row':rn} for k,v in new.items() if k in old and old[k] not in missing_values and v not in missing_values and old[k]!=v}
     if conflicts:old['conflict']=True;old.setdefault('index_label_conflicts',[]).append(conflicts);label_conflicts.update(conflicts.keys())
     for k,v in new.items():
      if k not in old or old[k] in missing_values:old[k]=v
 for rec in manifest['files']:
  if rec['kind']!='quotes':continue
  p=Path(rec['path'])
  verify_input_records([rec])
  if progress:progress(message='读取本联赛报价与已合并赛果标签',input_file=p.name,raw_events=audit['quote_rows'])
  with verified_csv(rec) as f:
   reader=csv.DictReader(f);validate_csv_header(reader.fieldnames,p.name);missing=REQUIRED-set(reader.fieldnames or ())
   if missing:raise ValueError(f'{p.name}: 完整指数CSV缺少必要列: '+','.join(sorted(missing)))
   for rn,a in enumerate(reader,2):
    if rn%2048==0:checkpoint()
    if plain(a.get('联赛'))!=league or plain(a.get('公司'))!=company:continue
    if included_sids is not None and plain(a['sId']) not in included_sids:continue
    audit['quote_rows']+=1
    closed=validate_quote_fields(a.get('日期'),a.get('封盘'),f'{p.name}:{rn}')
    sid=plain(a['sId']);mk={'大小球':0,'让球':1}.get(plain(a['盘口类型']));st={'早':0,'即':0,'滚':1}.get(plain(a['状态']))
    if mk is None or st is None:audit['unsupported_market_or_status_rows']+=1;continue
    t=timestamp(a.get('变化时间',''))
    # A supported quote without identity or change time would silently vanish from research; inspection rejects it as well.
    if not sid or t==MISSING:raise ValueError(f'{p.name}:{rn}: sId缺失或变化时间不是 YYYY-MM-DD HH:MM')
    for key in ('上水/大球','下水/小球'):
     max_dp=max(max_dp,water_decimal_places(plain(a[key])))
    final=score(a.get('全场比分',''));date=plain(a.get('日期',''));status=plain(a.get('比赛状态',''))
    label={'final':final,'date':date,'kickoff':timestamp(a.get('开球时间','')),'result_status':'完' if status in ('完','完场','已结束') else status,'label_source':'joined_index_columns' if rec.get('joined_results') else 'quote_repeated_label'}
    if sid in labels:
     missing_values=('',MISSING,[MISSING,MISSING])
     changed=[key for key in ('final','date','kickoff','result_status') if labels[sid][key] not in missing_values and label[key] not in missing_values and labels[sid][key]!=label[key]]
     if changed:labels[sid]['conflict']=True;label_conflicts.update(changed)
     for key in ('final','date','kickoff','result_status'):
      if labels[sid][key] in missing_values and label[key] not in missing_values:
       labels[sid][key]=label[key];audit['repeated_label_missing_filled:'+key]+=1
    else:labels[sid]=label
    # Missing repeated labels may be completed; malformed nonempty labels must
    # still exclude the match instead of disappearing behind another row.
    malformed=[key for key,column in (('final','全场比分'),('kickoff','开球时间')) if plain(a.get(column)) not in ('','-','--') and label[key] in (MISSING,[MISSING,MISSING])]
    if malformed:
     labels[sid]['conflict']=True
     labels[sid]['malformed_repeated_label_fields']=sorted(set(labels[sid].get('malformed_repeated_label_fields',[]))|set(malformed))
     audit['malformed_repeated_label_rows']+=1;label_conflicts.update(malformed)
    mn=plain(a.get('比赛分钟',''));minute=int(mn) if mn.isdigit() else -2 if mn=='中场' else MISSING
    current_score=score(a.get('当时比分',''))
    if not st:
     if current_score[0]!=MISSING and current_score!=[0,0]:
      audit['prematch_nonzero_score']+=1;labels[sid]['prematch_score_conflict']=True
     elif current_score[0]==MISSING:
      if plain(a.get('当时比分')) in ('','-','--'):
       current_score=[0,0];audit['prematch_missing_score_normalized']+=1
      else:audit['prematch_unparsed_score']+=1;labels[sid]['prematch_score_conflict']=True
    e={'sid':sid,'market':mk,'phase':st,'ts':t,'source':rec['sha256'],'row':rn,'minute':minute,'minute_raw':mn,'status':plain(a['状态']),
       'l_raw':plain(a.get('盘口数值','')),'w0_raw':plain(a.get('上水/大球','')),'w1_raw':plain(a.get('下水/小球','')),
       'score':current_score,'closed':closed}
    groups[(sid,mk,st)].append(e)
 for sid,l in labels.items():
  if sid in index:
   conflicts={k:{'quotes':l[k],'index':index[sid][k]} for k in ('final','date','kickoff','result_status') if k in index[sid] and l[k] not in ('',MISSING,[MISSING,MISSING]) and index[sid][k] not in ('',MISSING,[MISSING,MISSING]) and index[sid][k]!=l[k]}
   if conflicts:l['conflict']=True;l['index_quote_conflicts']=conflicts;label_conflicts.update(conflicts.keys())
   # Missing index cells are absence of information, not an instruction to erase
   # a complete repeated result carried by the quote archive.
   for key,value in index[sid].items():
    if key in ('final','date','kickoff','result_status') and value in ('',MISSING,[MISSING,MISSING]):continue
    l[key]=value
   l['label_source']='index'
  validate_date(l['date'],sid)
  l['eligible']=not l.get('conflict') and l['final'][0]!=MISSING and l['result_status'] not in ('推迟','腰斩','中断','取消','未','未开','待定')
  l['quality_status']='UPLOADED_LABEL_UNVERIFIED'
  if l.get('prematch_score_conflict'):l['eligible']=False;audit['prematch_score_excluded_matches']+=1
  if not l['result_status']:audit['result_status_missing_trial_only']+=1
  if l.get('conflict'):audit['conflicting_labels_excluded']+=1
  if l['result_status'] and l['result_status'] not in ('完','完场','已结束'):
   l['eligible']=False;audit['unknown_result_status']+=1
  if sid in excluded:
   l['eligible']=False;l['quality_exclusions']=sorted(excluded[sid]);audit['source_quality_excluded_matches']+=1
  if sid in quality_notes:l['quality_notes']=sorted(quality_notes[sid]);audit['source_quality_noted_matches']+=1
  try:l['year']=int(l['date'][:4])
  except ValueError:l['year']=0;l['eligible']=False
 scale=10**max_dp;contract=make_contract(league,company,scale);events=[];sidmap={s:i for i,s in enumerate(sorted(labels))}
 for (sid,mk,st),rr in sorted(groups.items()):
  checkpoint()
  # Cross-file ties lack reliable ordering: stop, never fabricate a sequence.
  at=defaultdict(set)
  for e in rr:at[e['ts']].add(e['source'])
  if any(len(v)>1 for v in at.values()):raise ValueError(f'{sid}: 同盘口同阶段跨文件同分钟顺序不明，请使用不重叠的源文件')
  rr.sort(key=lambda e:(e['ts'],e['row']))
  last_known_score=None;last_open_quote=None;awaiting_score_refresh=False
  for e in rr:
   e['line']=scaled(e.pop('l_raw'),4);e['water']=[scaled(e.pop('w0_raw'),scale),scaled(e.pop('w1_raw'),scale)]
   structurally_open=not e['closed'] and e['line']!=MISSING and min(e['water'])>0 and (mk==1 or e['line']>=0)
   if st and e['score'][0]!=MISSING and last_known_score is not None and e['score']!=last_known_score:awaiting_score_refresh=True
   fingerprint=(e['line'],*e['water'])
   unchanged_after_score=False
   if st and awaiting_score_refresh and structurally_open:
    if last_open_quote is not None and fingerprint==last_open_quote:unchanged_after_score=True
    else:awaiting_score_refresh=False
   # Blank score cells do not erase goals already observed in this match/market.
   # Use only the latest score known at this row, allowing explicit corrections,
   # and preserve the original score cell for audit and handicap settlement.
   known_score=e['score'] if e['score'][0]!=MISSING else last_known_score
   impossible_total=bool(st and mk==0 and known_score is not None and e['line']!=MISSING and e['line']<4*sum(known_score))
   e['quality_blocked']=impossible_total or unchanged_after_score
   e['valid']=structurally_open and not e['quality_blocked']
   e['mid']=sidmap[sid];e['eid']=len(events)
   e['event_key']=f"{e['source']}:{e['row']}"
   if e['line']==MISSING and not e['closed']:audit['missing_or_nonquarter_line']+=1
   if impossible_total:audit['live_total_line_below_known_goals']+=1
   if unchanged_after_score:audit['live_score_change_quote_unchanged']+=1
   if not e['valid']:audit['invalid_or_closed']+=1
   if st and e['score'][0]!=MISSING and labels[sid]['final'][0]!=MISSING and any(e['score'][i]>labels[sid]['final'][i] for i in (0,1)):
    audit['score_exceeds_unverified_final']+=1
   if e['score'][0]!=MISSING:last_known_score=e['score']
   if structurally_open:last_open_quote=fingerprint
   events.append(bind_event(e,contract))
 audit['usable_events']=sum(e['valid'] for e in events);audit['matches']=len(labels)
 audit['eligible_matches']=sum(l['eligible'] for l in labels.values())
 verify_input_records(sources)
 return events,labels,scale,{'counts':dict(audit),'label_conflicts_by_field':dict(label_conflicts),'inputs':sources,'league':league,'company':company,'water_scale':scale,'contract':contract,'timestamp':'UTC+08:00 source wall-minute','settlement_status':'上传标签试算；未独立核验，不是实盘资格',
  'source_quality_policy':'source_hard_and_quarantine_v1','source_quality_loaded':any(r['kind']=='quality' for r in sources),'independent_label_verification':False}
