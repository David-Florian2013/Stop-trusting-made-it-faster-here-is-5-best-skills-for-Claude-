#!/usr/bin/env python3
"""perf-verifier: turns "this should be faster" into measurements.

  pf.py profile [--top N] -- CMD ...                      where the time goes (python or node commands)
  pf.py compare [--base REF | --a CMD --b CMD] [--runs N] [--verify CMD] [--expect faster|same] -- CMD ...
  pf.py scale --sizes 1000,2000,4000 [--runs N] -- CMD ... {N} ...   how time grows with input size
  pf.py bench [--runs N] -- CMD ...                       time and peak memory of one command
  pf.py scan [PATH ...] [--base REF] [--all-lines] [--low]   slow patterns in the changed code
  pf.py check [PATH ...] [--cmd CMD] [--base REF] [--expect faster|same] [--verify CMD]   all of it + one summary line
"""
import argparse, ast, bisect, hashlib, json, math, os, re, shlex, shutil, signal, subprocess, sys, tarfile, tempfile, threading, time
from collections import namedtuple
from functools import lru_cache

Finding = namedtuple('Finding', 'path line end rule sev msg fix')
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', '__pycache__', '.tox', '.mypy_cache', '.next', '.nuxt', 'coverage', 'vendor',
             'target', '.pytest_cache', 'site-packages', '.venv', 'venv', '.ruff_cache'}
CODE_EXT = {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'}
JS_EXT = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'}
MAX_BYTES = 1_500_000
SEV_ORDER = {'high': 0, 'medium': 1, 'low': 2}

def norm(p):
    return p.replace('\\', '/')

def read_text(path):
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return None
    if b'\x00' in data[:4096]:
        return None
    return data.decode('utf-8-sig', errors='replace').replace('\r\n', '\n').replace('\r', '\n')

def git(*args, cwd=None):
    try:
        r = subprocess.run(['git', *args], capture_output=True, text=True, cwd=cwd, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None

def repo_root(cwd):
    out = git('rev-parse', '--show-toplevel', cwd=cwd)
    return out.strip() if out else None

def walk_files(root):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not os.path.exists(os.path.join(d, x, 'pyvenv.cfg'))]
        for f in files:
            yield os.path.join(d, f)

# ================================================================ targets (what to scan)

class Targets:
    def __init__(self):
        self.root, self.files, self.mode, self.base = os.getcwd(), {}, '', None

def diff_added(root, base, path):
    out = git('diff', '-U0', '--no-renames', base, '--', path, cwd=root)
    added = set()
    for m in re.finditer(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', out or '', re.M):
        start, cnt = int(m.group(1)), int(m.group(2)) if m.group(2) is not None else 1
        added.update(range(start, start + cnt))
    return added

def get_targets(paths, base):
    t = Targets()
    root = repo_root(os.getcwd())
    if paths:
        t.root, t.mode = root or os.getcwd(), 'explicit paths'
        for p in paths:
            if os.path.isdir(p):
                for f in walk_files(p):
                    if os.path.splitext(f)[1] in CODE_EXT:
                        t.files[os.path.abspath(f)] = None
            elif os.path.exists(p):
                t.files[os.path.abspath(p)] = None
        return t
    if not root:
        t.mode = 'no git: all code files'
        for f in walk_files(os.getcwd()):
            if os.path.splitext(f)[1] in CODE_EXT and len(t.files) < 400:
                t.files[os.path.abspath(f)] = None
        return t
    t.root = root
    has_base = git('rev-parse', '--verify', '--quiet', base + '^{commit}', cwd=root) is not None
    t.base = base if has_base else None
    untracked = (git('ls-files', '--others', '--exclude-standard', cwd=root) or '').split('\n')
    if has_base:
        t.mode = f'changed lines vs {base}'
        for line in (git('diff', '--name-status', '--no-renames', base, cwd=root) or '').split('\n'):
            if not line.strip():
                continue
            st, _, p = line.partition('\t')
            ap = os.path.join(root, p)
            if not st.startswith('D') and os.path.exists(ap):
                t.files[ap] = diff_added(root, base, p)
    else:
        t.mode = 'git repo without commits: all files'
        untracked += (git('ls-files', cwd=root) or '').split('\n')
    for p in untracked:
        if p.strip() and os.path.exists(os.path.join(root, p)):
            t.files[os.path.join(root, p)] = None
    t.files = {p: a for p, a in t.files.items() if not any(seg in SKIP_DIRS for seg in norm(os.path.relpath(p, root)).split('/'))}
    return t

def rel(t, p):
    try:
        return norm(os.path.relpath(p, t.root))
    except ValueError:
        return norm(p)

SUPPRESS = re.compile(r'pf:\s*ignore(?:\s+([A-Z0-9-]+))?')

def suppressed(raw, line, rule):
    for k in (line - 1, line - 2):
        if 0 <= k < len(raw):
            m = SUPPRESS.search(raw[k])
            if m and (not m.group(1) or m.group(1) == rule) and (k == line - 1 or raw[k].lstrip().startswith(('#', '//', '/*'))):
                return True
    return False

# ================================================================ scan: Python (AST)

def dotted(n):
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        b = dotted(n.value)
        return (b + '.' + n.attr) if b else n.attr
    return ''

def root_name(n):
    while isinstance(n, (ast.Attribute, ast.Subscript, ast.Call)):
        n = n.value if not isinstance(n, ast.Call) else n.func
    return n.id if isinstance(n, ast.Name) else ''

def stored_names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}

def names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

DB_RECV = re.compile(r'(?i)^(?:\w*_)?(?:cur|cursor|conn|connection|db|session|engine|database|dbh|pool)$')
HTTP_RECV = re.compile(r'(?i)^(?:\w*_)?(?:session|client|http|api)$')
HTTP_MODULES = {'requests', 'httpx', 'aiohttp', 'urllib3'}
HTTP_METHODS = {'get', 'post', 'put', 'delete', 'patch', 'head', 'options', 'request'}
CONNECT_CALLS = {'sqlite3.connect', 'psycopg2.connect', 'psycopg.connect', 'pymysql.connect', 'mysql.connector.connect', 'redis.Redis', 'redis.StrictRedis',
                 'pymongo.MongoClient', 'MongoClient', 'requests.Session', 'httpx.Client', 'httpx.AsyncClient', 'boto3.client', 'boto3.resource',
                 'create_engine', 'sqlalchemy.create_engine', 'smtplib.SMTP', 'smtplib.SMTP_SSL', 'ftplib.FTP', 'socket.create_connection', 'aiohttp.ClientSession'}
BLOCKING_IN_ASYNC = {'time.sleep', 'urllib.request.urlopen', 'urlopen', 'subprocess.run', 'subprocess.call', 'subprocess.check_output',
                     'subprocess.check_call', 'os.system', 'socket.create_connection', 'shutil.copytree', 'shutil.rmtree'}
ORM_METHODS = {'get', 'filter', 'all', 'first', 'exclude', 'exists', 'count', 'create', 'get_or_create', 'update', 'delete', 'last', 'aggregate', 'values', 'values_list'}

def small_iter(it):
    if isinstance(it, (ast.List, ast.Tuple, ast.Set)) and len(it.elts) <= 8:
        return True
    if isinstance(it, ast.Call) and dotted(it.func) == 'range' and it.args and all(isinstance(a, ast.Constant) and isinstance(a.value, int) and a.value <= 8 for a in it.args):
        return True
    if isinstance(it, ast.Constant):
        return True
    return False

class Scope:
    def __init__(self, node, is_async=False):
        self.node, self.is_async = node, is_async
        self.kinds = {}     # name -> 'list' | 'hashed' | 'str' | None(ambiguous)
        self.df = set()

def value_kind(v):
    if isinstance(v, (ast.List, ast.ListComp)):
        return 'list'
    if isinstance(v, (ast.Set, ast.SetComp, ast.Dict, ast.DictComp)):
        return 'hashed'
    if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Mult) and isinstance(v.left, ast.List):
        return 'list'
    if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add) and 'list' in (value_kind(v.left), value_kind(v.right)):
        return 'list'
    if isinstance(v, ast.Call):
        n = dotted(v.func)
        if n in ('list', 'sorted', 'reversed'):
            return 'list'
        if n in ('set', 'frozenset', 'dict', 'defaultdict', 'collections.defaultdict', 'Counter', 'collections.Counter', 'OrderedDict'):
            return 'hashed'
        if isinstance(v.func, ast.Attribute) and v.func.attr in ('split', 'splitlines', 'readlines', 'keys_list'):
            return 'list'
    if isinstance(v, (ast.Constant,)) and isinstance(v.value, str):
        return 'str'
    if isinstance(v, ast.JoinedStr):
        return 'str'
    return None

def build_scope(node, is_async=False):
    sc = Scope(node, is_async)
    body_nodes = []
    def collect(n):
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            body_nodes.append(ch)
            collect(ch)
    collect(node)
    seen = {}
    for n in body_nodes:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            k = value_kind(n.value)
            nm = n.targets[0].id
            if nm in seen and seen[nm] != k:
                seen[nm] = 'mixed'
            else:
                seen.setdefault(nm, k)
            if isinstance(n.value, ast.Call) and dotted(n.value.func) in ('pd.DataFrame', 'pandas.DataFrame', 'pd.read_csv', 'pd.read_json', 'pd.read_parquet'):
                sc.df.add(nm)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            ann = ast.unparse(n.annotation) if hasattr(ast, 'unparse') else ''
            k = 'list' if re.match(r'(?:typing\.)?(?:List|list)\b', ann) else (value_kind(n.value) if n.value is not None else None)
            seen[n.target.id] = 'mixed' if n.target.id in seen and seen[n.target.id] != k else k
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for a in node.args.args + node.args.kwonlyargs:
            if a.annotation is not None and hasattr(ast, 'unparse') and re.match(r'(?:typing\.)?(?:List|list|Sequence|Iterable)\b\[?', ast.unparse(a.annotation)) \
                    and not re.match(r'(?:typing\.)?(?:Iterable)', ast.unparse(a.annotation)):
                seen.setdefault(a.arg, 'list')
    sc.kinds = {k: v for k, v in seen.items() if v in ('list', 'hashed', 'str')}
    return sc

class Loop:
    def __init__(self, node, kind, targets, iter_root):
        self.node, self.kind, self.targets, self.iter_root = node, kind, targets, iter_root
        self.stored = stored_names(node)

class PyPerf(ast.NodeVisitor):
    def __init__(self, path, raw, tree):
        self.path, self.raw, self.f = path, raw, []
        self.loops, self.scopes = [], [build_scope(tree)]
        self.seen = set()

    def add(self, node, rule, sev, msg, fix):
        line = getattr(node, 'lineno', 1)
        key = (line, rule)
        if key in self.seen or suppressed(self.raw, line, rule):
            return
        self.seen.add(key)
        self.f.append(Finding(self.path, line, getattr(node, 'end_lineno', line) or line, rule, sev, msg, fix))

    @property
    def sc(self):
        return self.scopes[-1]

    def in_loop(self):
        return bool(self.loops)

    def visit_FunctionDef(self, node):
        self.scopes.append(build_scope(node, isinstance(node, ast.AsyncFunctionDef)))
        saved, self.loops = self.loops, []
        self.generic_visit(node)
        self.loops = saved
        self.scopes.pop()
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        saved, self.loops = self.loops, []
        self.generic_visit(node)
        self.loops = saved

    def _loop_targets(self, target):
        return stored_names(target) if target is not None else set()

    def visit_For(self, node):
        self._for(node, 'for')
    def visit_AsyncFor(self, node):
        self._for(node, 'async for')

    def _for(self, node, kind):
        self.visit(node.iter)
        if isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Attribute) and node.iter.func.attr == 'iterrows':
            self.add(node.iter, 'PY-ITERROWS', 'medium', 'DataFrame.iterrows() builds a Series per row (often 10-100x slower than vectorised code)', 'vectorise with column operations, or itertuples() if a loop is unavoidable')
        lp = Loop(node, kind, self._loop_targets(node.target), root_name(node.iter))
        lp.small = small_iter(node.iter)
        self.nested_join(node, lp)
        self.await_in_loop(node, lp)
        self.loops.append(lp)
        for s in node.body + node.orelse:
            self.visit(s)
        self.loops.pop()

    def visit_While(self, node):
        self.visit(node.test)
        lp = Loop(node, 'while', set(), '')
        lp.small = False
        self.loops.append(lp)
        for s in node.body + node.orelse:
            self.visit(s)
        self.loops.pop()

    def _comp(self, node, gens, parts):
        for i, g in enumerate(gens):
            self.visit(g.iter)
        lps = []
        for g in gens:
            lp = Loop(node, 'comprehension', self._loop_targets(g.target), root_name(g.iter))
            lp.small = small_iter(g.iter)
            lps.append(lp)
            self.loops.append(lp)
        for g in gens:
            for c in g.ifs:
                self.visit(c)
        for p in parts:
            self.visit(p)
        for _ in gens:
            self.loops.pop()
    def visit_ListComp(self, n): self._comp(n, n.generators, [n.elt])
    def visit_SetComp(self, n): self._comp(n, n.generators, [n.elt])
    def visit_GeneratorExp(self, n): self._comp(n, n.generators, [n.elt])
    def visit_DictComp(self, n): self._comp(n, n.generators, [n.key, n.value])

    def nested_join(self, outer, lp):
        for inner in ast.walk(outer):
            if inner is outer or not isinstance(inner, ast.For):
                continue
            if root_name(inner.iter) in lp.targets or (names_in(inner.iter) & lp.targets):
                continue
            it = stored_names(inner.target)
            for cond in ast.walk(inner):
                if isinstance(cond, ast.Compare) and len(cond.ops) == 1 and isinstance(cond.ops[0], (ast.Eq, ast.NotEq)):
                    a, b = root_name(cond.left), root_name(cond.comparators[0])
                    if (a in lp.targets and b in it) or (b in lp.targets and a in it):
                        self.add(inner, 'PY-NESTED-LOOP-JOIN', 'medium', 'nested loops matching items of two collections by equality: O(n*m)', 'build a dict/set index of one collection first, then look up in O(1)')
                        return

    def await_in_loop(self, node, lp):
        if not self.sc.is_async or lp.kind != 'for' or lp.small:
            return
        body = ast.Module(body=node.body, type_ignores=[])
        awaits = [n for n in ast.walk(body) if isinstance(n, ast.Await)]
        if not awaits or any(isinstance(n, (ast.Break, ast.Return, ast.Continue)) for n in ast.walk(body)):
            return
        for aw in awaits:
            call = aw.value
            cname = dotted(call.func) if isinstance(call, ast.Call) else ''
            if re.search(r'sleep|delay|wait', cname, re.I):
                return
            if isinstance(call, ast.Call) and (names_in(call) & lp.targets):
                self.add(aw, 'PY-AWAIT-IN-LOOP', 'medium', 'await inside a for loop runs the iterations one after another', 'if iterations are independent: asyncio.gather(...) (bound it with a Semaphore); keep the loop only if order or rate limits matter')
                return

    # ---- statements inside loops
    def visit_Assign(self, node):
        if self.in_loop() and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            n = node.targets[0].id
            v = node.value
            kind = self.sc.kinds.get(n)
            if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add) and isinstance(v.left, ast.Name) and v.left.id == n:
                if kind == 'list' or isinstance(v.right, (ast.List, ast.ListComp)):
                    self.add(node, 'PY-LIST-CONCAT-LOOP', 'medium', f'`{n} = {n} + [...]` copies the whole list every iteration (quadratic)', f'use `{n}.append(...)` / `{n}.extend(...)`')
                elif kind == 'str':
                    self.add(node, 'PY-STR-CONCAT-LOOP', 'low', f'string built with `{n} = {n} + ...` in a loop', 'collect the parts in a list and "".join(parts) once')
            if isinstance(v, ast.Call) and dotted(v.func) in ('pd.concat', 'pandas.concat') and v.args and isinstance(v.args[0], (ast.List, ast.Tuple)) \
                    and any(isinstance(e, ast.Name) and e.id == n for e in v.args[0].elts):
                self.add(node, 'PY-DF-CONCAT-LOOP', 'medium', f'`{n} = pd.concat([{n}, ...])` in a loop copies the frame every iteration (quadratic)', 'collect the pieces in a list and concat once after the loop')
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        if self.in_loop() and isinstance(node.op, ast.Add) and isinstance(node.target, ast.Name) and self.sc.kinds.get(node.target.id) == 'str' \
                and not isinstance(node.value, ast.Constant):
            self.add(node, 'PY-STR-CONCAT-LOOP', 'low', f'string built with `{node.target.id} += ...` in a loop', 'collect the parts in a list and "".join(parts) once')
        self.generic_visit(node)

    def visit_Compare(self, node):
        if self.in_loop():
            for op, c in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and isinstance(c, ast.Name) and self.sc.kinds.get(c.id) == 'list':
                    lp = [l for l in self.loops if not l.small]
                    # the list must outlive one iteration: a list rebuilt inside the same loop body is tested once per pass, not n times
                    if lp and c.id not in lp[-1].stored:
                        self.add(node, 'PY-LIST-MEMBERSHIP-IN-LOOP', 'medium', f'`in {c.id}` scans the whole list on every iteration (O(n) each, O(n^2) total)', f'make `{c.id}` a set (or dict) - membership becomes O(1)')
        self.generic_visit(node)

    def visit_Call(self, node):
        name = dotted(node.func)
        last = name.split('.')[-1] if name else (node.func.attr if isinstance(node.func, ast.Attribute) else '')
        recv = dotted(node.func.value) if isinstance(node.func, ast.Attribute) else ''
        recv_root = recv.split('.')[-1] if recv else ''
        real_loops = [l for l in self.loops if not l.small]
        if last == 'iterrows' and not self.in_loop():
            pass
        if self.sc.is_async:
            if name in BLOCKING_IN_ASYNC or (name.split('.')[0] in HTTP_MODULES and last in HTTP_METHODS and name.split('.')[0] in ('requests', 'urllib3')):
                self.add(node, 'PY-BLOCKING-IN-ASYNC', 'high', f'{name}() blocks the event loop: nothing else runs until it returns', 'use the async equivalent (asyncio.sleep, httpx.AsyncClient, asyncio.create_subprocess_exec) or asyncio.to_thread(...)')
        if real_loops:
            lst = self.sc.kinds
            if last in ('execute', 'fetchone', 'fetchall', 'find_one', 'scalar', 'scalars') and DB_RECV.search(recv_root or '') or \
                    (last in ORM_METHODS and '.objects' in ('.' + recv)) or (last == 'query' and DB_RECV.search(recv_root or '')):
                self.add(node, 'PY-QUERY-IN-LOOP', 'high', 'database query inside a loop: one round trip per item (N+1)', 'fetch everything in one query (JOIN / WHERE id IN (...) / prefetch_related / select_related) before the loop')
            elif (name.split('.')[0] in HTTP_MODULES and last in HTTP_METHODS) or name in ('urlopen', 'urllib.request.urlopen') or \
                    (last in HTTP_METHODS and HTTP_RECV.search(recv_root or '') and recv_root not in lst):
                self.add(node, 'PY-HTTP-IN-LOOP', 'medium', 'network request inside a loop runs them one by one', 'batch the calls, cache repeated ones, or run them concurrently (ThreadPoolExecutor / asyncio.gather)')
            if name in CONNECT_CALLS or name.split('.')[-1] in ('MongoClient',) and name in CONNECT_CALLS:
                self.add(node, 'PY-CONNECT-IN-LOOP', 'high', f'{name}() creates a new connection/client on every iteration', 'create it once before the loop (or use a pool) and reuse it')
            if name in ('copy.deepcopy', 'deepcopy'):
                self.add(node, 'PY-DEEPCOPY-LOOP', 'medium', 'deepcopy inside a loop is very expensive', 'copy once outside the loop, or copy only what is mutated')
            if name == 'open' and node.args and not self._writes(node):
                a0 = node.args[0]
                inv = isinstance(a0, ast.Constant) or (isinstance(a0, ast.Name) and not any(a0.id in l.stored for l in real_loops))
                if inv:
                    self.add(node, 'PY-IO-IN-LOOP', 'medium', 'same file opened and read on every iteration', 'read it once before the loop')
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and lst.get(node.func.value.id) == 'list':
                if node.func.attr in ('index', 'count', 'remove'):
                    self.add(node, 'PY-LIST-SCAN-IN-LOOP', 'medium', f'list.{node.func.attr}() scans the list on every iteration (O(n^2) total)', 'keep a dict/set (value -> position/count) instead')
                if (node.func.attr == 'pop' and len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and node.args[0].value == 0) or \
                        (node.func.attr == 'insert' and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 0):
                    self.add(node, 'PY-LIST-FRONT-OP-LOOP', 'medium', f'list.{node.func.attr}(0, ...) shifts every element: O(n) per call', 'use collections.deque (popleft / appendleft)')
            if name in ('sorted', 'min', 'max', 'sum') and len(node.args) == 1 and isinstance(node.args[0], ast.Name):
                x = node.args[0].id
                inner = real_loops[-1]
                if x not in inner.stored and x not in inner.targets and self.sc.kinds.get(x) in ('list', 'hashed'):
                    self.add(node, 'PY-RECOMPUTE-IN-LOOP', 'medium', f'{name}({x}) recomputed on every iteration over the same collection', 'hoist it out of the loop; if the collection changes, use heapq / bisect / a running total')
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'sort' and isinstance(node.func.value, ast.Name) and self.sc.kinds.get(node.func.value.id) == 'list' \
                    and not (node.func.value.id in real_loops[-1].stored):
                self.add(node, 'PY-RECOMPUTE-IN-LOOP', 'medium', f'{node.func.value.id}.sort() inside a loop re-sorts every iteration', 'sort once after the loop, or keep it ordered with bisect.insort / heapq')
        self.generic_visit(node)

    def _writes(self, call):
        mode = None
        if len(call.args) > 1 and isinstance(call.args[1], ast.Constant):
            mode = call.args[1].value
        for k in call.keywords:
            if k.arg == 'mode' and isinstance(k.value, ast.Constant):
                mode = k.value.value
        return isinstance(mode, str) and any(c in mode for c in 'wax+')

def scan_py(path, text):
    raw = text.split('\n')
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], 'syntax error (cannot scan)'
    v = PyPerf(path, raw, tree)
    v.visit(tree)
    return v.f, ''

# ================================================================ scan: JS / TS (text based, offsets preserved)

def js_blank(text):
    """same-length copy: comments and string/regex/template contents replaced by spaces"""
    out, n, i, prev = list(text), len(text), 0, ''
    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != '\n':
                out[k] = ' '
    while i < n:
        c, c2 = text[i], text[i:i + 2]
        if c2 == '//':
            j = text.find('\n', i)
            j = n if j == -1 else j
            blank(i, j); i = j; continue
        if c2 == '/*':
            j = text.find('*/', i + 2)
            j = n if j == -1 else j + 2
            blank(i, j); i = j; continue
        if c in '"\'':
            j = i + 1
            while j < n and text[j] != c and text[j] != '\n':
                j += 2 if text[j] == '\\' else 1
            if j < n and text[j] == c:
                blank(i + 1, j); i, prev = j + 1, c; continue
            i += 1; continue
        if c == '`':
            j = i + 1
            while j < n:
                if text[j] == '\\':
                    j += 2; continue
                if text[j] == '`':
                    break
                if text[j:j + 2] == '${':
                    d, j = 1, j + 2
                    while j < n and d > 0:
                        d += 1 if text[j] == '{' else (-1 if text[j] == '}' else 0)
                        j += 1
                    continue
                j += 1
            blank(i + 1, j); i, prev = j + 1, '`'; continue
        if c == '/' and (prev == '' or prev in '(=:[!&|?{};,+-*%<>~^'):
            j, cls = i + 1, False
            while j < n and text[j] != '\n':
                ch = text[j]
                if ch == '\\':
                    j += 2; continue
                if ch == '[': cls = True
                elif ch == ']': cls = False
                elif ch == '/' and not cls: break
                j += 1
            if j < n and text[j] == '/' and j > i + 1:
                blank(i + 1, j); i, prev = j + 1, '/'; continue
        if not c.isspace():
            prev = c
        i += 1
    return ''.join(out)

def match_close(s, i, o, c):
    d, n = 0, len(s)
    for k in range(i, n):
        if s[k] == o:
            d += 1
        elif s[k] == c:
            d -= 1
            if d == 0:
                return k
    return -1

LOOP_HEAD = re.compile(r'\b(for|while)\s*(await\s*)?\(')
CB_HEAD = re.compile(r'\.\s*(forEach|map|filter|reduce|reduceRight|flatMap|some|every|find|findIndex|findLast)\s*\(')
IDENT = r'[A-Za-z_$][\w$]*'

class JsCtx:
    def __init__(self, code):
        self.code = code
        self.offs = [0]
        for l in code.split('\n'):
            self.offs.append(self.offs[-1] + len(l) + 1)
        self.loops = []       # (start, end, kind, names)
        self.asyncs = []      # (start, end)
        self.handlers = []
        self._build()

    def line(self, pos):
        return bisect.bisect_right(self.offs, pos)

    def _names_from_head(self, head):
        names = set()
        for m in re.finditer(r'(?:const|let|var)\s+(?:\{([^}]*)\}|\[([^\]]*)\]|(' + IDENT + r'))', head):
            if m.group(3):
                names.add(m.group(3))
            for g in (m.group(1), m.group(2)):
                if g:
                    for part in g.split(','):
                        nm = re.split(r'[:=]', part)[-1 if ':' in part else 0].strip().lstrip('.')
                        if re.fullmatch(IDENT, nm):
                            names.add(nm)
        return names

    def _cb_params(self, code, po):
        m = re.match(r'\s*(?:async\s+)?(?:function\s*' + IDENT + r'?\s*)?\(([^)]*)\)\s*(?:=>)?', code[po + 1:po + 300]) or \
            re.match(r'\s*(?:async\s+)?(' + IDENT + r')\s*=>', code[po + 1:po + 300])
        if not m:
            return []
        raw = m.group(1)
        return [p for p in (re.split(r'[=:]', x)[0].strip().lstrip('.') for x in raw.split(',')) if re.fullmatch(IDENT, p)]

    def _build(self):
        code = self.code
        for m in LOOP_HEAD.finditer(code):
            po = m.end() - 1
            pc = match_close(code, po, '(', ')')
            if pc < 0:
                continue
            k = pc + 1
            while k < len(code) and code[k].isspace():
                k += 1
            if k < len(code) and code[k] == '{':
                e = match_close(code, k, '{', '}')
            else:
                e = code.find(';', k)
            if e < 0:
                continue
            kind = 'for-await' if m.group(2) else m.group(1)
            self.loops.append((k, e, kind, self._names_from_head(code[po:pc + 1]), po, pc))
        for m in CB_HEAD.finditer(code):
            po = m.end() - 1
            pc = match_close(code, po, '(', ')')
            if pc > 0:
                self.loops.append((po, pc, 'cb:' + m.group(1), set(self._cb_params(code, po)[:2]), po, pc))
        for m in re.finditer(r'\basync\b', code):
            pos = m.end()
            while pos < len(code) and code[pos].isspace(): pos += 1
            if code.startswith('function', pos):
                pos += 8
                while pos < len(code) and (code[pos].isspace() or code[pos] == '*'): pos += 1
            im = re.match(IDENT, code[pos:pos + 80])
            if im:
                pos += im.end()
                while pos < len(code) and code[pos].isspace(): pos += 1
            if pos < len(code) and code[pos] == '(':
                pc = match_close(code, pos, '(', ')')
                if pc < 0:
                    continue
                pos = pc + 1
                while pos < len(code) and code[pos].isspace(): pos += 1
                if code.startswith(':', pos):
                    j = code.find('{', pos)
                    pos = j if 0 <= j < pos + 120 else pos
            if code.startswith('=>', pos):
                pos += 2
                while pos < len(code) and code[pos].isspace(): pos += 1
            if pos < len(code) and code[pos] == '{':
                e = match_close(code, pos, '{', '}')
                if e > 0:
                    self.asyncs.append((pos, e))
        for m in re.finditer(r'\(\s*(?:req|request)\s*(?::[^,)]*)?,\s*(?:res|response|reply)\b[^)]*\)\s*(?:=>\s*)?\{', code):
            s = m.end() - 1
            e = match_close(code, s, '{', '}')
            if e > 0:
                self.handlers.append((s, e))

    def enclosing_loops(self, pos, kinds=None):
        r = [l for l in self.loops if l[0] <= pos <= l[1] and (kinds is None or l[2] in kinds)]
        return sorted(r, key=lambda l: l[1] - l[0])

    def in_async(self, pos):
        return any(a <= pos <= b for a, b in self.asyncs)

    def in_handler(self, pos):
        return any(a <= pos <= b for a, b in self.handlers)

LOOPY = {'for', 'while', 'for-await', 'cb:forEach', 'cb:map', 'cb:filter', 'cb:reduce', 'cb:reduceRight', 'cb:flatMap', 'cb:some', 'cb:every', 'cb:find', 'cb:findIndex', 'cb:findLast'}
SEQ_LOOPS = {'for', 'while'}
JS_DB = re.compile(r'([\w$]+)\s*\.\s*(query|execute|findOne|findById|findUnique|findFirst|findMany|findAll|aggregate|countDocuments)\s*\(')
JS_DB_RECV = re.compile(r'(?i)^(?:\w*)(?:db|pool|client|conn|connection|knex|sequelize|prisma|supabase|pg|mysql2?|sql|database|repo|repository|manager|em|trx|tx|transaction|collection|model|session)$')
JS_DB_SPECIFIC = {'findOne', 'findById', 'findUnique', 'findFirst', 'findMany', 'findAll', 'countDocuments'}
JS_CONNECT = re.compile(r'\bnew\s+(?:Pool|Client|PrismaClient|MongoClient|Redis|IORedis|S3Client|DynamoDBClient|Sequelize)\s*\(|\b(?:createConnection|createPool|mongoose\s*\.\s*connect)\s*\(|\baxios\s*\.\s*create\s*\(')
JS_SYNC_IO = re.compile(r'\b(?:readFileSync|writeFileSync|appendFileSync|readdirSync|statSync|lstatSync|existsSync|execSync|spawnSync|copyFileSync|mkdirSync|execFileSync)\s*\(')

def scan_js(path, text):
    raw = text.split('\n')
    if any(len(l) > 2000 for l in raw):
        return [], 'minified/very long lines'
    code = js_blank(text)
    cx = JsCtx(code)
    F, seen = [], set()
    def add(pos, rule, sev, msg, fix):
        line = cx.line(pos)
        if (line, rule) in seen or suppressed(raw, line, rule):
            return
        seen.add((line, rule))
        F.append(Finding(path, line, line, rule, sev, msg, fix))
    arrayish = set()
    for m in re.finditer(r'(?:const|let|var)\s+(' + IDENT + r')\s*(?::[^=;]+)?=\s*(?:\[|Array\.from\b|new\s+Array\b|[\w.$]+\s*\.\s*(?:map|filter|split|slice|concat|flat|flatMap|sort)\s*\()', code):
        arrayish.add(m.group(1))
    # ---- queries / connections / json clone / new RegExp in loops
    for m in JS_DB.finditer(code):
        recv, meth = m.group(1), m.group(2)
        awaited = re.search(r'\bawait\s+[\w$.]*$', code[max(0, m.start() - 60):m.start()]) is not None
        if not (meth in JS_DB_SPECIFIC or JS_DB_RECV.match(recv) or (awaited and recv not in ('this', 'self'))):
            continue
        if cx.enclosing_loops(m.start(), LOOPY):
            add(m.start(), 'JS-QUERY-IN-LOOP', 'high', 'database query inside a loop/callback: one round trip per item (N+1)', 'fetch all rows in one query (JOIN / WHERE id IN (...) / include) before the loop')
    for m in JS_CONNECT.finditer(code):
        if cx.enclosing_loops(m.start(), LOOPY):
            add(m.start(), 'JS-CONNECT-IN-LOOP', 'high', 'new client/connection created on every iteration', 'create it once outside the loop (or use a pool) and reuse it')
    for m in re.finditer(r'JSON\s*\.\s*parse\s*\(\s*JSON\s*\.\s*stringify\s*\(', code):
        if cx.enclosing_loops(m.start(), LOOPY):
            add(m.start(), 'JS-JSON-CLONE-LOOP', 'medium', 'JSON round-trip used as a deep clone inside a loop', 'structuredClone once outside the loop, or clone only what is mutated')
    for m in re.finditer(r'\bnew\s+RegExp\s*\(', code):
        if cx.enclosing_loops(m.start(), LOOPY):
            add(m.start(), 'JS-REGEXP-NEW-LOOP', 'low', 'RegExp compiled on every iteration', 'build it once outside the loop')
    for m in re.finditer(r'\bdocument\s*\.\s*(?:querySelector(?:All)?|getElementById|getElementsBy\w+)\s*\(', code):
        if cx.enclosing_loops(m.start(), {'for', 'while', 'cb:forEach'}):
            add(m.start(), 'JS-DOM-QUERY-IN-LOOP', 'low', 'DOM lookup on every iteration', 'query once before the loop')
    # ---- sequential await in a for/while loop
    for (s, e, kind, names, po, pc) in cx.loops:
        if kind not in SEQ_LOOPS:
            continue
        body = code[s:e + 1]
        if re.search(r'\b(?:break|return|continue)\b', body):
            continue
        for am in re.finditer(r'\bawait\b', body):
            stmt_end = min([x for x in (body.find(';', am.start()), body.find('\n', am.start())) if x >= 0] or [len(body)])
            stmt = body[am.start():stmt_end]
            if re.search(r'\b(?:sleep|delay|setTimeout|wait|timeout)\b', stmt, re.I):
                continue
            if any(re.search(r'(?<![\w$.])' + re.escape(nm) + r'\b', stmt) for nm in names) and '(' in stmt:
                line_start = body.rfind('\n', 0, am.start()) + 1
                full = body[line_start:stmt_end]
                collected = re.search(r'(?:\bconst|\blet|\bvar)\s+[\w${}\[\], ]+=\s*await|\.push\(\s*await|\]\s*=\s*await|(?<![=!<>])\b\w+\s*=\s*await', full)
                io_work = re.search(r'\b(?:fetch|axios|got|request|download|upload|query|send|get|post|put|read|load|fetchThing)\w*\s*\(', stmt, re.I)
                sev = 'medium' if (collected or io_work) else 'low'
                add(s + am.start(), 'JS-AWAIT-IN-LOOP', sev, 'await inside a for/while loop runs the iterations one after another',
                    'if iterations are independent: await Promise.all(items.map(...)) (limit concurrency for large lists); keep the loop only if order or rate limits matter')
                break
    # ---- nested linear lookup (join) and includes/indexOf on arrays inside loops
    for m in re.finditer(r'\b(' + IDENT + r')\s*\.\s*(includes|indexOf|lastIndexOf)\s*\(', code):
        if m.group(1) in arrayish and cx.enclosing_loops(m.start(), LOOPY):
            add(m.start(), 'JS-INCLUDES-IN-LOOP', 'medium', f'`{m.group(1)}.{m.group(2)}` scans the whole array on every iteration (O(n^2) total)', f'turn `{m.group(1)}` into a Set/Map once, then use .has()/.get()')
    for m in re.finditer(r'\b(' + IDENT + r'(?:\s*\.\s*' + IDENT + r')*)\s*\.\s*(find|findIndex|filter|some)\s*\(', code):
        outer = [l for l in cx.enclosing_loops(m.start(), LOOPY)]
        if not outer:
            continue
        po = m.end() - 1
        pc = match_close(code, po, '(', ')')
        if pc < 0:
            continue
        inner = code[po:pc + 1]
        ov = set().union(*[l[3] for l in outer])
        recv_root = re.split(r'\s*\.\s*', m.group(1))[0]
        if recv_root in ov:
            continue
        cmp_ = re.findall(r'([\w$.\[\]]+)\s*(?:===|!==|==|!=)\s*([\w$.\[\]]+)', inner)
        hit = any(any(re.match(r'(?<![\w$])' + re.escape(v) + r'\b', side.strip('()')) or side.startswith(v + '.') or side == v for v in ov) for pair in cmp_ for side in pair)
        if hit:
            add(m.start(), 'JS-NESTED-LOOKUP', 'medium', f'`{m.group(1)}.{m.group(2)}(...)` inside a loop is a linear search per item: O(n*m)', 'build a Map keyed by the compared field once, then map.get(key)')
    # ---- quadratic accumulation
    for (s, e, kind, names, po, pc) in cx.loops:
        if kind in ('cb:reduce', 'cb:reduceRight'):
            params = cx._cb_params(code, po)
            if not params:
                continue
            acc = re.escape(params[0])
            pat = r'\[\s*\.\.\.\s*' + acc + r'\b|\{\s*\.\.\.\s*' + acc + r'\b|(?<![\w$.])' + acc + r'\s*\.\s*concat\s*\('
            mm = re.search(pat, code[s:e + 1])
            if mm:
                add(s + mm.start(), 'JS-SPREAD-IN-LOOP', 'high', 'accumulator copied on every iteration of reduce (quadratic)', 'mutate the accumulator (acc.push(x) / acc[key] = v) and return it')
        elif kind in ('for', 'while'):
            for mm in re.finditer(r'(' + IDENT + r')\s*=\s*(?:\[\s*\.\.\.\s*\1\b|\{\s*\.\.\.\s*\1\b|\1\s*\.\s*concat\s*\()', code[s:e + 1]):
                add(s + mm.start(), 'JS-SPREAD-IN-LOOP', 'high', f'`{mm.group(1)}` re-created from itself on every iteration (quadratic)', 'mutate it in place (push / assign) instead of copying')
    for m in re.finditer(r'\.\s*(shift|unshift)\s*\(', code):
        if cx.enclosing_loops(m.start(), {'while', 'for'}):
            add(m.start(), 'JS-SHIFT-LOOP', 'low', f'Array.{m.group(1)}() moves every element: O(n) per call (fine for small arrays)', 'use an index pointer, or a deque, when the array can be large')
    # ---- sync I/O where it blocks other work
    for m in JS_SYNC_IO.finditer(code):
        if cx.in_async(m.start()) or cx.in_handler(m.start()) or cx.enclosing_loops(m.start(), {'for', 'while'}):
            add(m.start(), 'JS-SYNC-IO', 'high', 'synchronous file/process call blocks the event loop while it runs', 'use the async API (fs.promises.*, execFile with a callback/promisify) or do it once at startup')
    # ---- React hooks that run/recompute on every render
    for m in re.finditer(r'\b(useEffect|useMemo|useCallback|useLayoutEffect)\s*\(', code):
        po = m.end() - 1
        pc = match_close(code, po, '(', ')')
        if pc < 0:
            continue
        depth, commas, k = 0, 0, po + 1
        while k < pc:
            ch = code[k]
            if ch in '([{': depth += 1
            elif ch in ')]}': depth -= 1
            elif ch == ',' and depth == 0 and code[k + 1:pc].strip():
                commas += 1
            k += 1
        if commas == 0:
            hook = m.group(1)
            msg = 'runs after every render' if 'Effect' in hook else 'recomputed on every render (useless without a dependency array)'
            add(m.start(), 'JS-REACT-NO-DEPS', 'medium', f'{hook}() without a dependency array {msg}', 'pass the dependency array (or [] to run once)')
    return F, ''

# ================================================================ scan driver

def scan_files(t, low=False, all_lines=False):
    findings, notes = [], []
    for p, added in sorted(t.files.items()):
        ext = os.path.splitext(p)[1]
        if ext not in CODE_EXT:
            continue
        text = read_text(p)
        rp = rel(t, p)
        if text is None:
            notes.append(f'{rp}: skipped (binary or over {MAX_BYTES // 1000} KB)')
            continue
        f, note = scan_py(rp, text) if ext == '.py' else scan_js(rp, text)
        if note:
            notes.append(f'{rp}: {note}')
        for x in f:
            if not low and x.sev == 'low':
                continue
            if added is None or all_lines or any(k in added for k in range(x.line, x.end + 1)):
                findings.append(x)
    return sorted(set(findings), key=lambda x: (SEV_ORDER[x.sev], x.path, x.line)), notes

def print_findings(findings, notes, t, limit=40):
    print(f'scan: {len(findings)} slow-pattern finding(s) in {len(t.files)} file(s) ({t.mode})')
    for f in findings[:limit]:
        print(f'  {f.sev.upper():6} {f.path}:{f.line}  {f.rule}  {f.msg}  -> {f.fix}')
    if len(findings) > limit:
        print(f'  ... +{len(findings) - limit} more')
    for n in notes[:5]:
        print(f'  note: {n}')

def cmd_scan(a):
    t = get_targets(a.paths, a.base)
    findings, notes = scan_files(t, a.low, a.all_lines)
    print_findings(findings, notes, t)
    return 0

# ================================================================ measurement engine

class Run:
    def __init__(self, t=0.0, rc=0, rss=0, out=b'', err=''):
        self.t, self.rc, self.rss, self.out, self.err = t, rc, rss, out, err

def run_once(cmd, cwd, env, timeout, shell=False):
    out, err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    t0 = time.perf_counter()
    try:
        p = subprocess.Popen(cmd, shell=shell, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                             start_new_session=hasattr(os, 'killpg'))
    except OSError as e:
        return Run(0.0, 'nostart', 0, b'', str(e))
    killed = []
    def kill():
        killed.append(1)
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                p.kill()
            except OSError:
                pass
    timer = threading.Timer(timeout, kill)
    timer.daemon = True
    timer.start()
    rss = 0
    try:
        if hasattr(os, 'wait4'):
            _, status, ru = os.wait4(p.pid, 0)
            t1 = time.perf_counter()
            rc = os.waitstatus_to_exitcode(status) if hasattr(os, 'waitstatus_to_exitcode') else (status >> 8)
            rss = ru.ru_maxrss / (1024.0 if sys.platform == 'darwin' else 1.0)
            p.returncode = rc
        else:
            p.wait()
            t1 = time.perf_counter()
            rc = p.returncode
    finally:
        timer.cancel()
    if killed:
        rc = 'timeout'
    out.seek(0)
    data = out.read(4_000_000)
    err.seek(0)
    tail = err.read()[-700:].decode('utf-8', 'replace').strip()
    out.close(); err.close()
    return Run(t1 - t0, rc, rss, data, tail)

def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

def noise_rel(xs):
    m = median(xs)
    return 1.4826 * median([abs(x - m) for x in xs]) / m if m else 0.0

def pctl(xs, q):
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]

@lru_cache(maxsize=None)
def _u_count(m, n, u):
    if u < 0:
        return 0
    if m == 0 or n == 0:
        return 1 if u == 0 else 0
    return _u_count(m - 1, n, u - n) + _u_count(m, n - 1, u)

def mw_p(a, b):
    """two-sided Mann-Whitney U p-value: exact for small tie-free samples, normal approximation otherwise"""
    n1, n2 = len(a), len(b)
    if n1 < 3 or n2 < 3:
        return None
    u1 = sum((x > y) + 0.5 * (x == y) for x in a for y in b)
    u2 = n1 * n2 - u1
    ties = len(set(a + b)) < n1 + n2
    if not ties and max(n1, n2) <= 15:
        umin = int(min(u1, u2))
        tot = math.comb(n1 + n2, n1)
        return min(1.0, 2 * sum(_u_count(n1, n2, u) for u in range(umin + 1)) / tot)
    vals = sorted(a + b)
    n = n1 + n2
    tie_sum, i = 0, 0
    while i < n:
        j = i
        while j + 1 < n and vals[j + 1] == vals[i]:
            j += 1
        t = j - i + 1
        tie_sum += t ** 3 - t
        i = j + 1
    sigma2 = n1 * n2 / 12.0 * ((n + 1) - tie_sum / (n * (n - 1)))
    if sigma2 <= 0:
        return 1.0
    z = max(0.0, (abs(u1 - n1 * n2 / 2.0) - 0.5) / math.sqrt(sigma2))
    return math.erfc(z / math.sqrt(2))

def fmt_t(s):
    if s >= 10:
        return f'{s:.1f} s'
    if s >= 1:
        return f'{s:.2f} s'
    if s >= 0.01:
        return f'{s * 1000:.0f} ms'
    if s >= 0.001:
        return f'{s * 1000:.1f} ms'
    return f'{s * 1e6:.0f} us'

def fmt_p(p):
    if p is None:
        return 'n/a'
    return 'p<0.001' if p < 0.001 else f'p={p:.3f}'

def fmt_mb(kb):
    return f'{kb / 1024:.0f} MB' if kb >= 1024 else f'{kb:.0f} KB'

def parse_cmd(cmd_str, rest):
    """-> (cmd, shell) ; --cmd 'string' or `-- argv...`"""
    if cmd_str:
        if re.search(r'[|&;<>$`]|\|\||&&', cmd_str):
            return cmd_str, True
        return shlex.split(cmd_str), False
    if rest:
        return list(rest), False
    return None, False

def side(cmd, shell, cwd, env=None):
    return {'cmd': cmd, 'shell': shell, 'cwd': cwd, 'env': env or dict(os.environ)}

def do_run(sd, timeout):
    return run_once(sd['cmd'], sd['cwd'], sd['env'], timeout, sd['shell'])

def sample_pair(A, B, runs, timeout, budget):
    """interleaved A/B runs after one warm-up each; returns dict"""
    wa, wb = do_run(A, timeout), do_run(B, timeout)
    res = {'wa': wa, 'wb': wb, 'ta': [], 'tb': [], 'ra': [], 'rb': [], 'oa': {wa.out}, 'ob': {wb.out}, 'fail': None}
    for w, name in ((wa, 'before'), (wb, 'after')):
        if w.rc != 0:
            res['fail'] = (name, w.rc, w.err)
            return res
    probe = max(wa.t, wb.t, 1e-4)
    if runs is None:
        runs = int(max(5, min(15, budget / (2 * probe))))
        if probe * 2 * 5 > budget:
            runs = max(3, int(budget / (2 * probe)))
    res['runs'] = runs
    deadline = time.time() + budget * 2
    for i in range(runs):
        order = ((A, 'a'), (B, 'b')) if i % 2 == 0 else ((B, 'b'), (A, 'a'))
        for sd, tag in order:
            r = do_run(sd, timeout)
            if r.rc != 0:
                res['fail'] = ('before' if tag == 'a' else 'after', r.rc, r.err)
                return res
            res['t' + tag].append(r.t)
            res['r' + tag].append(r.rss)
            res['o' + tag].add(r.out)
        if time.time() > deadline and i + 1 >= 3:
            break
    return res

def judge(ta, tb):
    ma, mb = median(ta), median(tb)
    noise = max(noise_rel(ta), noise_rel(tb))
    p = mw_p(ta, tb)
    min_effect = max(0.03, min(0.30, 1.5 * noise))
    delta = (mb - ma) / ma if ma else 0.0
    if p is None:
        status = 'INCONCLUSIVE'
    elif p < 0.05 and abs(delta) >= min_effect:
        status = 'FASTER' if delta < 0 else 'SLOWER'
    else:
        status = 'NO MEASURABLE DIFFERENCE'
    return {'status': status, 'ma': ma, 'mb': mb, 'ratio': (ma / mb) if mb else float('inf'), 'delta': delta, 'p': p, 'noise': noise, 'min_effect': min_effect}

def cmd_bench(a, rest):
    cmd, shell = parse_cmd(a.cmd, rest)
    if not cmd:
        print('bench: give a command after -- (or --cmd "...")'); return 2
    sd = side(cmd, shell, os.getcwd())
    w = do_run(sd, a.timeout)
    if w.rc != 0:
        print(f'bench: command failed (exit {w.rc}); a failing command cannot be timed' + (f'\n  | {w.err[-300:]}' if w.err else '')); return 1
    runs = a.runs or int(max(5, min(15, a.budget / max(w.t, 1e-4))))
    ts, rs = [], []
    for _ in range(runs):
        r = do_run(sd, a.timeout)
        if r.rc != 0:
            print(f'bench: a run failed (exit {r.rc})'); return 1
        ts.append(r.t); rs.append(r.rss)
    print(f'bench: {len(ts)} runs | median {fmt_t(median(ts))}  min {fmt_t(min(ts))}  p95 {fmt_t(pctl(ts, 0.95))}  noise ±{noise_rel(ts) * 100:.1f}% | peak memory {fmt_mb(max(rs))}')
    if median(ts) < 0.03:
        print('  note: the command takes <30 ms, so process start-up dominates; use a bigger input for a trustworthy number')
    return 0

# ---- base version of the code (committed files only), for before/after

def export_base(root, base, relcwd, links):
    tmp = tempfile.mkdtemp(prefix='pf-base-')
    p = subprocess.Popen(['git', 'archive', '--format=tar', base], stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=root)
    try:
        with tarfile.open(fileobj=p.stdout, mode='r|') as tf:
            try:
                tf.extractall(tmp, filter='fully_trusted')
            except TypeError:
                tf.extractall(tmp)
    finally:
        p.stdout.close()
        p.wait()
    if p.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    cands = {'node_modules', '.venv', 'venv', 'env'} | set(links)
    for base_rel in {'.', relcwd}:
        for name in cands:
            src = os.path.join(root, base_rel, name)
            dst = os.path.join(tmp, base_rel, name)
            if os.path.exists(src) and not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    os.symlink(os.path.abspath(src), dst)
                except OSError:
                    pass
    return tmp

def dirty_vs(root, base):
    if git('diff', '--quiet', base, cwd=root) is None:   # exit 1 => differences
        return True
    others = [x for x in (git('ls-files', '--others', '--exclude-standard', cwd=root) or '').split('\n') if x.strip()]
    return any(os.path.splitext(x)[1] in CODE_EXT for x in others)

def do_compare(a, cmd, shell):
    """returns result dict"""
    cwd = os.getcwd()
    res = {'kind': 'compare'}
    cleanup = None
    if a.a and a.b:
        ca, sa = parse_cmd(a.a, [])
        cb, sb = parse_cmd(a.b, [])
        A, B = side(ca, sa, cwd), side(cb, sb, cwd)
        la, lb = 'A', 'B'
        res['desc'] = f'A: {a.a} vs B: {a.b}'
    else:
        root = repo_root(cwd)
        if not root:
            return {'kind': 'error', 'msg': 'not a git repository: use --a "old command" --b "new command" to compare two commands'}
        if not (git('rev-parse', '--verify', '--quiet', a.base + '^{commit}', cwd=root) is not None):
            return {'kind': 'error', 'msg': f'base ref {a.base!r} not found'}
        if not a.force and not dirty_vs(root, a.base):
            return {'kind': 'nochange', 'msg': f'working tree has no code changes vs {a.base}: nothing to compare (use --force to measure the noise floor)'}
        relcwd = os.path.relpath(cwd, root)
        base_dir = export_base(root, a.base, relcwd, a.link or [])
        if not base_dir:
            return {'kind': 'error', 'msg': 'could not export the base version with git archive'}
        cleanup = base_dir
        A = side(cmd, shell, os.path.normpath(os.path.join(base_dir, relcwd)))
        B = side(cmd, shell, cwd)
        la, lb = f'before ({a.base})', 'after (working tree)'
        res['desc'] = ' '.join(cmd) if isinstance(cmd, list) else cmd
    try:
        s = sample_pair(A, B, a.runs, a.timeout, a.budget)
        res['s'], res['la'], res['lb'] = s, la, lb
        if s['fail']:
            res['kind'] = 'failed'
            return res
        j = judge(s['ta'], s['tb'])
        res['j'] = j
        oa, ob = s['oa'], s['ob']
        if a.no_output_check:
            res['out'] = 'not checked'
        elif len(oa) > 1 or len(ob) > 1:
            res['out'] = 'nondeterministic'
        elif oa == ob:
            res['out'] = 'identical'
        else:
            res['out'] = 'DIFFERENT'
        res['hash'] = hashlib.sha1(next(iter(oa))).hexdigest()[:8] if len(oa) == 1 else ''
        res['verify'] = None
        if a.verify:
            vc, vs = parse_cmd(a.verify, [])
            vr = run_once(vc, cwd, dict(os.environ), a.timeout, vs)
            res['verify'] = 'OK' if vr.rc == 0 else f'FAIL (exit {vr.rc})' + (f': {vr.err[-200:]}' if vr.err else '')
        return res
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)

def summarize_compare(res, expect):
    """-> (verdict, summary_text)"""
    j, s = res['j'], res['s']
    n = len(s['ta'])
    st = j['status']
    if st == 'FASTER':
        core = f"{fmt_t(j['ma'])} -> {fmt_t(j['mb'])} ({j['ratio']:.1f}x faster, {fmt_p(j['p'])}, {n} runs each)"
    elif st == 'SLOWER':
        core = f"{fmt_t(j['ma'])} -> {fmt_t(j['mb'])} ({1 / j['ratio']:.1f}x SLOWER, {fmt_p(j['p'])}, {n} runs each)"
    elif st == 'INCONCLUSIVE':
        core = f"{fmt_t(j['ma'])} vs {fmt_t(j['mb'])} (too few runs to tell, n={n})"
    else:
        core = f"{fmt_t(j['ma'])} vs {fmt_t(j['mb'])} (no measurable difference, noise ±{j['noise'] * 100:.0f}%, {n} runs each)"
    extra = []
    if res['out'] == 'identical':
        extra.append('output identical')
    elif res['out'] == 'DIFFERENT':
        extra.append('OUTPUT DIFFERS')
    elif res['out'] == 'nondeterministic':
        extra.append('output nondeterministic (not compared)')
    if res.get('verify'):
        extra.append('verify ' + res['verify'].split(':')[0])
    verdict = 'PASS'
    if res['out'] == 'DIFFERENT' or (res.get('verify') and not res['verify'].startswith('OK')) or st == 'SLOWER':
        verdict = 'FAIL'
    elif expect == 'faster' and st != 'FASTER':
        verdict = 'NOT PROVEN'
    return verdict, ' | '.join([core] + extra)

def print_compare(res, expect):
    if res['kind'] in ('error', 'nochange'):
        print(f"compare: {res['msg']}")
        return ('FAIL' if res['kind'] == 'error' else 'NO CHANGES'), ''
    s = res['s']
    if res['kind'] == 'failed':
        who, rc, err = s['fail']
        print(f'compare: the {who} command failed (exit {rc}); a failing command cannot be timed' + (f'\n  | {err[-400:]}' if err else ''))
        return 'FAIL', ''
    j = res['j']
    n = len(s['ta'])
    print(f"compare: {res['desc']} | {n} runs each, interleaved")
    for lab, ts, rs in ((res['la'], s['ta'], s['ra']), (res['lb'], s['tb'], s['rb'])):
        print(f'  {lab:22} median {fmt_t(median(ts)):>8}  min {fmt_t(min(ts)):>8}  p95 {fmt_t(pctl(ts, 0.95)):>8}  noise ±{noise_rel(ts) * 100:.1f}%  peak mem {fmt_mb(max(rs))}')
    st = j['status']
    if st == 'FASTER':
        line = f"FASTER {j['ratio']:.2f}x ({j['delta'] * 100:+.0f}% time)"
    elif st == 'SLOWER':
        line = f"SLOWER {1 / j['ratio']:.2f}x ({j['delta'] * 100:+.0f}% time)"
    else:
        line = f"{st} (median differs {j['delta'] * 100:+.1f}%, needs >={j['min_effect'] * 100:.0f}% and p<0.05 to count)"
    print(f"  result   {line}  {fmt_p(j['p'])}")
    fast_side = min(median(s['ta']), median(s['tb']))
    if fast_side < 0.03:
        print(f'  note     the faster side runs in {fmt_t(fast_side)}, mostly process start-up: the real speedup is larger than shown. Use a bigger input to measure it')
    if n < 5:
        print(f'  note     only {n} runs fit the time budget: low statistical power')
    print(f"  output   {res['out']}" + (f" (sha1 {res['hash']})" if res['out'] == 'identical' and res['hash'] else ''))
    if res['out'] == 'nondeterministic':
        print('           stdout differs between runs of the same code (timestamps/ids?): equality was NOT verified')
    if res['out'] == 'DIFFERENT':
        print('           the two versions print different output: an optimisation must not change results')
    if res.get('verify'):
        print(f"  verify   {res['verify']}")
    return summarize_compare(res, expect)[0], summarize_compare(res, expect)[1]

def cmd_compare(a, rest):
    cmd, shell = parse_cmd(a.cmd, rest)
    if not (a.a and a.b) and not cmd:
        print('compare: give the command after -- (or --cmd "..."), or --a "old" --b "new"'); return 2
    res = do_compare(a, cmd, shell)
    verdict, summary = print_compare(res, a.expect)
    if summary:
        print(f'VERDICT: {verdict}')
        print(f'SUMMARY: perf-verifier: {summary}')
    return 1 if verdict in ('FAIL', 'NOT PROVEN') else 0

# ---- scaling behaviour

def parse_sizes(s):
    out = []
    for x in s.split(','):
        x = x.strip().lower()
        m = re.fullmatch(r'(\d+(?:\.\d+)?)([km]?)', x)
        if not m:
            raise SystemExit(f'scale: bad size {x!r}')
        out.append(int(float(m.group(1)) * {'': 1, 'k': 1000, 'm': 1000000}[m.group(2)]))
    return out

def fit_loglog(xs, ys):
    lx, ly = [math.log(x) for x in xs], [math.log(y) for y in ys]
    n = len(xs)
    mx, my = sum(lx) / n, sum(ly) / n
    sxx = sum((a - mx) ** 2 for a in lx)
    if sxx == 0:
        return None
    k = sum((a - mx) * (b - my) for a, b in zip(lx, ly)) / sxx
    ss_tot = sum((b - my) ** 2 for b in ly)
    ss_res = sum((b - (my + k * (a - mx))) ** 2 for a, b in zip(lx, ly))
    return k, (1 - ss_res / ss_tot) if ss_tot else 1.0

def classify(k):
    if k < 0.3:
        return 'about constant: the input size barely matters'
    if k < 1.25:
        return 'about linear, O(n)'
    if k < 1.6:
        return 'n log n or mildly superlinear'
    if k < 2.4:
        return 'about QUADRATIC, O(n^2)'
    if k < 3.4:
        return 'about CUBIC, O(n^3)'
    return 'worse than cubic (exponential?)'

def cmd_scale(a, rest):
    cmd, shell = parse_cmd(a.cmd, rest)
    if not cmd:
        print('scale: give the command after -- with {N} where the size goes (also exported as $PF_N)'); return 2
    sizes = parse_sizes(a.sizes)
    if len(sizes) < 3:
        print('scale: need at least 3 sizes (e.g. --sizes 2000,4000,8000,16000)'); return 2
    def build(n):
        env = dict(os.environ, PF_N=str(n))
        if shell:
            return cmd.replace('{N}', str(n)), env
        return [c.replace('{N}', str(n)) for c in cmd], env
    if not (('{N}' in cmd) if shell else any('{N}' in c for c in cmd)):
        print('scale: the command has no {N} placeholder; nothing varies with the size (PF_N is still exported)')
    cwd = os.getcwd()
    def t_at(n):
        c, env = build(n)
        rs = []
        for i in range(a.runs + 1):
            r = run_once(c, cwd, env, a.timeout, shell)
            if r.rc != 0:
                return None, r
            if i > 0 or a.runs == 0:
                rs.append(r)
        return rs, None
    base_rs, err = t_at(a.baseline_n)
    t0, base_rss = 0.0, 0
    if err is None:
        t0, base_rss = median([r.t for r in base_rs]), median([r.rss for r in base_rs])
        base_note = f'start-up baseline {fmt_t(t0)} (N={a.baseline_n})'
    else:
        base_note = f'baseline run with N={a.baseline_n} failed (exit {err.rc}); not subtracting start-up time'
    rows = []
    for n in sizes:
        rs, err = t_at(n)
        if err is not None:
            print(f'scale: N={n} failed (exit {err.rc}) ' + (f'| {err.err[-200:]}' if err.err else '')); return 1
        rows.append((n, median([r.t for r in rs]), median([r.rss for r in rs])))
    print(f'scale: {a.runs} runs per size | {base_note}')
    print(f"  {'N':>9}  {'time':>9}  {'net':>9}  {'x vs prev':>10}  {'peak mem':>9}")
    prev = None
    for n, t, rss in rows:
        net = max(t - t0, 0.0)
        step = f'{net / prev[1]:.1f}x (N x{n / prev[0]:.1f})' if prev and prev[1] > 0 and net > 0 else ''
        print(f'  {n:>9}  {fmt_t(t):>9}  {fmt_t(net):>9}  {step:>10}  {fmt_mb(rss):>9}')
        prev = (n, net)
    pts = [(n, t - t0) for n, t, _ in rows if t - t0 >= 0.005]
    if len(pts) < 3:
        print('  result   too small to fit: fewer than 3 sizes take >5 ms above start-up. Use bigger sizes (the largest should take a second or so).')
        print('SUMMARY: perf-verifier: scaling NOT determined (sizes too small)')
        return 0
    k, r2 = fit_loglog([p[0] for p in pts], [p[1] for p in pts])
    last3 = pts[-3:]
    k3, _ = fit_loglog([p[0] for p in last3], [p[1] for p in last3])
    print(f'  fitted   time ~ N^{k:.2f} (R²={r2:.2f}); last three sizes: N^{k3:.2f}  ->  {classify(k3)}')
    mem = [(n, rss - base_rss) for n, _, rss in rows if rss - base_rss > 2048]
    if len(mem) >= 3:
        mk, _ = fit_loglog([p[0] for p in mem], [p[1] for p in mem])
        print(f'  memory   peak grows ~ N^{mk:.2f}')
    if r2 < 0.9:
        print('  note     poor fit (R²<0.9): timings are noisy or the growth is irregular; use more runs / larger sizes')
    print(f"SUMMARY: perf-verifier: time grows ~N^{k3:.2f} ({classify(k3)}) over N={rows[0][0]}..{rows[-1][0]}")
    return 0

# ---- profiler

def short_path(p):
    p = p.replace('file://', '')
    try:
        r = os.path.relpath(p, os.getcwd())
        return r if not r.startswith('..') else p
    except ValueError:
        return p

def is_project_path(p):
    p = p.replace('file://', '')
    if not p or p.startswith(('~', '<', 'node:', '(')):
        return False
    ap = os.path.abspath(p)
    return ap.startswith(os.getcwd() + os.sep) and not any(x in ap for x in ('site-packages', 'node_modules', '/lib/python'))

def print_profile(rows, total, top, unit):
    print(f"  {'self%':>6} {'self':>9} {'total':>9} {'calls':>8}  function  (* = your code)")
    shown = rows[:top]
    for r in shown:
        star = '*' if r['proj'] else ' '
        print(f"  {r['self'] / total * 100:5.1f}% {fmt_t(r['self']):>9} {fmt_t(r['cum']) if r['cum'] is not None else '':>9} {str(r['calls'] if r['calls'] is not None else '-'):>8}  {star}{r['name']}  {r['where']}")
    proj = [r for r in rows if r['proj']]
    if proj:
        r = proj[0]
        share = r['self'] / total
        ceil = 1 / (1 - share) if share < 0.999 else float('inf')
        print(f"  ceiling  making `{r['name']}` free would speed the whole run up by at most {ceil:.1f}x; anything under ~5% self time cannot give more than ~1.05x")
    return proj[0]['name'] if proj else None

def cmd_profile(a, rest):
    cmd, shell = parse_cmd(a.cmd, rest)
    if not cmd or shell:
        print('profile: give an argv command after --, e.g. -- python3 script.py args   or   -- node app.js'); return 2
    exe = os.path.basename(cmd[0])
    cwd = os.getcwd()
    tmpd = tempfile.mkdtemp(prefix='pf-prof-')
    try:
        if re.fullmatch(r'python[\d.]*', exe):
            outf = os.path.join(tmpd, 'p.prof')
            args = list(cmd[1:])
            if args[:1] == ['-c'] and len(args) >= 2:      # cProfile cannot run -c code: profile it as a temporary script
                scr = os.path.join(tmpd, 'snippet.py')
                with open(scr, 'w') as fh:
                    fh.write(args[1])
                args = [scr] + args[2:]
            new = [cmd[0], '-m', 'cProfile', '-o', outf] + args
            r = run_once(new, cwd, dict(os.environ), a.timeout)
            if r.rc != 0 or not os.path.exists(outf):
                print(f'profile: the command failed (exit {r.rc})' + (f'\n  | {r.err[-500:]}' if r.err else '')); return 1
            import pstats
            st = pstats.Stats(outf)
            total = st.total_tt or 1e-9
            rows = []
            for (fn, ln, name), (cc, nc, tt, ct, _c) in st.stats.items():
                if fn.endswith('cProfile.py') or name == '<built-in method builtins.exec>' and tt < 1e-4:
                    continue
                where = '(built-in)' if fn == '~' else f'{short_path(fn)}:{ln}'
                rows.append({'name': name if fn != '~' else name.strip('{}'), 'where': where, 'self': tt, 'cum': ct, 'calls': nc, 'proj': is_project_path(fn)})
            rows.sort(key=lambda x: -x['self'])
            print(f'profile: {" ".join(cmd)} | {fmt_t(total)} profiled (cProfile inflates call-heavy code; trust the shares, not the seconds)')
            top = print_profile(rows, total, a.top, 's')
        elif exe in ('node', 'nodejs'):
            new = [cmd[0], '--cpu-prof', '--cpu-prof-dir=' + tmpd] + cmd[1:]
            r = run_once(new, cwd, dict(os.environ), a.timeout)
            files = [f for f in os.listdir(tmpd) if f.endswith('.cpuprofile')]
            if r.rc != 0 or not files:
                print(f'profile: the command failed (exit {r.rc})' + (f'\n  | {r.err[-500:]}' if r.err else '')); return 1
            prof = json.load(open(os.path.join(tmpd, files[0])))
            nodes = {n['id']: n for n in prof['nodes']}
            selft = {}
            samples, deltas = prof.get('samples', []), prof.get('timeDeltas', [])
            for i, sid in enumerate(samples):
                dt = deltas[i + 1] if i + 1 < len(deltas) else 0
                selft[sid] = selft.get(sid, 0) + dt
            agg = {}
            for sid, us in selft.items():
                cf = nodes[sid]['callFrame']
                nm = cf.get('functionName') or '(anonymous)'
                if nm == '(idle)':
                    continue
                key = (nm, cf.get('url', ''), cf.get('lineNumber', 0) + 1)
                agg[key] = agg.get(key, 0) + us / 1e6
            total = sum(agg.values()) or 1e-9
            rows = [{'name': k[0], 'where': (f'{short_path(k[1])}:{k[2]}' if k[1] else '(native)'), 'self': v, 'cum': None, 'calls': None,
                     'proj': is_project_path(k[1])} for k, v in agg.items()]
            rows.sort(key=lambda x: -x['self'])
            print(f'profile: {" ".join(cmd)} | {fmt_t(total)} sampled CPU time (self time per function)')
            top = print_profile(rows, total, a.top, 's')
        else:
            print(f'profile: only python and node commands are supported here (got {exe!r}); for other stacks use their own profiler (perf, py-spy, pprof, async-profiler)'); return 2
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    if top:
        print(f'SUMMARY: perf-verifier: profiled; top own-code hotspot: {top}')
    else:
        print('SUMMARY: perf-verifier: profiled; the time is not in your own code (library or runtime dominates)')
    return 0

# ================================================================ check: everything, one receipt

def cmd_check(a, rest):
    t0 = time.time()
    t = get_targets(a.paths, a.base)
    findings, notes = scan_files(t, low=False, all_lines=False)
    fh, fm = [f for f in findings if f.sev == 'high'], [f for f in findings if f.sev == 'medium']
    cmd, shell = parse_cmd(a.cmd, rest)
    res, verdict_m, summary_m = None, None, ''
    if cmd or (a.a and a.b):
        res = do_compare(a, cmd, shell)
        verdict_m, summary_m = print_compare(res, a.expect)
        print()
    verify_txt = None
    if a.verify and not (res and res.get('verify')):
        vc, vs = parse_cmd(a.verify, [])
        vr = run_once(vc, os.getcwd(), dict(os.environ), a.timeout, vs)
        verify_txt = 'OK' if vr.rc == 0 else f'FAIL (exit {vr.rc})' + (f': {vr.err[-200:]}' if vr.err else '')
    elif res and res.get('verify'):
        verify_txt = res['verify']
    if findings:
        print('details:')
        for f in findings[:25]:
            print(f'  {f.sev.upper():6} {f.path}:{f.line}  {f.rule}  {f.msg}  -> {f.fix}')
        print()
    print(f'perf-verifier receipt | {len(t.files)} file(s) | {t.mode} | {time.strftime("%H:%M:%S")} | {time.time() - t0:.0f}s')
    rows = []
    if not t.files:
        rows.append(('scan', 'n/a', 'no code files in scope'))
    elif fh or fm:
        rows.append(('scan', 'REVIEW', f'{len(fh)} high, {len(fm)} medium slow pattern(s) in the scanned code'))
    else:
        rows.append(('scan', 'OK', f'no slow patterns in {len(t.files)} file(s)'))
    verdict = 'PASS'
    if res is None:
        rows.append(('measure', 'NOT RUN', 'no --cmd given: nothing was measured, so no speed claim is supported'))
    elif res['kind'] == 'nochange':
        rows.append(('measure', 'n/a', 'no code changes vs base'))
    elif res['kind'] in ('error', 'failed'):
        rows.append(('measure', 'FAIL', res.get('msg') or 'the command failed'))
    else:
        j = res['j']
        rows.append(('measure', {'PASS': 'OK', 'FAIL': 'FAIL', 'NOT PROVEN': 'NOT PROVEN'}[verdict_m], summary_m))
    if verify_txt is not None:
        rows.append(('verify', 'OK' if verify_txt.startswith('OK') else 'FAIL', verify_txt))
    for k, st, det in rows:
        print(f'  {k:8} {st:11} {det}')
    if any(r[1] == 'FAIL' for r in rows):
        verdict = 'FAIL'
    elif any(r[1] == 'NOT PROVEN' for r in rows):
        verdict = 'NOT PROVEN'
    elif any(r[1] == 'REVIEW' for r in rows):
        verdict = 'REVIEW'
    elif res is None or res['kind'] == 'nochange':
        verdict = 'UNMEASURED'
    notes_txt = {'FAIL': 'FAIL - fix it before reporting done', 'NOT PROVEN': 'NOT PROVEN - no measurable speedup: do not claim one',
                 'REVIEW': 'REVIEW - explain or fix each flagged pattern', 'UNMEASURED': 'UNMEASURED - do not claim it is faster',
                 'PASS': 'PASS'}[verdict]
    print(f'VERDICT: {notes_txt}')
    scan_txt = f'scan: {len(fh) + len(fm)} slow pattern(s) flagged' if (fh or fm) else 'scan: clean'
    head = summary_m if summary_m else 'NOT MEASURED'
    print(f'SUMMARY: perf-verifier: {head} | {scan_txt}')
    return 1 if verdict in ('FAIL', 'NOT PROVEN') else 0

# ================================================================ main

def add_measure_args(p):
    p.add_argument('--cmd')
    p.add_argument('--base', default='HEAD')
    p.add_argument('--a'); p.add_argument('--b')
    p.add_argument('--runs', type=int)
    p.add_argument('--timeout', type=float, default=120)
    p.add_argument('--budget', type=float, default=60, help='seconds of total measuring time')
    p.add_argument('--verify')
    p.add_argument('--expect', choices=['faster', 'same'], default='same')
    p.add_argument('--force', action='store_true')
    p.add_argument('--link', action='append')
    p.add_argument('--no-output-check', action='store_true')

def main():
    argv = sys.argv[1:]
    rest = []
    if '--' in argv:
        i = argv.index('--')
        argv, rest = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog='pf.py')
    sub = ap.add_subparsers(dest='name', required=True)
    s = sub.add_parser('scan'); s.add_argument('paths', nargs='*'); s.add_argument('--base', default='HEAD')
    s.add_argument('--all-lines', action='store_true'); s.add_argument('--low', action='store_true')
    b = sub.add_parser('bench'); b.add_argument('--cmd'); b.add_argument('--runs', type=int); b.add_argument('--timeout', type=float, default=120)
    b.add_argument('--budget', type=float, default=30)
    c = sub.add_parser('compare'); add_measure_args(c)
    sc = sub.add_parser('scale'); sc.add_argument('--cmd'); sc.add_argument('--sizes', required=True); sc.add_argument('--runs', type=int, default=3)
    sc.add_argument('--baseline-n', type=int, default=1); sc.add_argument('--timeout', type=float, default=120)
    pr = sub.add_parser('profile'); pr.add_argument('--cmd'); pr.add_argument('--top', type=int, default=8); pr.add_argument('--timeout', type=float, default=300)
    ck = sub.add_parser('check'); ck.add_argument('paths', nargs='*'); add_measure_args(ck)
    a = ap.parse_args(argv)
    fn = {'scan': lambda: cmd_scan(a), 'bench': lambda: cmd_bench(a, rest), 'compare': lambda: cmd_compare(a, rest),
          'scale': lambda: cmd_scale(a, rest), 'profile': lambda: cmd_profile(a, rest), 'check': lambda: cmd_check(a, rest)}[a.name]
    sys.exit(fn())

if __name__ == '__main__':
    if hasattr(signal, 'SIGPIPE'):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    main()
