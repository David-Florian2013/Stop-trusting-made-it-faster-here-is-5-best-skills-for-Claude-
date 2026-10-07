#!/usr/bin/env python3
"""code-verifier: turns "I think this works" into evidence.

  vf.py check   [PATH ...] [--base REF] [--timeout S] [--strict]   everything, ends with a receipt
  vf.py scan    [PATH ...] [--base REF] [--all-lines] [--low]      risky patterns
  vf.py imports [PATH ...] [--base REF] [--shallow] [--online]     imports that do not resolve
  vf.py tamper  [--base REF]                                       tests weakened in the diff
  vf.py run     [--only tests|lint|types] [--timeout S]            the project's own checks
"""
import argparse, ast, bisect, glob, json, os, re, shutil, signal, subprocess, sys, time
from collections import namedtuple

Finding = namedtuple('Finding', 'path line end rule sev msg fix')
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', '__pycache__', '.tox', '.mypy_cache', '.next', '.nuxt',
             'coverage', 'vendor', 'target', '.pytest_cache', 'site-packages', '.venv', 'venv', '.ruff_cache'}
CODE_EXT = {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'}
PY_EXT, JS_EXT = {'.py'}, {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'}
MAX_BYTES = 1_500_000

def norm(p):
    return p.replace('\\', '/')

def is_test_path(p):
    return bool(re.search(r'(^|/)(tests?|__tests__|specs?|e2e)(/|$)|(^|/)test_[^/]*\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)conftest\.py$', norm(p)))

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

# ---------------------------------------------------------------- targets

class Targets:
    """files to inspect: path -> set of added/modified line numbers, or None meaning every line is new"""
    def __init__(self):
        self.root = os.getcwd()
        self.files = {}
        self.deleted = []
        self.mode = ''
        self.base = None

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
        t.root = root or os.getcwd()
        t.mode = 'explicit paths'
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
        t.mode = f'git diff vs {base}'
        for line in (git('diff', '--name-status', '--no-renames', base, cwd=root) or '').split('\n'):
            if not line.strip():
                continue
            st, _, p = line.partition('\t')
            ap = os.path.join(root, p)
            if st.startswith('D'):
                t.deleted.append(norm(p))
            elif os.path.exists(ap):
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

# ---------------------------------------------------------------- JS text helpers

def js_strip(text, keep_strings=False):
    """Return lines with comments removed; string/template contents blanked unless keep_strings."""
    out, buf = [], []
    in_block = in_tpl = False
    for line in text.split('\n'):
        buf = []
        i, n = 0, len(line)
        while i < n:
            c = line[i]
            if in_block:
                if line.startswith('*/', i):
                    in_block = False; i += 2
                else:
                    i += 1
                continue
            if in_tpl:
                if c == '\\':
                    buf.append(line[i:i + 2] if keep_strings else ''); i += 2; continue
                if c == '`':
                    in_tpl = False; buf.append('`'); i += 1; continue
                buf.append(c if keep_strings else ''); i += 1; continue
            if line.startswith('//', i):
                break
            if line.startswith('/*', i):
                in_block = True; i += 2; continue
            if c == '`':
                in_tpl = True; buf.append('`'); i += 1; continue
            if c in '"\'':
                k = i + 1
                while k < n and line[k] != c:
                    k += 2 if line[k] == '\\' else 1
                if k < n:
                    buf.append(c + (line[i + 1:k] if keep_strings else '') + c); i = k + 1; continue
                buf.append(c); i += 1; continue
            if c == '/':
                prev = ''.join(buf).rstrip()[-1:]
                if prev == '' or prev in '(=:[!&|?{};,+-*%<>~^':
                    k, cls = i + 1, False
                    while k < n:
                        if line[k] == '\\': k += 2; continue
                        if line[k] == '[': cls = True
                        elif line[k] == ']': cls = False
                        elif line[k] == '/' and not cls: break
                        k += 1
                    if k < n and k > i + 1:
                        buf.append('/' + (line[i + 1:k] if keep_strings else '') + '/'); i = k + 1; continue
            buf.append(c); i += 1
        out.append(''.join(buf))
    return out

def call_text(raw, i, col=0, limit=12):
    """text of the call starting at raw[i][col:], up to its balancing parenthesis"""
    depth, seen, parts = 0, False, []
    for k in range(i, min(len(raw), i + limit)):
        s = raw[k][col:] if k == i else raw[k]
        for ch in s:
            if ch == '(':
                depth += 1; seen = True
            elif ch == ')':
                depth -= 1
        parts.append(s)
        if seen and depth <= 0:
            break
    return '\n'.join(parts)

SUPPRESS = re.compile(r'vf:\s*ignore(?:\s+([A-Z0-9-]+))?')

def suppressed(raw, line, rule):
    for k in (line - 1, line - 2):
        if 0 <= k < len(raw):
            m = SUPPRESS.search(raw[k])
            if m and (not m.group(1) or m.group(1) == rule) and (k == line - 1 or raw[k].lstrip().startswith(('#', '//', '/*'))):
                return True
    return False

SECRET_RE = re.compile(r'''(?ix)
    (?:api[_-]?key|secret|passwd|password|auth[_-]?token|access[_-]?token|private[_-]?key|client[_-]?secret)\w*
    ["']?\s*[:=]\s*(["'])([A-Za-z0-9_\-/+=.@!$%^&*]{10,})\1''')
SECRET_TOKENS = re.compile(r'AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|\bsk-[A-Za-z0-9]{20,}|\bghp_[A-Za-z0-9]{30,}|\bxox[baprs]-[A-Za-z0-9-]{10,}')
PLACEHOLDER = re.compile(r'(?i)example|your[_-]|xxx|changeme|change_me|placeholder|<[^>]+>|\$\{|\{\{|process\.env|dummy|sample|test|todo|\*{3,}|\.\.\.|password123|abc123|secret123')
NONSECRET_VALUE = re.compile(r'(?i)^(?:production|development|staging|local|admin|administrator|user|guest|anonymous|unknown|default|disabled|enabled|true|false|null|none|debug|release)$')

def scan_secrets(raw, path):
    out = []
    for i, line in enumerate(raw, 1):
        if len(line) > 1000:
            continue
        m = SECRET_RE.search(line)
        if m and not PLACEHOLDER.search(m.group(2)) and not re.match(r'^[a-z_]+\(.*\)$', m.group(2)) and not NONSECRET_VALUE.match(m.group(2)):
            out.append(Finding(path, i, i, 'SECRET', 'high', 'credential-looking value written into source', 'read it from an environment variable or secret store'))
        elif SECRET_TOKENS.search(line):
            out.append(Finding(path, i, i, 'SECRET', 'high', 'token/private key pattern in source', 'remove it, rotate it, load from environment'))
    return out

# ---------------------------------------------------------------- scan: JS / TS

SQL_RE = re.compile(r'\b(select|insert\s+into|update|delete\s+from|drop\s+table|alter\s+table|where|values|order\s+by)\b', re.I)
JS_REQ = re.compile(r'\breq(?:uest)?\.(?:body|query|params|headers|cookies)\b')
JS_PASSWORDISH = re.compile(r'(?i)pass(?:word|wd)|pwd|secret|token|session|nonce|otp|api[_-]?key')

def js_taint(code):
    names = set()
    for line in code:
        m = re.search(r'(?:const|let|var)\s+(\w+)\s*=\s*(?:await\s+)?req(?:uest)?\.(?:body|query|params|headers|cookies)\b', line)
        if m:
            names.add(m.group(1))
        m = re.search(r'(?:const|let|var)\s*\{([^}]*)\}\s*=\s*req(?:uest)?\.(?:body|query|params|headers|cookies)\b', line)
        if m:
            for part in m.group(1).split(','):
                part = part.split('=')[0]
                nm = part.split(':')[-1].strip()
                if re.fullmatch(r'\w+', nm):
                    names.add(nm)
    return names

def has_user_input(line, taint):
    if JS_REQ.search(line):
        return True
    return any(re.search(r'(?<![\w.$])' + re.escape(t) + r'\b', line) for t in taint)

def scan_js(path, text):
    raw = text.split('\n')
    if any(len(l) > 2000 for l in raw):
        return [], 'minified/very long lines'
    code = js_strip(text)
    keep = js_strip(text, keep_strings=True)
    taint = js_taint(code)
    F = []
    def add(i, rule, sev, msg, fix, end=None):
        if not suppressed(raw, i, rule):
            F.append(Finding(path, i, end or i, rule, sev, msg, fix))
    async_names = set()
    for line in code:
        for pat in (r'async\s+function\s*\*?\s*(\w+)', r'(?:const|let|var)\s+(\w+)\s*=\s*async\b', r'^\s*(?:static\s+)?async\s+(\w+)\s*\(', r'\b(\w+)\s*:\s*async\b'):
            for m in re.finditer(pat, line):
                async_names.add(m.group(1))
    cors_wild = [i for i, l in enumerate(keep, 1) if re.search(r'origin\s*:\s*[\'"]\*[\'"]', l)]
    if cors_wild and any(re.search(r'credentials\s*:\s*true', l) for l in code):
        add(cors_wild[0], 'JS-CORS', 'high', "CORS origin '*' together with credentials: true", 'list the allowed origins explicitly')
    for i, line in enumerate(code, 1):
        if not line.strip():
            continue
        r = keep[i - 1]
        if re.search(r'\b(?:eval|new\s+Function)\s*\(', line):
            add(i, 'JS-EVAL', 'high', 'eval/new Function executes a string as code', 'parse data with JSON.parse or use a lookup table')
        m = re.search(r'\.(?:innerHTML|outerHTML)\s*(\+?=)(?!=)', line)
        if m:
            eq_pos = r.find(m.group(1), r.find('.innerHTML') if '.innerHTML' in r else r.find('.outerHTML'))
            rhs_full = r[eq_pos + len(m.group(1)):]
            rhs = rhs_full.split(';', 1)[0].strip()
            const = re.fullmatch(r'(["\'])[^"\']*\1', rhs) or re.fullmatch(r'`[^`$]*`', rhs)
            if not const:
                add(i, 'JS-XSS-HTML', 'high', 'HTML assigned from a non-constant value (XSS)', 'use textContent, or sanitize (DOMPurify) first')
        if 'dangerouslySetInnerHTML' in line:
            add(i, 'JS-XSS-REACT', 'medium', 'dangerouslySetInnerHTML bypasses escaping', 'sanitize the HTML (DOMPurify) or render it as React elements')
        if re.search(r'\bdocument\.write(?:ln)?\s*\(', line):
            add(i, 'JS-XSS-DOCWRITE', 'medium', 'document.write injects raw HTML', 'create elements and set textContent')
        m = re.search(r'\bres\.(?:send|write|end)\s*\(', line)
        if m:
            arg = line[m.end():].lstrip()
            if not arg.startswith(('{', '[')) and has_user_input(call_text(code, i - 1, m.start()), taint):
                add(i, 'JS-XSS-REFLECT', 'high', 'request data written into the response body unescaped (reflected XSS)', 'send JSON with res.json(...) or HTML-escape the value')
        m = re.search(r'\.(?:query|execute|raw|\$queryRawUnsafe|\$executeRawUnsafe|queryRawUnsafe|executeRawUnsafe)\s*\(', line)
        if m:
            ct = call_text(keep, i - 1, m.start())
            if SQL_RE.search(ct) and (re.search(r'`[^`]*\$\{', ct) or re.search(r'["\'`]\s*\+\s*[\w(]|[\w)\]]\s*\+\s*["\'`]', ct)):
                add(i, 'JS-SQLI', 'high', 'SQL built by string interpolation/concatenation', 'use placeholders: db.query("... WHERE id = $1", [id])')
        m = re.search(r'(?<![.\w$])(?:exec|execSync)\s*\(|(?:child_process|cp|shell)\.(?:exec|execSync)\s*\(', line)
        if m:
            ct = call_text(keep, i - 1, m.start())
            if re.search(r'`[^`]*\$\{', ct) or re.search(r'["\'`]\s*\+\s*[\w(]|[\w)\]]\s*\+\s*["\'`]', ct):
                add(i, 'JS-CMDI', 'high', 'shell command built from variables (command injection)', 'use execFile/spawn with an argument array')
        m = re.search(r'\b(?:readFile|readFileSync|createReadStream|sendFile|writeFile|writeFileSync|unlink|unlinkSync|readdir|readdirSync)\s*\(', line)
        if m and has_user_input(call_text(code, i - 1, m.start()), taint):
            add(i, 'JS-PATH-USER', 'high', 'file path derived from request data (path traversal)', 'resolve against a fixed base dir and reject paths that escape it')
        m = re.search(r'\b(?:console|logger|log)\.(?:log|info|warn|error|debug)\s*\(', line)
        if m and has_user_input(call_text(code, i - 1, m.start()), taint):
            add(i, 'JS-LOG-USER', 'medium', 'request data logged raw (log injection)', 'strip newlines/control chars or log it as a structured field')
        if re.search(r'\.forEach\s*\(\s*async\b', line):
            add(i, 'JS-FOREACH-ASYNC', 'high', 'forEach does not await an async callback', 'use for...of with await, or Promise.all(items.map(...))')
        if re.search(r'catch\s*(?:\([^)]*\))?\s*\{\s*\}', line) or (re.search(r'catch\s*(?:\([^)]*\))?\s*\{\s*$', line) and i < len(code) and code[i].strip() == '}'):
            add(i, 'JS-EMPTY-CATCH', 'medium', 'empty catch swallows the error', 'handle it, or rethrow, or log with context')
        if re.search(r'for\s*\([^;]*;\s*\w+\s*<=\s*[\w.\[\]()]+\.length\b', line):
            add(i, 'JS-OFF-BY-ONE', 'medium', 'loop runs to index === length (one past the end)', 'use < length')
        if re.search(r'rejectUnauthorized\s*:\s*false', line) or re.search(r'NODE_TLS_REJECT_UNAUTHORIZED', r) and re.search(r'[\'"]?0[\'"]?', r):
            add(i, 'JS-TLS-OFF', 'high', 'TLS certificate verification disabled', 'fix the certificate instead of disabling verification')
        if re.search(r'createHash\s*\(', line) and re.search(r'createHash\s*\(\s*[\'"](?:md5|sha1)[\'"]', r):
            ctx = ' '.join(raw[max(0, i - 3):i + 2])
            if re.search(r'(?i)pass(?:word|wd)|pwd', ctx):
                add(i, 'JS-WEAK-HASH', 'medium', 'md5/sha1 used near a password', 'use bcrypt, scrypt or argon2')
        if re.search(r'Math\.random\s*\(', line) and JS_PASSWORDISH.search(' '.join(raw[max(0, i - 3):i])):
            add(i, 'JS-RANDOM-SECRET', 'medium', 'Math.random is not cryptographically secure', 'use crypto.randomBytes / crypto.randomUUID')
        for m in re.finditer(r'(?:^|[{;]|=>)\s*(fetch|axios(?:\.\w+)?)\s*\(', line):
            pre = line[:m.start(1)].rstrip()
            if pre and pre[-1] not in '{;>' and not pre.endswith('=>'):
                continue
            if re.search(r'(?:await|return)\s*$', line[:m.start(1)]):
                continue
            if re.search(r'^\s*(?:const|let|var)\b', line[:m.start(1)].rsplit(';', 1)[-1]):
                continue
            call_end = call_text(code, i - 1, m.end(1), limit=6)
            depth = 0
            close_at = None
            for ci, ch in enumerate(call_end):
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        close_at = ci
                        break
            handled = close_at is not None and re.match(r'\s*\.(?:then|catch)\b', call_end[close_at + 1:])
            if not handled and not (i < len(code) and re.match(r'\s*\.(?:then|catch|finally)\b', code[i])):
                add(i, 'JS-FLOATING-PROMISE', 'medium', 'request promise is neither awaited nor handled', 'await it inside try/catch, or add .catch()')
        if async_names:
            for m in re.finditer(r'(?:^|[{;]|=>)\s*(?:this\.)?(\w+)\s*\(', line):
                nm = m.group(1)
                if nm not in async_names:
                    continue
                pre = line[:m.start(1)].rstrip()
                if pre and pre[-1] not in '{;>':
                    continue
                if re.search(r'(?:await|return)\s*$', line[:m.start(1)]):
                    continue
                if re.search(r'^\s*(?:const|let|var)\b', line[:m.start(1)].rsplit(';', 1)[-1]):
                    continue
                if not (i < len(code) and re.match(r'\s*\.(?:then|catch|finally)\b', code[i])):
                    add(i, 'JS-UNAWAITED', 'high', f'async function {nm}() called without await', 'await it, or return it, or handle the promise')
        if re.search(r'JSON\.parse\s*\(', line) and not any(re.search(r'\btry\s*\{', code[k]) for k in range(max(0, i - 16), i)) and '.catch' not in line:
            add(i, 'JS-JSON-PARSE', 'low', 'JSON.parse throws on malformed input and is not inside try', 'wrap in try/catch and return a 4xx / fallback')
        if re.search(r'(?<![=!<>])[=!]=(?![=])', line) and not re.search(r'[=!]=\s*(?:null|undefined)\b|\b(?:null|undefined)\s*[=!]=|typeof', line):
            add(i, 'JS-LOOSE-EQ', 'low', '== / != coerce types', 'use === / !==')
    return F, ''

# ---------------------------------------------------------------- scan: Python (AST)

ROUTE_ATTRS = {'route', 'get', 'post', 'put', 'delete', 'patch', 'head', 'options', 'websocket', 'api_route'}
INFRA_ARGS = {'self', 'cls', 'request', 'req', 'response', 'db', 'session', 'background_tasks', 'settings', 'current_user'}
LOG_METHODS = {'debug', 'info', 'warning', 'warn', 'error', 'exception', 'critical', 'log'}
EXEC_METHODS = {'execute', 'executemany', 'executescript', 'raw', 'extra'}
REQUESTS_METHODS = {'get', 'post', 'put', 'delete', 'patch', 'head', 'options', 'request'}
SUBPROC = {'run', 'call', 'check_call', 'check_output', 'Popen', 'getoutput', 'getstatusoutput'}

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

def const_str(n):
    return isinstance(n, ast.Constant) and isinstance(n.value, str)

class PyScan(ast.NodeVisitor):
    def __init__(self, path, raw, tree):
        self.path, self.raw, self.f = path, raw, []
        self.taint = [set()]
        self.async_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
        self.base = os.path.basename(path)

    def add(self, node, rule, sev, msg, fix):
        line = getattr(node, 'lineno', 1)
        if not suppressed(self.raw, line, rule):
            self.f.append(Finding(self.path, line, getattr(node, 'end_lineno', line) or line, rule, sev, msg, fix))

    def user(self, node):
        t = self.taint[-1]
        for s in ast.walk(node):
            if isinstance(s, ast.Name) and s.id in t:
                return True
            if isinstance(s, (ast.Attribute, ast.Subscript)) and root_name(s) in ('request', 'req'):
                return True
        return False

    def line_text(self, node):
        return self.raw[node.lineno - 1] if 0 < node.lineno <= len(self.raw) else ''

    def dyn_sql(self, a):
        if isinstance(a, ast.Call) and dotted(a.func).split('.')[-1] == 'text' and a.args:
            a = a.args[0]
        strs = ' '.join(s.value for s in ast.walk(a) if const_str(s))
        if not SQL_RE.search(strs):
            return False
        if isinstance(a, ast.JoinedStr):
            return any(isinstance(v, ast.FormattedValue) for v in a.values)
        if isinstance(a, ast.BinOp) and isinstance(a.op, (ast.Mod, ast.Add)):
            return not (isinstance(a.left, ast.Constant) and isinstance(a.right, ast.Constant))
        if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) and a.func.attr == 'format' and const_str(a.func.value):
            return True
        return False

    def visit_FunctionDef(self, node):
        mut = [d for d in node.args.defaults + [k for k in node.args.kw_defaults if k] if isinstance(d, (ast.List, ast.Dict, ast.Set))
               or (isinstance(d, ast.Call) and dotted(d.func) in ('list', 'dict', 'set'))]
        for d in mut:
            self.add(d, 'PY-MUT-DEFAULT', 'high', 'mutable default argument is shared between calls', 'default to None and create the object inside the function')
        t = set(self.taint[-1])
        if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ROUTE_ATTRS for d in node.decorator_list):
            for a in node.args.args + node.args.kwonlyargs:
                if a.arg not in INFRA_ARGS:
                    t.add(a.arg)
        for s in ast.walk(node):
            if isinstance(s, ast.Assign) and self.user_with(s.value, t):
                for tg in s.targets:
                    for n in ast.walk(tg):
                        if isinstance(n, ast.Name):
                            t.add(n.id)
            elif isinstance(s, (ast.AnnAssign, ast.AugAssign)) and s.value is not None and self.user_with(s.value, t) and isinstance(s.target, ast.Name):
                t.add(s.target.id)
            elif isinstance(s, ast.NamedExpr) and self.user_with(s.value, t):
                t.add(s.target.id)
        self.taint.append(t)
        self.generic_visit(node)
        self.taint.pop()
    visit_AsyncFunctionDef = visit_FunctionDef

    def user_with(self, node, t):
        for s in ast.walk(node):
            if isinstance(s, ast.Name) and s.id in t:
                return True
            if isinstance(s, (ast.Attribute, ast.Subscript)) and root_name(s) in ('request', 'req'):
                return True
        return False

    def visit_ExceptHandler(self, node):
        if node.type is None:
            self.add(node, 'PY-BARE-EXCEPT', 'medium', 'bare except also catches KeyboardInterrupt/SystemExit and hides bugs', 'catch the specific exception you expect')
        elif dotted(node.type) in ('Exception', 'BaseException') and all(isinstance(b, (ast.Pass, ast.Continue)) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)) for b in node.body):
            self.add(node, 'PY-SWALLOWED', 'medium', 'exception caught and silently ignored', 'handle it, log it with context, or let it propagate')
        self.generic_visit(node)

    def visit_Compare(self, node):
        for op, c in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(c, ast.Constant) and isinstance(c.value, (str, bytes, int, float)) and not isinstance(c.value, bool):
                self.add(node, 'PY-IS-LITERAL', 'high', "'is' compares identity, not value, for a literal", 'use == / !=')
        self.generic_visit(node)

    def visit_Expr(self, node):
        v = node.value
        if isinstance(v, ast.Call):
            f = v.func
            if (isinstance(f, ast.Name) and f.id in self.async_names) or \
               (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == 'self' and f.attr in self.async_names) or \
               dotted(f) in ('asyncio.sleep', 'asyncio.gather', 'asyncio.wait_for'):
                self.add(node, 'PY-UNAWAITED', 'high', 'coroutine created but never awaited', 'add await (or asyncio.create_task and keep the reference)')
        self.generic_visit(node)

    def visit_Return(self, node):
        if node.value is not None:
            self.check_html(node.value, node)
        self.generic_visit(node)

    def check_html(self, v, node):
        if isinstance(v, ast.JoinedStr) and any('<' in c.value for c in v.values if const_str(c)) and \
           any(isinstance(x, ast.FormattedValue) and self.user(x.value) for x in v.values):
            self.add(node, 'PY-XSS-REFLECT', 'high', 'request data interpolated into an HTML string (reflected XSS)', 'render a template with autoescape, or html.escape() the value')

    def visit_Assign(self, node):
        if self.base.startswith('settings') and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            nm = node.targets[0].id
            if nm == 'DEBUG' and isinstance(node.value, ast.Constant) and node.value.value is True:
                self.add(node, 'PY-DEBUG-ON', 'medium', 'DEBUG = True in settings', 'read it from the environment, default False')
            if nm == 'ALLOWED_HOSTS' and isinstance(node.value, (ast.List, ast.Tuple)) and any(const_str(e) and e.value == '*' for e in node.value.elts):
                self.add(node, 'PY-ALLOWED-HOSTS', 'medium', "ALLOWED_HOSTS = ['*']", 'list the real host names')
        self.generic_visit(node)

    def visit_Call(self, node):
        name = dotted(node.func)
        last = name.split('.')[-1]
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        args = node.args
        a0 = args[0] if args else None
        if name in ('eval', 'exec') and a0 is not None and not isinstance(a0, ast.Constant):
            self.add(node, 'PY-EVAL', 'high', f'{name}() executes a computed string', 'parse data with json/ast.literal_eval, or use a lookup table')
        if name in ('os.system', 'os.popen') and a0 is not None and not isinstance(a0, ast.Constant):
            self.add(node, 'PY-CMDI', 'high', f'{name} with a computed command string', 'subprocess.run([...], shell=False) with an argument list')
        if name.startswith('subprocess.') and last in SUBPROC and a0 is not None and not isinstance(a0, (ast.Constant, ast.List, ast.Tuple)) \
                and isinstance(kw.get('shell'), ast.Constant) and kw['shell'].value is True:
            self.add(node, 'PY-CMDI', 'high', 'subprocess with shell=True and a computed command', 'pass an argument list and drop shell=True')
        if name in ('pickle.load', 'pickle.loads', 'cPickle.loads', 'marshal.loads') and a0 is not None and self.user(a0):
            self.add(node, 'PY-PICKLE', 'high', 'unpickling data that comes from the request', 'use JSON; never unpickle untrusted data')
        if last == 'load' and name.startswith('yaml'):
            ld = dotted(kw['Loader']) if 'Loader' in kw else ''
            if not ld or 'Safe' not in ld:
                self.add(node, 'PY-YAML-LOAD', 'medium', 'yaml.load without SafeLoader can execute code', 'yaml.safe_load(...)')
        if name.startswith('requests.') and last in REQUESTS_METHODS:
            if 'timeout' not in kw and not any(k.arg is None for k in node.keywords):
                self.add(node, 'PY-NO-TIMEOUT', 'medium', 'requests call without timeout can hang forever', 'pass timeout=(3, 10)')
            if a0 is not None and self.user(a0):
                self.add(node, 'PY-SSRF', 'medium', 'request to a URL taken from user input (SSRF)', 'allow-list hosts before fetching')
        if isinstance(kw.get('verify'), ast.Constant) and kw['verify'].value is False:
            self.add(node, 'PY-TLS-OFF', 'high', 'TLS verification disabled (verify=False)', 'fix the certificate chain instead')
        if last in EXEC_METHODS and a0 is not None and self.dyn_sql(a0):
            self.add(node, 'PY-SQLI', 'high', 'SQL built with f-string/%/+/format', 'pass parameters: cursor.execute("... WHERE id = %s", (x,))')
        if (name == 'open' or last in ('send_file', 'send_from_directory', 'FileResponse')) and any(self.user(a) for a in args):
            self.add(node, 'PY-PATH-USER', 'high', 'file path derived from request data (path traversal)', 'resolve against a fixed base dir and reject paths that escape it')
        if last == 'redirect' and a0 is not None and self.user(a0):
            self.add(node, 'PY-OPEN-REDIRECT', 'medium', 'redirect target comes from user input', 'allow-list targets or use relative paths only')
        if last in LOG_METHODS and isinstance(node.func, ast.Attribute) and root_name(node.func) in ('logger', 'logging', 'log', '_logger', 'LOGGER') \
                and any(self.user(a) for a in args):
            self.add(node, 'PY-LOG-USER', 'medium', 'request data logged raw (log injection)', 'log it as a structured field or strip newlines/control chars')
        if name in ('hashlib.md5', 'hashlib.sha1') and re.search(r'(?i)pass(?:word|wd)|pwd', self.line_text(node)):
            self.add(node, 'PY-WEAK-HASH', 'medium', 'md5/sha1 used for a password', 'use bcrypt, scrypt or argon2')
        if name.startswith('random.') and last in ('random', 'randint', 'choice', 'choices', 'getrandbits') and re.search(r'(?i)token|secret|pass(?:word|wd)|otp|nonce|session|api[_-]?key', self.line_text(node)):
            self.add(node, 'PY-RANDOM-SECRET', 'medium', 'random is not cryptographically secure', 'use the secrets module')
        if last == 'run' and isinstance(kw.get('debug'), ast.Constant) and kw['debug'].value is True and name.split('.')[0] in ('app', 'application', 'flask_app'):
            self.add(node, 'PY-DEBUG-ON', 'medium', 'debug=True exposes the Werkzeug console', 'enable debug only from an environment variable')
        if last == 'render_template_string' and a0 is not None and not isinstance(a0, ast.Constant):
            self.add(node, 'PY-SSTI', 'high', 'template compiled from a computed string (SSTI)', 'use render_template with a fixed file')
        if last in ('mark_safe', 'Markup') and a0 is not None and not isinstance(a0, ast.Constant):
            self.add(node, 'PY-XSS-MARKSAFE', 'high', f'{last}() disables escaping for a computed value', 'escape the parts first (html.escape / format_html)')
        if last == 'Environment' and isinstance(kw.get('autoescape'), ast.Constant) and kw['autoescape'].value is False:
            self.add(node, 'PY-AUTOESCAPE-OFF', 'high', 'Jinja autoescape disabled', 'autoescape=True (or select_autoescape())')
        if name == 'tempfile.mktemp':
            self.add(node, 'PY-MKTEMP', 'low', 'tempfile.mktemp is racy', 'use NamedTemporaryFile or mkstemp')
        if last in ('HTMLResponse', 'make_response', 'Response') and a0 is not None:
            self.check_html(a0, node)
        self.generic_visit(node)

    def visit_Try(self, node):
        self.generic_visit(node)
    visit_TryStar = visit_Try

def scan_py(path, text):
    raw = text.split('\n')
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], 'syntax error (see syntax check)'
    v = PyScan(path, raw, tree)
    v.visit(tree)
    return v.f, ''

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
        raw = text.split('\n')
        if ext in PY_EXT:
            f, note = scan_py(rp, text)
        else:
            f, note = scan_js(rp, text)
        f += [x for x in scan_secrets(raw, rp) if not suppressed(raw, x.line, 'SECRET')]
        if note:
            notes.append(f'{rp}: {note}')
        for x in f:
            if not low and x.sev == 'low':
                continue
            if added is None or all_lines or any(k in added for k in range(x.line, x.end + 1)):
                findings.append(x)
    findings = sorted(set(findings), key=lambda x: ({'high': 0, 'medium': 1, 'low': 2}[x.sev], x.path, x.line))
    return findings, notes

def cmd_scan(a):
    t = get_targets(a.paths, a.base)
    findings, notes = scan_files(t, a.low, a.all_lines)
    print_findings(findings, notes, t)
    return 1 if any(f.sev == 'high' for f in findings) else 0

def print_findings(findings, notes, t, limit=40):
    if not findings:
        print(f'scan: 0 findings in {len(t.files)} files ({t.mode})')
    else:
        print(f'scan: {len(findings)} findings in {len(t.files)} files ({t.mode})')
    for f in findings[:limit]:
        print(f'  {f.sev.upper():6} {f.path}:{f.line}  {f.rule}  {f.msg}  -> {f.fix}')
    if len(findings) > limit:
        print(f'  ... +{len(findings) - limit} more')
    for n in notes[:5]:
        print(f'  note: {n}')

# ---------------------------------------------------------------- syntax

def syntax_check(t):
    bad, ok, skipped = [], 0, 0
    node = shutil.which('node')
    for p, _ in sorted(t.files.items()):
        ext = os.path.splitext(p)[1]
        rp = rel(t, p)
        if ext not in CODE_EXT and ext != '.json':
            continue
        text = read_text(p)
        if text is None:
            skipped += 1; continue
        if ext == '.py':
            try:
                ast.parse(text, filename=rp)
                ok += 1
            except SyntaxError as e:
                bad.append(f'{rp}:{e.lineno}  {e.msg}')
        elif ext == '.json':
            try:
                json.loads(text); ok += 1
            except ValueError as e:
                bad.append(f'{rp}  invalid JSON: {e}')
        elif ext in ('.js', '.mjs', '.cjs') and node:
            r = subprocess.run([node, '--check', p], capture_output=True, text=True, timeout=30)
            if r.returncode != 0 and ('import statement outside a module' in r.stderr or "Unexpected token 'export'" in r.stderr):
                tmp = p + '.vf-check.mjs'
                try:
                    with open(tmp, 'w') as f:
                        f.write(text)
                    r = subprocess.run([node, '--check', tmp], capture_output=True, text=True, timeout=30)
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)
            if r.returncode == 0:
                ok += 1
            else:
                m = re.search(r':(\d+)\n', r.stderr)
                msg = next((l for l in r.stderr.split('\n') if 'Error' in l), 'syntax error')
                bad.append(f'{rp}:{m.group(1) if m else "?"}  {msg.strip()}')
        else:
            skipped += 1
    return ok, bad, skipped

# ---------------------------------------------------------------- imports

PY_ALIASES = {'yaml': 'pyyaml', 'pil': 'pillow', 'cv2': 'opencv-python', 'bs4': 'beautifulsoup4', 'sklearn': 'scikit-learn',
              'dotenv': 'python-dotenv', 'jwt': 'pyjwt', 'dateutil': 'python-dateutil', 'attr': 'attrs', 'openssl': 'pyopenssl',
              'crypto': 'pycryptodome', 'serial': 'pyserial', 'magic': 'python-magic', 'git': 'gitpython', 'docx': 'python-docx',
              'pptx': 'python-pptx', 'fitz': 'pymupdf', 'skimage': 'scikit-image', 'mysqldb': 'mysqlclient', 'google': 'protobuf',
              'ruamel': 'ruamel-yaml', 'zmq': 'pyzmq', 'usb': 'pyusb', 'wx': 'wxpython', 'gi': 'pygobject', 'psycopg2': 'psycopg2-binary'}
NODE_BUILTINS = set('assert async_hooks buffer child_process cluster console constants crypto dgram diagnostics_channel dns domain events fs http http2 https inspector module net os path perf_hooks process punycode querystring readline repl stream string_decoder sys timers tls trace_events tty url util v8 vm wasi worker_threads zlib'.split())
NODE_PREFIX_ONLY = {'test', 'sqlite', 'sea'}

def norm_dist(s):
    return re.sub(r'[-_.]+', '-', s).lower()

def declared_py(root):
    names = set()
    pat = re.compile(r'(?i)^(?:.*requirements.*\.(?:txt|in)|pyproject\.toml|setup\.cfg|setup\.py|pipfile|environment\.ya?ml|tox\.ini)$')
    n = 0
    for f in walk_files(root):
        if not pat.match(os.path.basename(f)):
            continue
        n += 1
        if n > 60:
            break
        text = read_text(f) or ''
        b = os.path.basename(f).lower()
        if b.endswith(('.txt', '.in')):
            for line in text.split('\n'):
                m = re.match(r'\s*([A-Za-z0-9][A-Za-z0-9._-]*)', line)
                if m and not line.lstrip().startswith(('#', '-')):
                    names.add(norm_dist(m.group(1)))
        else:
            for m in re.finditer(r'["\']([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;@(][^"\']*)?["\']', text):
                names.add(norm_dist(m.group(1)))
            for m in re.finditer(r'(?m)^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*["\'{\[]', text):
                names.add(norm_dist(m.group(1)))
            for m in re.finditer(r'(?m)^\s*-\s*([A-Za-z0-9][A-Za-z0-9._-]*)', text):
                names.add(norm_dist(m.group(1)))
    return names

def py_site_dirs(root):
    dirs = []
    for name in ('.venv', 'venv', 'env'):
        dirs += glob.glob(os.path.join(root, name, 'lib', 'python*', 'site-packages')) + glob.glob(os.path.join(root, name, 'Lib', 'site-packages'))
    dirs += [p for p in sys.path if p and os.path.isdir(p) and os.path.abspath(p) != os.path.abspath(root)]
    return dirs

def find_in(dotted, dirs):
    parts = dotted.split('.')
    for d in dirs:
        cur, kind, ok = d, None, True
        for i, part in enumerate(parts):
            pkg = os.path.join(cur, part)
            last = i == len(parts) - 1
            if os.path.isdir(pkg):
                cur, kind = pkg, 'pkg'
                continue
            if os.path.isfile(pkg + '.py') or os.path.isfile(pkg + '.so') or glob.glob(pkg + '.*.so') or glob.glob(pkg + '.*.pyd') or glob.glob(pkg + '.pyd'):
                path = pkg + '.py'
                return ('mod' if last else 'mod-sub', path if os.path.isfile(path) else None)
            ok = False
            break
        if ok:
            return (kind, cur)
    return None

_names_cache = {}

def module_names(pyfile):
    """top-level names a module defines, or None when it cannot be known statically"""
    if pyfile in _names_cache:
        return _names_cache[pyfile]
    res = None
    text = read_text(pyfile) if pyfile else None
    if text is not None:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            names, unknown = set(), False
            def visit(stmts):
                nonlocal unknown
                for s in stmts:
                    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.add(s.name)
                        if s.name == '__getattr__':
                            unknown = True
                    elif isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        for tg in (s.targets if isinstance(s, ast.Assign) else [s.target]):
                            for n in ast.walk(tg):
                                if isinstance(n, ast.Name):
                                    names.add(n.id)
                    elif isinstance(s, ast.Import):
                        for al in s.names:
                            names.add((al.asname or al.name).split('.')[0])
                    elif isinstance(s, ast.ImportFrom):
                        for al in s.names:
                            if al.name == '*':
                                unknown = True
                            names.add(al.asname or al.name)
                    elif isinstance(s, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                        for fld in ('body', 'orelse', 'finalbody'):
                            visit(getattr(s, fld, []) or [])
                        for h in getattr(s, 'handlers', []) or []:
                            visit(h.body)
                    if isinstance(s, (ast.For, ast.With)):
                        for n in ast.walk(s.target if isinstance(s, ast.For) else ast.Module(body=[], type_ignores=[])):
                            if isinstance(n, ast.Name):
                                names.add(n.id)
            visit(tree.body)
            if re.search(r'\bglobals\s*\(\s*\)|\bsetattr\s*\(|\bexec\s*\(|sys\.modules\[', text):
                unknown = True
            res = None if unknown else names
    _names_cache[pyfile] = res
    return res

def py_import_findings(t, deep=True):
    root = t.root
    stdlib = set(getattr(sys, 'stdlib_module_names', ())) | set(sys.builtin_module_names)
    stdlib_dir = os.path.dirname(os.__file__)
    site = py_site_dirs(root)
    declared = declared_py(root)
    try:
        from importlib.metadata import packages_distributions
        dist_map = packages_distributions()
    except Exception:
        dist_map = {}
    out, count = [], 0
    for p, added in sorted(t.files.items()):
        if os.path.splitext(p)[1] != '.py':
            continue
        text = read_text(p)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rp = rel(t, p)
        raw = text.split('\n')
        fdir = os.path.dirname(p)
        local_dirs, d = [], fdir
        while True:
            local_dirs.append(d)
            if os.path.abspath(d) == os.path.abspath(root) or os.path.dirname(d) == d:
                break
            d = os.path.dirname(d)
        local_dirs += [os.path.join(root, 'src'), os.path.join(root, 'lib')]
        optional = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Try) and any((h.type is None) or ('ImportError' in ast.dump(h.type)) or ('ModuleNotFoundError' in ast.dump(h.type)) for h in n.handlers):
                for s in ast.walk(ast.Module(body=n.body, type_ignores=[])):
                    if isinstance(s, (ast.Import, ast.ImportFrom)):
                        optional.add(s.lineno)
            # `if TYPE_CHECKING:` (or `typing.TYPE_CHECKING`): imports here are type-stub-only
            # (e.g. _typeshed) and never exist at runtime, so treat them as optional too.
            if isinstance(n, ast.If) and (
                (isinstance(n.test, ast.Name) and n.test.id == 'TYPE_CHECKING') or
                (isinstance(n.test, ast.Attribute) and n.test.attr == 'TYPE_CHECKING')
            ):
                for s in ast.walk(ast.Module(body=n.body, type_ignores=[])):
                    if isinstance(s, (ast.Import, ast.ImportFrom)):
                        optional.add(s.lineno)
        for n in ast.walk(tree):
            if not isinstance(n, (ast.Import, ast.ImportFrom)):
                continue
            if added is not None and not any(k in added for k in range(n.lineno, (n.end_lineno or n.lineno) + 1)):
                continue
            if suppressed(raw, n.lineno, 'IMPORT'):
                continue
            def emit(rule, sev, msg, fix):
                out.append(Finding(rp, n.lineno, n.lineno, rule, sev, msg, fix))
            if isinstance(n, ast.Import):
                targets = [(al.name, []) for al in n.names]
            elif n.level:
                base = fdir
                for _ in range(n.level - 1):
                    base = os.path.dirname(base)
                if n.module:
                    tp = os.path.join(base, *n.module.split('.'))
                    found = os.path.isdir(tp) or os.path.isfile(tp + '.py')
                    count += 1
                    if not found:
                        emit('IMPORT-UNRESOLVED', 'high', f"relative import '{'.' * n.level}{n.module}' does not exist", 'check the path; create the module or fix the import')
                        continue
                    pyf = os.path.join(tp, '__init__.py') if os.path.isdir(tp) else tp + '.py'
                    targets = [(None, [al.name for al in n.names], pyf, tp)]
                else:
                    targets = [(None, [al.name for al in n.names], os.path.join(base, '__init__.py'), base)]
                for _, names_, pyf, tp in targets:
                    if not deep:
                        continue
                    known = module_names(pyf) if os.path.isfile(pyf) else None
                    for nm in names_:
                        if nm == '*':
                            continue
                        sub = os.path.join(tp, nm)
                        if os.path.isdir(sub) or os.path.isfile(sub + '.py'):
                            continue
                        if known is not None and nm not in known:
                            emit('IMPORT-NAME', 'high', f"'{nm}' is not defined in {os.path.relpath(pyf, root)}", 'check the exact name in that module')
                continue
            else:
                targets = [(n.module, [al.name for al in n.names])]
            for tgt in targets:
                modname, names_ = tgt[0], tgt[1]
                if not modname:
                    continue
                count += 1
                top = modname.split('.')[0]
                opt = n.lineno in optional
                res = None
                if top in stdlib:
                    res = ('std', None) if '.' not in modname else (find_in(modname, [stdlib_dir]) or (None if top not in sys.builtin_module_names else ('std', None)))
                    if res and res[0] == 'mod-sub':
                        res = ('std', None)
                if res is None:
                    res = find_in(modname, local_dirs)
                if res is None:
                    res = find_in(modname, site)
                if res is None:
                    dn = norm_dist(PY_ALIASES.get(top.lower(), top))
                    dists = [norm_dist(x) for x in dist_map.get(top, [])]
                    is_declared = dn in declared or norm_dist(top) in declared or any(x in declared for x in dists)
                    if opt:
                        emit('IMPORT-OPTIONAL', 'low', f"'{modname}' not found (guarded by try/except ImportError or TYPE_CHECKING, treated as optional)", '')
                    elif is_declared:
                        emit('IMPORT-NOT-INSTALLED', 'low', f"'{modname}' is declared but not installed in this environment", 'pip install the project requirements')
                    elif '.' in modname and find_in(top, local_dirs + site + [stdlib_dir]):
                        emit('IMPORT-SUBMODULE', 'high', f"'{modname}': package '{top}' exists but has no such submodule", 'check the package layout for this version')
                    else:
                        emit('IMPORT-UNRESOLVED', 'high', f"'{modname}' is not stdlib, not in the project, not installed, not declared (invented package?)", 'verify it exists on PyPI, then add it to requirements')
                    continue
                if deep and names_ and res[0] in ('mod', 'pkg'):
                    pyf = res[1] if res[0] == 'mod' else os.path.join(res[1], '__init__.py')
                    if pyf and os.path.isfile(pyf):
                        known = module_names(pyf)
                        if known is not None and res[0] in ('mod', 'pkg'):
                            for nm in names_:
                                if nm == '*' or nm in known:
                                    continue
                                if res[0] == 'pkg' and (os.path.isdir(os.path.join(res[1], nm)) or find_in(nm, [res[1]])):
                                    continue
                                if opt:
                                    continue
                                emit('IMPORT-NAME', 'high', f"'{nm}' is not defined in module '{modname}'", 'check the exact name in the installed version')
    return out, count

# ---- JS

def strip_json_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    text = re.sub(r'(?m)^\s*//.*$', '', text)
    text = re.sub(r'(?m)(?<=[,\s])//[^"\n]*$', '', text)
    return re.sub(r',(\s*[}\]])', r'\1', text)

_pkg_cache = {}

def load_pkg(dirpath):
    if dirpath in _pkg_cache:
        return _pkg_cache[dirpath]
    data = None
    p = os.path.join(dirpath, 'package.json')
    if os.path.isfile(p):
        try:
            data = json.loads(read_text(p) or '{}')
        except ValueError:
            data = None
    _pkg_cache[dirpath] = data
    return data

def workspace_names(root):
    names, n = set(), 0
    for f in walk_files(root):
        if os.path.basename(f) == 'package.json':
            n += 1
            if n > 300:
                break
            try:
                d = json.loads(read_text(f) or '{}')
                if isinstance(d, dict) and d.get('name'):
                    names.add(d['name'])
            except ValueError:
                pass
    return names

def ts_aliases(root):
    """returns (mappings, base_urls). mappings: list of (pattern_prefix, [target_prefix,...], has_star)"""
    mappings, base_urls, seen_base = [], [], set()
    for name in ('tsconfig.json', 'jsconfig.json', 'tsconfig.base.json'):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            try:
                d = json.loads(strip_json_comments(read_text(p) or '{}'))
            except ValueError:
                continue
            co = d.get('compilerOptions', {}) if isinstance(d, dict) else {}
            base = os.path.normpath(os.path.join(root, co.get('baseUrl', '.')))
            if base not in seen_base:
                base_urls.append(base); seen_base.add(base)
            for k, targets in (co.get('paths') or {}).items():
                star = k.endswith('*')
                mappings.append((k[:-1] if star else k, [t[:-1] if t.endswith('*') else t for t in targets], star))
    return mappings, base_urls

def resolve_alias(spec, mappings, base_urls):
    """True/False/None: None means no mapping pattern matched (caller should try other resolution)"""
    matched_any = False
    for pat, targets, star in mappings:
        if star:
            if not spec.startswith(pat):
                continue
        elif spec != pat:
            continue
        matched_any = True
        rest = spec[len(pat):] if star else ''
        for tgt in targets:
            for b in base_urls:
                if js_resolve_file(os.path.normpath(os.path.join(b, tgt + rest))):
                    return True
    return False if matched_any else None

JS_EXTS = ['', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts', '.json', '.node', '.d.ts']

def js_resolve_file(base):
    for e in JS_EXTS:
        if os.path.isfile(base + e):
            return True
    if base.endswith(('.js', '.jsx', '.mjs', '.cjs')):
        stem = base.rsplit('.', 1)[0]
        for e in ('.ts', '.tsx', '.mts', '.cts', '.d.ts'):
            if os.path.isfile(stem + e):
                return True
    if os.path.isdir(base):
        for e in JS_EXTS[1:]:
            if os.path.isfile(os.path.join(base, 'index' + e)):
                return True
        if os.path.isfile(os.path.join(base, 'package.json')):
            return True
    return False

JS_IMPORT_PATTERNS = [
    re.compile(r'\bimport\s+(?:type\s+)?(?:[^\'";]*?\s+from\s+)?[\'"]([^\'"\n]+)[\'"]'),
    re.compile(r'\bexport\s+(?:type\s+)?(?:\*|\{[^}]*\})(?:\s+as\s+\w+)?\s+from\s+[\'"]([^\'"\n]+)[\'"]'),
    re.compile(r'\brequire\s*\(\s*[\'"]([^\'"\n]+)[\'"]\s*\)'),
    re.compile(r'\bimport\s*\(\s*[\'"]([^\'"\n]+)[\'"]\s*\)'),
]

def js_import_findings(t):
    root = t.root
    ws = workspace_names(root)
    ts_map, base_urls = ts_aliases(root)
    out, count = [], 0
    for p, added in sorted(t.files.items()):
        if os.path.splitext(p)[1] not in JS_EXT:
            continue
        text = read_text(p)
        if text is None:
            continue
        rp = rel(t, p)
        raw = text.split('\n')
        clean = '\n'.join(js_strip(text, keep_strings=True))
        offs = [0]
        for l in clean.split('\n'):
            offs.append(offs[-1] + len(l) + 1)
        fdir = os.path.dirname(p)
        chain, d = [], fdir
        while True:
            chain.append(d)
            if os.path.abspath(d) == os.path.abspath(root) or os.path.dirname(d) == d:
                break
            d = os.path.dirname(d)
        declared = set()
        for d in chain:
            pk = load_pkg(d)
            if pk:
                for key in ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'):
                    declared |= set((pk.get(key) or {}).keys())
        seen = set()
        for pat in JS_IMPORT_PATTERNS:
            for m in pat.finditer(clean):
                spec = m.group(1)
                line = bisect.bisect_right(offs, m.start(1)) 
                if (spec, line) in seen:
                    continue
                seen.add((spec, line))
                if added is not None and line not in added:
                    continue
                if suppressed(raw, line, 'IMPORT'):
                    continue
                count += 1
                def emit(rule, sev, msg, fix):
                    out.append(Finding(rp, line, line, rule, sev, msg, fix))
                if spec.startswith(('http:', 'https:', 'data:', 'file:', 'npm:', 'jsr:', 'virtual:', 'astro:', 'bun:', 'deno:', 'cloudflare:')) or '!' in spec or spec.startswith(('%', '~', '#', '$')):
                    continue
                if spec.startswith('.'):
                    if not js_resolve_file(os.path.normpath(os.path.join(fdir, spec))):
                        emit('IMPORT-UNRESOLVED', 'high', f"relative import '{spec}' does not resolve to a file", 'check the path and file extension')
                    continue
                if spec.startswith('/'):
                    r = resolve_alias(spec, ts_map, base_urls)
                    if r is True:
                        continue
                    if r is False:
                        emit('IMPORT-UNRESOLVED', 'high', f"'{spec}' matches a tsconfig path alias but no target file exists", 'check the tsconfig "paths" mapping and the target file')
                        continue
                r = resolve_alias(spec, ts_map, base_urls)
                if r is True:
                    continue
                if r is False:
                    emit('IMPORT-UNRESOLVED', 'high', f"'{spec}' matches a tsconfig path alias but no target file exists", 'check the tsconfig "paths" mapping and the target file')
                    continue
                bare = spec[5:] if spec.startswith('node:') else spec
                top = bare.split('/')[0]
                if spec.startswith('node:'):
                    if top in NODE_BUILTINS or top in NODE_PREFIX_ONLY:
                        continue
                    emit('IMPORT-UNRESOLVED', 'high', f"'{spec}' is not a Node built-in module", 'check the module name')
                    continue
                if top in NODE_BUILTINS:
                    continue
                if any(js_resolve_file(os.path.join(b, spec)) for b in base_urls):
                    continue
                pkg = '/'.join(bare.split('/')[:2]) if bare.startswith('@') else top
                sub = bare[len(pkg) + 1:]
                if pkg in ws:
                    continue
                installed = None
                for d in chain + [root]:
                    cand = os.path.join(d, 'node_modules', pkg)
                    if os.path.isdir(cand):
                        installed = cand
                        break
                types_ok = None
                if installed is None and os.path.splitext(p)[1] in ('.ts', '.tsx', '.mts', '.cts'):
                    tp = ('@types/' + pkg[1:].replace('/', '__')) if pkg.startswith('@') else '@types/' + pkg
                    for d in chain + [root]:
                        if os.path.isdir(os.path.join(d, 'node_modules', tp)):
                            types_ok = tp
                            break
                if installed is None and not types_ok:
                    opt = any(re.search(r'\btry\s*\{', l) for l in raw[max(0, line - 4):line])
                    if opt:
                        emit('IMPORT-OPTIONAL', 'low', f"'{pkg}' not found (inside try, treated as optional)", '')
                    elif pkg in declared:
                        emit('IMPORT-NOT-INSTALLED', 'low', f"'{pkg}' is declared but node_modules does not contain it", 'run the package manager install')
                    else:
                        emit('IMPORT-UNRESOLVED', 'high', f"'{pkg}' is not a Node built-in, not declared in package.json, not installed (invented package?)", 'verify it exists on npm, then add it to package.json')
                    continue
                if installed and pkg not in declared and not any(pkg == w for w in ws):
                    emit('IMPORT-UNDECLARED', 'low', f"'{pkg}' resolves only via node_modules hoisting; it is not in package.json", 'add it to dependencies')
                if installed and sub:
                    pj = load_pkg(installed)
                    if not (pj and pj.get('exports')) and not js_resolve_file(os.path.join(installed, sub)):
                        emit('IMPORT-SUBPATH', 'high', f"package '{pkg}' has no file '{sub}'", 'check the package layout for the installed version')
    return out, count

def registry_note(name, kind):
    import urllib.request, urllib.error
    url = f'https://pypi.org/pypi/{name}/json' if kind == 'py' else f'https://registry.npmjs.org/{name.replace("/", "%2F")}'
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'code-verifier'}), timeout=6):
            return 'exists on the registry (so it is real but not declared in this project)'
    except urllib.error.HTTPError as e:
        return 'NOT on the registry: almost certainly invented' if e.code == 404 else None
    except Exception:
        return None

def import_findings(t, shallow=False, online=False, low=False):
    a, n1 = py_import_findings(t, deep=not shallow)
    b, n2 = js_import_findings(t)
    res = a + b
    if online:
        res2 = []
        for f in res:
            if f.rule == 'IMPORT-UNRESOLVED':
                m = re.match(r"'([^']+)'", f.msg)
                if m:
                    nm = m.group(1).split('.')[0] if f.path.endswith('.py') else ('/'.join(m.group(1).split('/')[:2]) if m.group(1).startswith('@') else m.group(1).split('/')[0])
                    note = registry_note(nm, 'py' if f.path.endswith('.py') else 'js')
                    if note:
                        f = f._replace(msg=f.msg + ' | ' + note)
            res2.append(f)
        res = res2
    if not low:
        res = [f for f in res if f.sev != 'low']
    return sorted(set(res), key=lambda x: ({'high': 0, 'medium': 1, 'low': 2}[x.sev], x.path, x.line)), n1 + n2

def cmd_imports(a):
    t = get_targets(a.paths, a.base)
    res, n = import_findings(t, a.shallow, a.online, a.low)
    print(f'imports: {n} checked, {len(res)} problems in {len(t.files)} files ({t.mode})')
    for f in res[:40]:
        print(f'  {f.sev.upper():6} {f.path}:{f.line}  {f.rule}  {f.msg}' + (f'  -> {f.fix}' if f.fix else ''))
    return 1 if any(f.sev == 'high' for f in res) else 0

# ---------------------------------------------------------------- tamper

ASSERT_RE = re.compile(r'\bassert\w*\b|\bexpect\s*\(|\.should\b|\.to(?:Be|Equal|Have|Match|Throw|Contain|Strict)\w*|\bself\.assert\w+|\bt\.(?:is|equal|deepEqual|true|truthy|throws)\b')
SKIP_RE = re.compile(r'@pytest\.mark\.(?:skip|xfail)|\bpytest\.(?:skip|xfail)\s*\(|@unittest\.(?:skip|expectedFailure)|\b(?:it|test|describe)\.(?:skip|todo|only)\b|\bx(?:it|describe|test)\s*\(|\bt\.Skip\(|\b(?:fit|fdescribe)\s*\(|\.only\s*\(')
TESTDEF_RE = re.compile(r'^\s*(?:async\s+)?def\s+(test_\w+)|^\s*(?:it|test)\s*\(\s*([\'"`])(.+?)\2')
CONFIG_FILES = re.compile(r'(?i)(^|/)(pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|jest\.config\.[cm]?[jt]s|vitest\.config\.[cm]?[jt]s|\.mocharc[^/]*|package\.json|\.github/workflows/[^/]+\.ya?ml|Makefile)$')
LOOSEN_RE = re.compile(r'(?i)skip|ignore|deselect|exclude|threshold|fail[_-]under|passWithNoTests|maxfail|xfail|continue-on-error|\|\|\s*true|exit\s+0|--bail')

def parse_diff(diff):
    files, cur = {}, None
    for line in diff.split('\n'):
        if line.startswith('diff --git '):
            m = re.match(r'diff --git a/(.*) b/(.*)$', line)
            cur = files.setdefault(m.group(2), {'deleted': False, 'new': False, 'hunks': []}) if m else None
        elif cur is None:
            continue
        elif line.startswith('deleted file mode'):
            cur['deleted'] = True
        elif line.startswith('new file mode'):
            cur['new'] = True
        elif line.startswith('@@'):
            cur['hunks'].append(([], []))
        elif cur['hunks'] and line.startswith('-') and not line.startswith('---'):
            cur['hunks'][-1][0].append(line[1:])
        elif cur['hunks'] and line.startswith('+') and not line.startswith('+++'):
            cur['hunks'][-1][1].append(line[1:])
    return files

def test_literals(root):
    strs, nums, n = set(), set(), 0
    for f in walk_files(root):
        if not is_test_path(os.path.relpath(f, root)) or os.path.splitext(f)[1] not in CODE_EXT:
            continue
        n += 1
        if n > 200:
            break
        text = read_text(f) or ''
        strs |= {m.group(2) for m in re.finditer(r'(["\'])([^"\'\n]{4,80})\1', text)}
        nums |= {int(m.group(1)) for m in re.finditer(r'\b(\d{4,9})\b', text)}
    return strs, nums

LIT = r'''-?\d+(?:\.\d+)?|"[^"]*"|'[^']*\''''

def parse_lit(s):
    s = s.strip()
    if re.fullmatch(r'-?\d+', s):
        return int(s)
    if re.fullmatch(r'-?\d+\.\d+', s):
        return float(s)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in '"\'':
        return s[1:-1]
    return None

def test_calls(root, name):
    """positional literal args -> expected literal, gathered from `name(args) == expected`
    or `assert name(args), expected` / `== expected` in nearby lines, across test files."""
    out, n = [], 0
    pat = re.compile(re.escape(name) + r'\s*\(([^()]*)\)')
    for f in walk_files(root):
        if not is_test_path(os.path.relpath(f, root)) or os.path.splitext(f)[1] not in CODE_EXT:
            continue
        n += 1
        if n > 200:
            break
        text = read_text(f) or ''
        for m in pat.finditer(text):
            tail = text[m.end():m.end() + 60]
            em = re.match(r'\s*(?:\)\s*)?==\s*(' + LIT + r')', tail) or re.match(r'\s*,\s*(' + LIT + r')\s*\)', tail)
            if not em:
                continue
            expected = parse_lit(em.group(1))
            if expected is None:
                continue
            args = []
            depth = 0
            for part in m.group(1).split(','):
                args.append(part)
            args = [a for a in args if a.strip() != '']
            parsed = [parse_lit(a) for a in args]
            if any(a is None for a in parsed) and any(x.strip() for x in args):
                continue
            out.append((tuple(parsed), expected))
    return out

class FuncParent(ast.NodeVisitor):
    """maps each If node id -> (enclosing function name, {param_name: position})"""
    def __init__(self):
        self.map = {}
        self.stack = []

    def visit_FunctionDef(self, node):
        params = [a.arg for a in node.args.args]
        self.stack.append((node.name, {p: i for i, p in enumerate(params)}))
        self.generic_visit(node)
        self.stack.pop()
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node):
        if self.stack:
            self.map[id(node)] = self.stack[-1]
        self.generic_visit(node)

def special_casing(t):
    """`if x == <literal used by a test>: return <literal used by a test>` in non-test code"""
    out = []
    cand = [(p, a) for p, a in t.files.items() if os.path.splitext(p)[1] in CODE_EXT and not is_test_path(rel(t, p))]
    if not cand:
        return out
    strs, nums = test_literals(t.root)
    def lit_ok(v, is_input):
        if isinstance(v, str):
            return len(v) >= 4 and v in strs
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v) in nums if is_input else (int(v) in nums or str(v) in strs)
        return False
    for p, added in cand:
        text = read_text(p)
        if text is None:
            continue
        rp = rel(t, p)
        if p.endswith('.py'):
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            def eq_pairs(test):
                """a Compare(==) or an And of such: yields (param_name_or_None, constant_node) per leg"""
                if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
                    left, right = test.left, test.comparators[0]
                    if isinstance(right, ast.Constant):
                        return [(left.id if isinstance(left, ast.Name) else None, right)]
                    if isinstance(left, ast.Constant):
                        return [(right.id if isinstance(right, ast.Name) else None, left)]
                    return []
                if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
                    out_, ok = [], True
                    for v in test.values:
                        c = eq_pairs(v)
                        if not c:
                            ok = False; break
                        out_ += c
                    return out_ if ok else []
                return []
            fp = FuncParent(); fp.visit(tree)
            call_cache = {}
            for n in ast.walk(tree):
                if isinstance(n, ast.If) and n.body and isinstance(n.body[0], ast.Return) and isinstance(n.body[0].value, ast.Constant) \
                        and (added is None or n.lineno in added):
                    pairs = eq_pairs(n.test)
                    if not pairs or id(n) not in fp.map:
                        continue
                    fname, param_pos = fp.map[id(n)]
                    pinned = {param_pos[nm]: c.value for nm, c in pairs if nm in param_pos}
                    if not pinned:
                        continue
                    ret = n.body[0].value.value
                    if fname not in call_cache:
                        call_cache[fname] = test_calls(t.root, fname)
                    matched = any(all(pos < len(args) and args[pos] == val for pos, val in pinned.items()) and expected == ret
                                  for args, expected in call_cache[fname])
                    if matched:
                        n_pin = len(pinned)
                        out.append((rp, n.lineno, f'returns a literal matching a test\'s exact call to {fname}() ({n_pin} arg{"s" if n_pin != 1 else ""} pinned) (special-cased to pass?)'))
        else:
            for i, line in enumerate(text.split('\n'), 1):
                m = re.search(r'if\s*\([^)]*?===?\s*([\'"][^\'"]{4,80}[\'"]|\d{4,9})\s*\)\s*(?:\{\s*)?return\s+([\'"][^\'"]*[\'"]|\d+)', line)
                if m and (added is None or i in added):
                    a_, b_ = m.group(1).strip('\'"'), m.group(2).strip('\'"')
                    if (a_ in strs or (a_.isdigit() and int(a_) in nums)) and (b_ in strs or (b_.isdigit() and (int(b_) in nums or b_ in strs))):
                        out.append((rp, i, 'returns a literal for one specific input that a test also uses (special-cased to pass?)'))
    return out

def tamper_findings(t):
    if t.base is None and not repo_root(os.getcwd()):
        return None, 'not a git repository: cannot compare with the original tests'
    if t.base is None:
        return [], 'no commit to compare with'
    diff = git('diff', '-U0', '--no-renames', t.base, cwd=t.root) or ''
    files = parse_diff(diff)
    res = []
    for path, info in sorted(files.items()):
        if is_test_path(path):
            removed = [l for h in info['hunks'] for l in h[0]]
            added = [l for h in info['hunks'] for l in h[1]]
            if info['deleted']:
                names = [m.group(1) or m.group(3) for l in removed for m in [TESTDEF_RE.match(l)] if m]
                res.append((path, 0, f'test file deleted ({len(names)} tests)'))
                continue
            if not info['new']:
                ra, aa = sum(bool(ASSERT_RE.search(l)) for l in removed), sum(bool(ASSERT_RE.search(l)) for l in added)
                if ra > aa:
                    res.append((path, 0, f'{ra - aa} assertion(s) removed net ({ra} removed, {aa} added)'))
                rn = [m.group(1) or m.group(3) for l in removed for m in [TESTDEF_RE.match(l)] if m]
                an = {m.group(1) or m.group(3) for l in added for m in [TESTDEF_RE.match(l)] if m}
                gone = [x for x in rn if x not in an]
                if gone:
                    res.append((path, 0, f'test(s) removed: {", ".join(gone[:4])}' + (' ...' if len(gone) > 4 else '')))
                changed = []
                for rem, add in info['hunks']:
                    for o, n_ in zip(rem, add):
                        if ASSERT_RE.search(o) and ASSERT_RE.search(n_) and o.strip() != n_.strip():
                            changed.append((o.strip()[:70], n_.strip()[:70]))
                if changed:
                    res.append((path, 0, f'{len(changed)} expectation(s) changed, e.g. `{changed[0][0]}` -> `{changed[0][1]}` (fine if the old test was wrong: say so)'))
            rem_skips = [l.strip() for l in removed if SKIP_RE.search(l)]
            new_skips = [l.strip() for l in added if SKIP_RE.search(l) and l.strip() not in rem_skips]
            if new_skips:
                res.append((path, 0, f'skip/xfail/only marker added: `{new_skips[0][:70]}`'))
        elif CONFIG_FILES.search(path):
            for h in info['hunks']:
                for l in h[1]:
                    if LOOSEN_RE.search(l) and not (path.endswith('package.json') and not re.search(r'"test|jest|vitest|mocha|coverage', l)):
                        res.append((path, 0, f'test/CI config line added that can loosen checks: `{l.strip()[:70]}`'))
                        break
    for rp, ln, msg in special_casing(t):
        res.append((rp, ln, msg))
    return res, ''

def cmd_tamper(a):
    t = get_targets([], a.base)
    res, note = tamper_findings(t)
    if res is None:
        print(f'tamper: NOT RUN ({note})')
        return 0
    print(f'tamper: {len(res)} to review (base {t.base})' if res else f'tamper: OK, no test weakening found (base {t.base}){" - " + note if note else ""}')
    for path, ln, msg in res[:30]:
        print(f'  REVIEW {path}{":" + str(ln) if ln else ""}  {msg}')
    return 0

# ---------------------------------------------------------------- run project checks

def run_cmd(argv, cwd, timeout):
    env = dict(os.environ, CI='1', NO_COLOR='1', FORCE_COLOR='0', TERM='dumb', PYTHONUNBUFFERED='1')
    t0 = time.time()
    try:
        p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True, errors='replace',
                             start_new_session=hasattr(os, 'killpg'))
    except OSError as e:
        return 'nostart', str(e), 0.0
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            p.kill()
        out, _ = p.communicate()
        return 'timeout', out or '', time.time() - t0
    return p.returncode, out or '', time.time() - t0

def parse_counts(out):
    lines = out.strip().split('\n')
    c = {}
    for l in reversed(lines[-80:]):
        pairs = re.findall(r'(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|deselected|todo)\b', l)
        if pairs and re.search(r'passed|failed|error|skipped|no tests ran', l):
            for n, k in pairs:
                c['error' if k.startswith('error') else k] = c.get('error' if k.startswith('error') else k, 0) + int(n)
            break
    tail = '\n'.join(lines[-80:])
    m = re.search(r'(\d+)\s+passing', tail)
    if m and not c:
        c['passed'] = int(m.group(1))
        f = re.search(r'(\d+)\s+failing', tail); p_ = re.search(r'(\d+)\s+pending', tail)
        c['failed'] = int(f.group(1)) if f else 0
        c['skipped'] = int(p_.group(1)) if p_ else 0
    if not c:
        nt = {k: int(v) for k, v in re.findall(r'(?:ℹ|#)\s*(tests|pass|fail|skipped|cancelled)\s+(\d+)', tail)}
        if 'tests' in nt:
            c = {'passed': nt.get('pass', 0), 'failed': nt.get('fail', 0) + nt.get('cancelled', 0), 'skipped': nt.get('skipped', 0)}
    if not c:
        m = re.search(r'Ran (\d+) tests? in', tail)
        if m:
            total = int(m.group(1))
            fl = re.search(r'FAILED \(([^)]*)\)', tail)
            f = e = 0
            if fl:
                f = int((re.search(r'failures=(\d+)', fl.group(1)) or [0, 0])[1]); e = int((re.search(r'errors=(\d+)', fl.group(1)) or [0, 0])[1])
            sk = re.search(r'skipped=(\d+)', tail)
            s = int(sk.group(1)) if sk else 0
            c = {'passed': max(total - f - e - s, 0), 'failed': f + e, 'skipped': s}
    return c

def key_lines(out, n=25):
    lines = [l for l in out.split('\n') if l.strip()]
    hits = [l for l in lines if re.search(r'FAILED|\bFAIL\b|✗|✕|✖|not ok|Error:|AssertionError|Traceback|●|^E\s|^>\s', l)]
    pick = hits[:n] if hits else lines[-n:]
    return [l[:220] for l in pick]

def judge_tests(label, rc, out, secs):
    if rc == 'nostart':
        return 'NOT RUN', f'{label}: could not start ({out})', []
    if rc == 'timeout':
        return 'FAIL', f'{label}: timed out after {int(secs)}s', key_lines(out, 8)
    c = parse_counts(out)
    p, f, e, s = c.get('passed', 0), c.get('failed', 0), c.get('error', 0), c.get('skipped', 0)
    bits = f'{p} passed, {f + e} failed, {s} skipped' if c else f'exit {rc}, count not parsed'
    if rc == 5 or re.search(r'no tests (?:ran|found|collected)|No test files found', out, re.I) and p == 0:
        return 'FAIL', f'{label}: no tests were collected (a run with zero tests is not a pass)', key_lines(out, 6)
    if rc != 0:
        return 'FAIL', f'{label}: {bits}', key_lines(out)
    if c and p == 0 and f == 0 and e == 0:
        return 'FAIL', f'{label}: 0 tests ran' + (f' ({s} skipped)' if s else ''), key_lines(out, 6)
    return 'OK', f'{label}: {bits} ({secs:.1f}s)', []

def has_marker(proj, *names):
    return any(os.path.exists(os.path.join(proj, n)) for n in names)

def py_exe(proj):
    for d in ('.venv', 'venv', 'env'):
        for sub in ('bin/python', 'Scripts/python.exe'):
            p = os.path.join(proj, d, sub)
            if os.path.isfile(p):
                return p
    return sys.executable

def find_tool(proj, name):
    for d in ('.venv', 'venv', 'env'):
        for sub in ('bin', 'Scripts'):
            p = os.path.join(proj, d, sub, name)
            if os.path.isfile(p):
                return [p]
    w = shutil.which(name)
    if w:
        return [w]
    r = subprocess.run([py_exe(proj), '-m', name, '--version'], capture_output=True, text=True)
    return [py_exe(proj), '-m', name] if r.returncode == 0 else None

def toml_has(proj, section):
    for fn in ('pyproject.toml',):
        p = os.path.join(proj, fn)
        if os.path.isfile(p) and re.search(r'(?m)^\s*\[' + re.escape(section), read_text(p) or ''):
            return True
    return False

def detect_python(proj, only, t, timeout):
    res = []
    pyfiles = [os.path.relpath(p, proj) for p in t.files if p.endswith('.py') and os.path.isfile(p)]
    if only in (None, 'tests'):
        tests_here = any(os.path.isdir(os.path.join(proj, d)) for d in ('tests', 'test')) or has_marker(proj, 'pytest.ini', 'conftest.py') \
            or toml_has(proj, 'tool.pytest') or any(re.search(r'(^|/)(test_[^/]*|[^/]*_test)\.py$', norm(os.path.relpath(f, proj))) for f in glob.glob(os.path.join(proj, '*.py')) + glob.glob(os.path.join(proj, '*', '*.py')))
        if not tests_here:
            res.append(('tests', 'NOT RUN', 'no python tests found (no tests/ dir, pytest config or test_*.py)', []))
        else:
            pt = find_tool(proj, 'pytest')
            if pt:
                rc, out, secs = run_cmd(pt + ['-q', '-p', 'no:cacheprovider'], proj, timeout)
                st, det, ex = judge_tests('pytest', rc, out, secs)
            else:
                rc, out, secs = run_cmd([py_exe(proj), '-m', 'unittest', 'discover'], proj, timeout)
                st, det, ex = judge_tests('unittest', rc, out, secs)
            res.append(('tests', st, det, ex))
    if only in (None, 'lint'):
        if not pyfiles and t.mode.startswith('git'):
            res.append(('lint', 'n/a', 'no python files changed', []))
        else:
            ruff, flake = find_tool(proj, 'ruff'), find_tool(proj, 'flake8')
            targets = pyfiles or ['.']
            cfg = has_marker(proj, 'ruff.toml', '.ruff.toml') or toml_has(proj, 'tool.ruff')
            if ruff:
                cmd = ruff + ['check', '--no-cache'] + ([] if cfg else ['--select', 'E9,F63,F7,F82']) + targets
                rc, out, secs = run_cmd(cmd, proj, timeout)
                res.append(('lint', 'OK' if rc == 0 else 'FAIL', f'ruff{"" if cfg else " (E9,F63,F7,F82: syntax + undefined names)"}' + ('' if rc == 0 else f': exit {rc}'), [] if rc == 0 else key_lines(out, 15)))
            elif flake:
                rc, out, secs = run_cmd(flake + ['--select=E9,F63,F7,F82'] + targets, proj, timeout)
                res.append(('lint', 'OK' if rc == 0 else 'FAIL', 'flake8 (E9,F63,F7,F82)' + ('' if rc == 0 else f': exit {rc}'), [] if rc == 0 else key_lines(out, 15)))
            else:
                res.append(('lint', 'NOT RUN', 'no ruff/flake8 available (pip install ruff to catch undefined names)', []))
    if only in (None, 'types'):
        cfg = has_marker(proj, 'mypy.ini', '.mypy.ini') or toml_has(proj, 'tool.mypy') or (os.path.isfile(os.path.join(proj, 'setup.cfg')) and '[mypy' in (read_text(os.path.join(proj, 'setup.cfg')) or ''))
        if cfg:
            my = find_tool(proj, 'mypy')
            if my:
                rc, out, secs = run_cmd(my + (pyfiles or ['.']), proj, timeout)
                res.append(('types', 'OK' if rc == 0 else 'FAIL', 'mypy' + ('' if rc == 0 else f': exit {rc}'), [] if rc == 0 else key_lines(out, 15)))
            else:
                res.append(('types', 'NOT RUN', 'mypy configured but not installed', []))
    return res

def detect_js(proj, only, t, timeout):
    res = []
    pj = load_pkg(proj) or {}
    scripts = pj.get('scripts') or {}
    pm = 'pnpm' if has_marker(proj, 'pnpm-lock.yaml') else 'yarn' if has_marker(proj, 'yarn.lock') else 'bun' if has_marker(proj, 'bun.lockb', 'bun.lock') else 'npm'
    pmbin = shutil.which(pm)
    have_nm = os.path.isdir(os.path.join(proj, 'node_modules'))
    def script(kind, names):
        for n in names:
            if n in scripts and not re.search(r'no test specified', scripts[n]):
                return n
        return None
    def exec_script(name):
        if not pmbin:
            return None, f'{pm} not installed'
        if not have_nm and (pj.get('dependencies') or pj.get('devDependencies')):
            return None, f'node_modules missing: run `{pm} install` first'
        return [pmbin, 'run', name] + (['--silent'] if pm == 'npm' else []), ''
    if only in (None, 'tests'):
        s = script('tests', ['test'])
        if not s:
            res.append(('tests', 'NOT RUN', 'no "test" script in package.json', []))
        else:
            cmd, why = exec_script(s)
            if not cmd:
                res.append(('tests', 'NOT RUN', why, []))
            else:
                rc, out, secs = run_cmd(cmd, proj, timeout)
                st, det, ex = judge_tests(f'{pm} test', rc, out, secs)
                res.append(('tests', st, det, ex))
    if only in (None, 'lint'):
        s = script('lint', ['lint'])
        if s:
            cmd, why = exec_script(s)
            if not cmd:
                res.append(('lint', 'NOT RUN', why, []))
            else:
                rc, out, secs = run_cmd(cmd, proj, timeout)
                res.append(('lint', 'OK' if rc == 0 else 'FAIL', f'{pm} run {s}' + ('' if rc == 0 else f': exit {rc}'), [] if rc == 0 else key_lines(out, 15)))
    if only in (None, 'types'):
        s = script('types', ['typecheck', 'type-check', 'check-types', 'tsc', 'types'])
        tsc = os.path.join(proj, 'node_modules', '.bin', 'tsc')
        if s:
            cmd, why = exec_script(s)
        elif has_marker(proj, 'tsconfig.json') and os.path.isfile(tsc):
            cmd, why = [tsc, '--noEmit'], ''
            s = 'tsc --noEmit'
        else:
            cmd, why, s = None, '', None
        if s:
            if not cmd:
                res.append(('types', 'NOT RUN', why, []))
            else:
                rc, out, secs = run_cmd(cmd, proj, timeout)
                res.append(('types', 'OK' if rc == 0 else 'FAIL', s + ('' if rc == 0 else f': exit {rc}'), [] if rc == 0 else key_lines(out, 15)))
    return res

def run_checks(t, only=None, timeout=300):
    cwd = os.getcwd()
    proj = cwd if has_marker(cwd, 'package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'pytest.ini', 'tox.ini', 'requirements.txt') else t.root
    js = has_marker(proj, 'package.json')
    # Python detection never gates on the diffed file list (t.files can be empty with no diff,
    # e.g. checking an already-committed repo) - detect_python has its own, thorough "no tests found" check.
    res = []
    if js:
        res += detect_js(proj, only, t, timeout)
    res += detect_python(proj, only, t, timeout)
    if not res and only in (None, 'tests'):
        res.append(('tests', 'NOT RUN', 'no test setup recognised (no package.json test script, no python tests)', []))
    return res

def cmd_run(a):
    t = get_targets(a.paths, a.base)
    res = run_checks(t, a.only, a.timeout)
    for kind, st, det, ex in res:
        print(f'{kind:6} {st:8} {det}')
        for l in ex:
            print(f'         | {l}')
    return 1 if any(r[1] == 'FAIL' for r in res) else 0

# ---------------------------------------------------------------- check (everything) + receipt

def cmd_check(a):
    t0 = time.time()
    t = get_targets(a.paths, a.base)
    rows, details = [], []
    n_py = sum(p.endswith('.py') for p in t.files)
    n_js = sum(os.path.splitext(p)[1] in JS_EXT for p in t.files)
    ok, bad, skipped = syntax_check(t)
    if not t.files:
        rows.append(('syntax', 'n/a', 'no changed code files'))
    elif bad:
        rows.append(('syntax', 'FAIL', f'{len(bad)} file(s) do not parse'))
        details += [f'  SYNTAX {b}' for b in bad[:10]]
    else:
        rows.append(('syntax', 'OK', f'{ok} file(s) parsed' + (f', {skipped} not parseable here (ts/jsx/binary)' if skipped else '')))
    imp, n_imp = import_findings(t, a.shallow, a.online)
    hi = [f for f in imp if f.sev == 'high']
    if hi:
        rows.append(('imports', 'FAIL', f'{len(hi)} unresolved of {n_imp} checked'))
        details += [f'  IMPORT {f.path}:{f.line}  {f.msg}' + (f'  -> {f.fix}' if f.fix else '') for f in hi[:12]]
    else:
        rows.append(('imports', 'OK' if n_imp else 'n/a', f'{n_imp} import(s) resolved' if n_imp else 'no imports in changed lines'))
    findings, notes = scan_files(t, low=False, all_lines=False)
    fh, fm = [f for f in findings if f.sev == 'high'], [f for f in findings if f.sev == 'medium']
    if fh:
        rows.append(('scan', 'FAIL', f'{len(fh)} high, {len(fm)} medium open'))
    elif fm:
        rows.append(('scan', 'REVIEW', f'{len(fm)} medium open'))
    else:
        rows.append(('scan', 'OK' if t.files else 'n/a', f'0 findings in {len(t.files)} file(s)' if t.files else 'nothing to scan'))
    details += [f'  {f.sev.upper():6} {f.path}:{f.line}  {f.rule}  {f.msg}  -> {f.fix}' for f in findings[:25]]
    tm, tnote = tamper_findings(t)
    if tm is None:
        rows.append(('tamper', 'NOT RUN', tnote))
    elif tm:
        rows.append(('tamper', 'REVIEW', f'{len(tm)} test-weakening signal(s): explain each in the reply'))
        details += [f'  REVIEW {p}{":" + str(l) if l else ""}  {m}' for p, l, m in tm[:15]]
    else:
        rows.append(('tamper', 'OK', 'no test weakening in the diff' + (f' ({tnote})' if tnote else '')))
    if bad:
        rows += [('tests', 'NOT RUN', 'skipped because of syntax errors'), ]
        runres = []
    else:
        runres = run_checks(t, None, a.timeout)
    for kind, st, det, ex in runres:
        rows.append((kind, st, det))
        if st == 'FAIL':
            details += [f'  {kind.upper()} | {l}' for l in ex[:15]]
    fails = [r[0] for r in rows if r[1] == 'FAIL']
    reviews = [r[0] for r in rows if r[1] == 'REVIEW']
    tests_row = next((r for r in rows if r[0] == 'tests'), None)
    notrun = [r[0] for r in rows if r[1] == 'NOT RUN']
    if fails:
        verdict = 'FAIL - ' + ', '.join(fails)
    elif reviews:
        verdict = 'REVIEW - ' + ', '.join(reviews) + ' need a written explanation'
    elif tests_row is None or tests_row[1] != 'OK':
        verdict = 'PARTIAL - tests did not run, so correctness is NOT verified'
    else:
        verdict = 'PASS' + (f' (not run: {", ".join(notrun)})' if notrun else '')
    if details:
        print('details:')
        print('\n'.join(details))
    print(f'code-verifier receipt | {len(t.files)} file(s) (py {n_py}, js/ts {n_js}) | {t.mode} | {time.strftime("%H:%M:%S")} | {time.time() - t0:.0f}s')
    for k, st, det in rows:
        print(f'  {k:8} {st:8} {det}')
    print(f'VERDICT: {verdict}')
    if fails:
        return 1
    return 1 if a.strict and not verdict.startswith('PASS') else 0

def main():
    ap = argparse.ArgumentParser(prog='vf.py')
    sub = ap.add_subparsers(dest='cmd', required=True)
    def common(p):
        p.add_argument('paths', nargs='*')
        p.add_argument('--base', default='HEAD')
    c = sub.add_parser('check'); common(c); c.add_argument('--timeout', type=int, default=300); c.add_argument('--strict', action='store_true')
    c.add_argument('--shallow', action='store_true'); c.add_argument('--online', action='store_true'); c.set_defaults(fn=cmd_check)
    s = sub.add_parser('scan'); common(s); s.add_argument('--all-lines', action='store_true'); s.add_argument('--low', action='store_true'); s.set_defaults(fn=cmd_scan)
    i = sub.add_parser('imports'); common(i); i.add_argument('--shallow', action='store_true'); i.add_argument('--online', action='store_true'); i.add_argument('--low', action='store_true'); i.set_defaults(fn=cmd_imports)
    tm = sub.add_parser('tamper'); tm.add_argument('--base', default='HEAD'); tm.set_defaults(fn=cmd_tamper)
    r = sub.add_parser('run'); common(r); r.add_argument('--only', choices=['tests', 'lint', 'types']); r.add_argument('--timeout', type=int, default=300); r.set_defaults(fn=cmd_run)
    a = ap.parse_args()
    sys.exit(a.fn(a))

if __name__ == '__main__':
    if hasattr(signal, 'SIGPIPE'):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    main()
