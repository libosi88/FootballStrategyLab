"""Regenerate application identity and allowed distribution hashes from locks."""
import sys,re,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import ROOT,VERSION,source_fingerprint,atomic_json

components=[]
for lock in ('requirements.lock','installer.lock'):
    text=(ROOT/lock).read_text(encoding='utf-8')
    pins=list(re.finditer(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)',text,re.M))
    for index,pin in enumerate(pins):
        body=text[pin.end():pins[index+1].start() if index+1<len(pins) else len(text)]
        hashes=sorted(set(re.findall(r'--hash=sha256:([0-9a-f]{64})',body)))
        if not hashes:raise ValueError('依赖缺少锁定哈希: '+pin[1])
        components.append({'type':'library','name':pin[1],'version':pin[2],'purl':f'pkg:pypi/{pin[1]}@{pin[2]}','hashes':[{'alg':'SHA-256','content':h} for h in hashes],'properties':[{'name':'fsl:hash-scope','value':'allowed distribution artifacts from '+lock}]})
sbom={'bomFormat':'CycloneDX','specVersion':'1.5','version':1,'metadata':{'component':{'type':'application','name':'FootballStrategyLab','version':VERSION,'hashes':[{'alg':'SHA-256','content':source_fingerprint()}],'properties':[{'name':'fsl:hash-scope','value':'FSL_RESEARCH_INPUTS_V6 canonical research source manifest; execution identity is separate; not a ZIP digest'}]}},'components':components}
atomic_json(ROOT/'sbom.cdx.json',sbom)
print(json.dumps({'version':VERSION,'components':len(components),'distribution_hashes':sum(len(c['hashes']) for c in components)},ensure_ascii=False))
