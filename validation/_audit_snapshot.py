import hashlib, os, sys, time


def snap():
    hashes = []
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in ('.venv', '__pycache__', 'node_modules')
                   and 'import_cache' not in os.path.join(root, d)]
        for f in files:
            if not f.endswith(('.py', '.js', '.json', '.md', '.bat', '.sh', '.ps1',
                               '.txt', '.lock', '.html', '.css')):
                continue
            p = os.path.join(root, f)
            try:
                with open(p, 'rb') as fh:
                    h = hashlib.sha256(fh.read()).hexdigest()
                hashes.append((p, h))
            except Exception:
                pass
    return hashes


s1 = snap()
d1 = hashlib.sha256(
    '\n'.join(sorted(f'{h}  {p}' for p, h in s1)).encode()
).hexdigest()
print(f'T1: {time.strftime("%H:%M:%S")} files={len(s1)} digest={d1[:16]}', flush=True)
time.sleep(30)
s2 = snap()
d2 = hashlib.sha256(
    '\n'.join(sorted(f'{h}  {p}' for p, h in s2)).encode()
).hexdigest()
print(f'T2: {time.strftime("%H:%M:%S")} files={len(s2)} digest={d2[:16]}', flush=True)
if d1 == d2:
    print('STABLE')
else:
    print('UNSTABLE')
    m1 = dict(s1)
    m2 = dict(s2)
    diff = []
    for k in set(m1) | set(m2):
        if m1.get(k) != m2.get(k):
            diff.append(k)
    print(f'Changed files ({len(diff)}):')
    for k in diff[:50]:
        print(' -', k)
