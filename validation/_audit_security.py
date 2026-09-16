import sys, os, re
sys.path.insert(0, '.')

# Check for security issues in server.py
with open('lab/server.py', 'r', encoding='utf-8') as f:
    content = f.read()

issues = []
if 'eval(' in content: issues.append('eval() found')
if 'exec(' in content: issues.append('exec() found')
if 'subprocess.call' in content and 'shell=True' in content: issues.append('shell=True subprocess')
if 'os.system' in content: issues.append('os.system found')
print('Security issues in server.py:', issues if issues else 'None found')

# Check for SQL injection patterns
sql_lines = []
for line in content.split('\n'):
    if 'execute(' in line and ('%' in line or 'f"' in line or "f'" in line):
        sql_lines.append(line.strip())
if sql_lines:
    print('Potential SQL injection patterns:', len(sql_lines))
    for l in sql_lines[:3]:
        print(' -', l[:100])
else:
    print('SQL injection patterns: None found')

# Check for hardcoded secrets
if re.search(r'(password|secret|token|api_key)\s*=\s*["\'][^"\']+["\']', content, re.IGNORECASE):
    print('Hardcoded secrets: Found')
else:
    print('Hardcoded secrets: None found')

# Check all lab files for common issues
print('\n=== Checking all lab/*.py files ===')
all_issues = []
for f in os.listdir('lab'):
    if not f.endswith('.py'):
        continue
    path = os.path.join('lab', f)
    with open(path, 'r', encoding='utf-8') as fh:
        code = fh.read()
    # Check for eval/exec
    if 'eval(' in code and 'eval()' not in code:
        all_issues.append((path, 'eval() usage'))
    if 'exec(' in code and 'exec()' not in code:
        all_issues.append((path, 'exec() usage'))
    # Check for shell=True
    if 'shell=True' in code:
        all_issues.append((path, 'shell=True subprocess'))
    # Check for os.system
    if 'os.system(' in code:
        all_issues.append((path, 'os.system usage'))

if all_issues:
    print('Issues found:', len(all_issues))
    for path, issue in all_issues[:10]:
        print(f' - {path}: {issue}')
else:
    print('No common security issues found in lab/*.py')

# Check for TODO/FIXME/HACK comments
print('\n=== TODO/FIXME/HACK comments ===')
todo_count = 0
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ('.venv', '__pycache__', 'node_modules', 'workspace', 'validation', 'setup_cache')]
    for f in files:
        if not f.endswith(('.py', '.js')):
            continue
        path = os.path.join(root, f)
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
                for i, line in enumerate(fh, 1):
                    if re.search(r'(TODO|FIXME|HACK|XXX)', line, re.IGNORECASE):
                        todo_count += 1
                        if todo_count <= 5:
                            print(f' - {path}:{i}: {line.strip()[:80]}')
        except:
            pass
print(f'Total TODO/FIXME/HACK comments: {todo_count}')
