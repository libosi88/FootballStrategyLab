"""Check every pinned artifact against official PyPI metadata; no lock mutation."""
import argparse,datetime,importlib.metadata,json,re,sys,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import ROOT,atomic_json,sha

def locked_components(root):
    for name in ('requirements.lock','installer.lock'):
        text=(root/name).read_text(encoding='utf-8');pins=list(re.finditer(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)',text,re.M))
        for i,pin in enumerate(pins):
            body=text[pin.end():pins[i+1].start() if i+1<len(pins) else len(text)]
            yield name,pin[1],pin[2],set(re.findall(r'--hash=sha256:([0-9a-f]{64})',body))

def audit(root=ROOT):
    rows=[]
    for lock,name,version,hashes in locked_components(Path(root)):
        url=f'https://pypi.org/pypi/{name}/{version}/json';row={'name':name,'version':version,'lock':lock,'official_metadata':url,'allowed_hash_count':len(hashes)}
        try:
            request=urllib.request.Request(url,headers={'User-Agent':'FootballStrategyLab-dependency-audit'})
            with urllib.request.urlopen(request,timeout=20) as response:metadata=json.load(response)
            published={item['digests']['sha256'] for item in metadata['urls']}
            row['unpublished_locked_hashes']=sorted(hashes-published)
            row['vulnerabilities']=[v for v in metadata.get('vulnerabilities',[]) if not v.get('withdrawn')]
            row['requires_python']=metadata['info'].get('requires_python')
            row['installed_version']=importlib.metadata.version(name)
            row['installed_version_matches']=row['installed_version']==version
            row['status']='PASS' if hashes and not row['unpublished_locked_hashes'] and not row['vulnerabilities'] and row['installed_version_matches'] else 'FAIL'
        except Exception as error:row.update(status='NOT_VERIFIED',error=str(error))
        rows.append(row)
    statuses={r['status'] for r in rows}
    return {'status':'FAIL' if 'FAIL' in statuses else 'NOT_VERIFIED' if 'NOT_VERIFIED' in statuses or not rows else 'PASS',
            'checked_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'components':rows,
            'locks':{n:sha(Path(root)/n) for n in ('requirements.lock','installer.lock')},
            'scope':'official PyPI known advisories and locked distribution hashes at check time; not proof of absence of all vulnerabilities'}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=audit();atomic_json(args.output,result);print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
