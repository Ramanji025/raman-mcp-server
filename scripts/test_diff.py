from git import Repo

repo = Repo('data/repos/dic-store-service')
ref = 'origin/DE194624-fixing-version-delete-error'
base = repo.merge_base(ref, 'origin/master')[0]
diff_text = repo.git.diff(base.hexsha, ref, '--unified=6', '--diff-filter=ACMR')

cur_file = cur_change = None
cur_hunks = []
cur_hunk = None
files = {}

def sh():
    if cur_hunk is not None and cur_file:
        cur_hunks.append(cur_hunk)

def sf():
    if cur_file:
        sh()
        files[cur_file] = {
            'path': cur_file, 'change': cur_change, 'hunks': list(cur_hunks),
            'lines_added': sum(len(h['after_lines']) for h in cur_hunks),
            'lines_removed': sum(len(h['before_lines']) for h in cur_hunks),
        }

for ln in diff_text.splitlines():
    if ln.startswith('diff --git '):
        sf()
        cur_file = ln.split(' b/', 1)[-1].strip()
        cur_change = 'M'; cur_hunks = []; cur_hunk = None
    elif ln.startswith('new file'):    cur_change = 'A'
    elif ln.startswith('deleted file'): cur_change = 'D'
    elif ln.startswith('@@ '):
        sh()
        cur_hunk = {'header': ln, 'before_lines': [], 'after_lines': [], 'context_lines': []}
    elif cur_hunk is not None and not ln.startswith(('---', '+++')):
        if ln.startswith('-'):   cur_hunk['before_lines'].append(ln[1:])
        elif ln.startswith('+'): cur_hunk['after_lines'].append(ln[1:])
        else:                    cur_hunk['context_lines'].append(ln[1:] if ln.startswith(' ') else ln)
sf()

java = {k: v for k, v in files.items() if k.endswith('.java')}
total_hunks = sum(len(v['hunks']) for v in java.values())
has_before = sum(1 for v in java.values() for h in v['hunks'] if h['before_lines'])
has_after  = sum(1 for v in java.values() for h in v['hunks'] if h['after_lines'])
print(f'Java files: {len(java)}, total hunks: {total_hunks}')
print(f'Hunks with BEFORE lines: {has_before}, with AFTER lines: {has_after}')

for fp, fd in list(java.items())[:4]:
    for h in fd['hunks'][:1]:
        if h['before_lines'] or h['after_lines']:
            print(f'\n--- {fp}')
            print(f'  BEFORE: {h["before_lines"][:3]}')
            print(f'  AFTER:  {h["after_lines"][:3]}')
            break
