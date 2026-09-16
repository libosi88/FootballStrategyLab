"""Whitelisted source snapshots; file integrity is not research certification."""
from pathlib import Path
import zipfile,os,shutil
from .common import ROOT,sha,digest,canonical,atomic_replace

ROOT_FILES=('app.py','requirements.txt','requirements.lock','installer.lock','sbom.cdx.json','README_先读.md','本版实测与未完成项.md',
            'setup_windows.ps1','启动工作台.bat','start_windows.bat','start_existing_python.bat',
            'start_linux.sh','build_source_release.py','.gitignore','.gitattributes')
def release_files(root=None):
    root=Path(root or ROOT).resolve();files=[]
    suffixes={'lab':{'.py'},'tests':{'.py','.json','.csv','.txt'},'web':{'.html','.js','.css','.svg','.png','.jpg','.ico','.woff','.woff2'},'config':{'.json','.toml','.yaml','.yml'},'docs':{'.md','.json','.txt'},'demo':{'.py','.csv','.json','.md'}}
    for folder in ('lab','tests','web','config','docs','demo'):
        for p in (root/folder).rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and p.suffix.lower() in suffixes[folder]:
                if root not in p.resolve().parents:raise ValueError('源码发布白名单包含越界链接')
                files.append(p)
    files.extend(root/name for name in ROOT_FILES if (root/name).is_file())
    files.extend(root/'validation'/name for name in ('README.md','frozen_regression.py','historical_acceptance.py','finalize_review_status.py','preserve_frozen_revision.py','update_sbom.py','check_dependencies.py') if (root/'validation'/name).is_file())
    for p in files:
        if root not in p.resolve().parents:raise ValueError('源码发布白名单包含越界链接')
    return sorted(set(files),key=lambda p:p.relative_to(root).as_posix())

def release_fingerprint(root=None):
    root=Path(root or ROOT).resolve()
    return manifest_fingerprint({p.relative_to(root).as_posix():sha(p) for p in release_files(root)})

def manifest_fingerprint(manifest):return digest(sorted(manifest.items()))

def freeze_release_tree(destination,root=None):
    """Copy exactly one verified whitelist snapshot for every output in a bundle."""
    root=Path(root or ROOT).resolve();destination=Path(destination).resolve()
    if destination==root or any(root/folder in destination.parents or destination==root/folder for folder in ('lab','tests','web','config','docs','demo')):
        raise ValueError('源码快照不能写入发布白名单目录')
    files=release_files(root)
    manifest={p.relative_to(root).as_posix():sha(p) for p in files}
    destination.mkdir(parents=True,exist_ok=False)
    for name,expected in manifest.items():
        target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(root/name,target)
        if sha(target)!=expected:raise ValueError('冻结期间源码发生改变: '+name)
    if release_fingerprint(root)!=manifest_fingerprint(manifest):raise ValueError('冻结期间发布文件集合发生改变')
    return manifest

def workspace_identity(workspace,root=None):
    return digest([os.path.normcase(str(Path(root or ROOT).resolve())),os.path.normcase(str(Path(workspace).resolve()))])

def build_snapshot(target,root=None):
    root=Path(root or ROOT).resolve();target=Path(target).resolve();target.parent.mkdir(parents=True,exist_ok=True)
    files=release_files(root)
    if target in files or any(root/folder in target.parents for folder in ('lab','tests','web','config','docs','demo')):raise ValueError('发布目标不能位于源码白名单目录')
    if target.exists():raise FileExistsError('源码快照已存在，请使用新文件名；不覆盖历史交付物')
    manifest={p.relative_to(root).as_posix():sha(p) for p in files}
    import ast
    tree=ast.parse((root/'lab/common.py').read_text(encoding='utf-8-sig'))
    version=next((node.value.value for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in node.targets) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str)),None)
    if version is None:raise ValueError('源码中缺少明确的VERSION，拒绝猜测发布版本')
    report={'version':version,'scope':'SOURCE_SNAPSHOT_NOT_RESEARCH_CERTIFICATE','files':manifest,
            'release_fingerprint':manifest_fingerprint(manifest),'contains_local_runtime_or_research_data':False}
    temporary=target.with_suffix(target.suffix+'.tmp')
    try:
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for p in files:z.write(p,p.relative_to(root).as_posix())
            z.writestr('release_manifest.json',canonical(report))
        with zipfile.ZipFile(temporary) as z:
            import hashlib
            if z.testzip() is not None or any(hashlib.sha256(z.read(name)).hexdigest()!=h for name,h in manifest.items()):raise ValueError('源码包完整性核对失败')
        if any(sha(root/name)!=h for name,h in manifest.items()):raise ValueError('封装期间源码发生改变')
        atomic_replace(temporary,target)
    finally:
        if temporary.exists():temporary.unlink()
    return {'archive':str(target),'sha256':sha(target),'file_count':len(files),**{k:v for k,v in report.items() if k!='files'}}
