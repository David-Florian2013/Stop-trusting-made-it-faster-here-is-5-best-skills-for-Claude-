#!/usr/bin/env python3
"""context-saver: cheaper views of files and command output, with a savings ledger.

  cs.py outline FILE
  cs.py peek FILE (--symbol NAME | --grep REGEX | --lines A-B) [--context N] [-i]
  cs.py squeeze [--head N] [--tail N] [--min-lines N] [--keep REGEX] < output
  cs.py report [--reset]
"""
import argparse, ast, difflib, hashlib, json, os, re, signal, sys, tempfile, time

if hasattr(signal, 'SIGPIPE'):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

def tok(s):
    return (len(s) + 3) // 4

ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
IMPORTANT = re.compile(r'error|fail|exception|traceback|fatal|panic|assert|denied|not found|cannot|unable|warn|'
                       r'exit (?:code|status)[: ]*[1-9]|non-?zero|abort|killed|timed? ?out|segmentation|out of memory|refused|invalid|unexpected', re.I)
REPORT_MIN = 100  # report stays silent unless net savings reach this many tokens

def state_dir():
    d = os.path.join(tempfile.gettempdir(), 'context-saver')
    os.makedirs(d, exist_ok=True)
    return d

def ledger_path():
    p = os.environ.get('CS_LEDGER')
    if p:
        return p
    key = hashlib.sha1(os.getcwd().encode()).hexdigest()[:10]
    return os.path.join(state_dir(), key + '.jsonl')

def file_key(path):
    """Same source -> same key, so reading it three ways is counted against ONE full read."""
    ap = os.path.abspath(path)
    base = os.path.basename(ap)
    if os.path.dirname(ap) == state_dir() and base.startswith('raw-'):
        return 's:' + base.rsplit('-', 1)[-1].split('.')[0]  # a saved squeeze output shares the squeeze's key
    try:
        st = os.stat(ap)
        return f'f:{ap}:{st.st_mtime_ns}:{st.st_size}'
    except OSError:
        return 'f:' + ap

def record(kind, key, raw_tokens, shown_tokens):
    row = json.dumps({'k': kind, 'key': key, 'raw': raw_tokens, 'shown': shown_tokens}) + '\n'
    with open(ledger_path(), 'a') as f:
        f.write(row)

def read_text(path):
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as e:
        sys.exit(f'cs: cannot read {path}: {e.strerror}')
    if b'\x00' in data[:8192]:
        sys.exit(f'cs: {path} looks binary; not shown')
    return data.decode('utf-8', errors='replace')

def split_lines(text):
    return text.replace('\r\n', '\n').split('\n')

MAX_WIDTH = 400

def clip(l, width):
    if width and len(l) > width:
        return l[:width] + f' ...[+{len(l) - width} chars; --max-width 0 shows all]'
    return l

def numbered(lines, start, width=MAX_WIDTH):
    w = len(str(start + len(lines)))
    return '\n'.join(f'{start + i:>{w}}| {clip(l, width)}' for i, l in enumerate(lines))

# ---------- symbol location ----------

def py_symbols(text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    out = []
    def walk(node, prefix, depth):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                out.append((prefix + ch.name, ch.name, start, ch.end_lineno, depth, ch))
                walk(ch, prefix + ch.name + '.', depth + 1)
            else:
                walk(ch, prefix, depth)
    walk(tree, '', 0)
    return out

DEF_WORDS = r'(?:function\*?|def|class|func|fn|const|let|var|interface|type|struct|enum|impl|trait|sub|proc|object|module)'

def generic_def_lines(lines, name):
    n = re.escape(name)
    pats = [
        re.compile(rf'^\s*(?:export\s+|pub(?:\([a-z]+\))?\s+|default\s+|async\s+|abstract\s+|static\s+|public\s+|private\s+|protected\s+|final\s+|unsafe\s+|extern\s+)*{DEF_WORDS}\s+(?:\([^)]*\)\s*)?{n}\b'),
        re.compile(rf'^\s*(?:(?:public|private|protected|static|async|final|override|get|set)\s+)*{n}\s*(?:<[^>]*>)?\s*\([^;]*$'),
    ]
    typed = re.compile(rf'^\s*(?:[A-Za-z_][\w<>\[\],.?*&:]*\s+)+{n}\s*\([^;]*$')  # Java/C#/C++/Kotlin: "int helper(...) {"
    not_call = re.compile(r'^\s*(?:return|if|else|for|while|switch|catch|new|throw|await|yield|case|do|try|delete|typeof)\b')
    def has_body(i):
        return any('{' in lines[k] for k in range(i, min(len(lines), i + 4)))
    hits = []
    for i, l in enumerate(lines):
        if pats[0].search(l):
            hits.append(i)
        elif (pats[1].search(l) or (typed.search(l) and not not_call.match(l))) and has_body(i):
            hits.append(i)
    return hits

REGEX_PREV = set('(=:[!&|?{};,+-*%<>~^') 

def brace_extent(lines, start):
    depth, seen, in_block, in_tpl, paren = 0, False, False, False, 0
    for i in range(start, min(len(lines), start + 3000)):
        s, j, n = lines[i], 0, len(lines[i])
        while j < n:
            c = s[j]
            if in_block:
                if s.startswith('*/', j):
                    in_block = False; j += 2; continue
                j += 1; continue
            if in_tpl:
                if c == '\\':
                    j += 2; continue
                if c == '`':
                    in_tpl = False
                j += 1; continue
            if s.startswith('//', j):
                break
            if s.startswith('/*', j):
                in_block = True; j += 2; continue
            if c == '`':
                in_tpl = True; j += 1; continue
            if c in '"\'':
                k = j + 1
                while k < n and s[k] != c:
                    k += 2 if s[k] == '\\' else 1
                if k < n:          # closed on this line: it was a string, skip it
                    j = k + 1; continue
                j += 1; continue   # never closed: apostrophe or regex fragment, not a string
            if c == '/':
                prev = s[:j].rstrip()[-1:] 
                if prev == '' or prev in REGEX_PREV:   # regex literal: skip to its closing slash
                    k, cls = j + 1, False
                    while k < n:
                        if s[k] == '\\': k += 2; continue
                        if s[k] == '[': cls = True
                        elif s[k] == ']': cls = False
                        elif s[k] == '/' and not cls: break
                        k += 1
                    if k < n:
                        j = k + 1; continue
            if c == '(':
                paren += 1
            elif c == ')':
                paren = max(0, paren - 1)
            elif paren == 0 and c == '{':      # braces inside (...) are default values or arguments, not the body
                depth += 1; seen = True
            elif paren == 0 and c == '}':
                depth -= 1
                if seen and depth == 0:
                    return i
            j += 1
        if not seen and i - start > 6:
            return None
    return None

def indent_extent(lines, start):
    def ind(s):
        return len(s) - len(s.lstrip())
    base = ind(lines[start])
    end = start
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == '':
            continue
        if ind(lines[i]) > base:
            end = i
        else:
            break
    return end

def find_symbol(text, lines, name, path):
    """return list of (start_idx, end_idx, note) 0-based inclusive"""
    results = []
    if path.endswith('.py'):
        syms = py_symbols(text)
        if syms is not None:
            for qual, short, s, e, d, node in syms:
                if name == qual or name == short:
                    results.append((s - 1, e - 1, ''))
            names = sorted({q for q, *_ in syms})
            return results, names
    hits = generic_def_lines(lines, name)
    for h in hits:
        if lines[h].rstrip().endswith(':') or re.match(r'^\s*(async\s+)?(def|class)\s', lines[h]):
            results.append((h, indent_extent(lines, h), ''))
            continue
        e = brace_extent(lines, h)
        if e is None:
            results.append((h, min(len(lines) - 1, h + 40), 'block end not detected, showing 40 lines'))
        else:
            results.append((h, e, ''))
    names = sorted(set(re.findall(rf'{DEF_WORDS}\s+([A-Za-z_$][\w$]*)', text)))
    return results, names

# ---------- outline ----------

OUTLINE_RE = re.compile(
    r'^\s*(?:export\s+|pub(?:\([a-z]+\))?\s+|default\s+|async\s+|abstract\s+|public\s+|private\s+|protected\s+|static\s+)*'
    r'(?:function\*?\s+[\w$]+|class\s+\w+|interface\s+\w+|type\s+\w+\s*=|enum\s+\w+|struct\s+\w+|impl\b.*|trait\s+\w+|'
    r'func\s+(?:\([^)]*\)\s*)?\w+|fn\s+\w+|(?:const|let|var)\s+[\w$]+\s*=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*=>)')

def cmd_outline(a):
    text = read_text(a.file)
    lines = split_lines(text)
    rows = []
    if a.file.endswith('.py'):
        syms = py_symbols(text)
        if syms is not None:
            for q, short, s, e, d, node in syms:
                first = lines[node.lineno - 1].strip()
                rows.append(f'{"  " * d}{node.lineno}-{e}  {first[:100]}')
    if not rows and a.file.endswith(('.md', '.markdown')):
        for i, l in enumerate(lines, 1):
            if re.match(r'^#{1,6}\s', l):
                rows.append(f'{i}  {l[:100]}')
    if not rows:
        for i, l in enumerate(lines, 1):
            if OUTLINE_RE.match(l):
                rows.append(f'{i}  {l.strip()[:100]}')
    if not rows:
        print(f'# {a.file}: {len(lines)} lines, no outline for this file type; use peek --grep')
        return
    extra = ''
    if len(rows) > 300:
        extra = f'\n... +{len(rows) - 300} more entries'
        rows = rows[:300]
    out = f'# {a.file}: {len(lines)} lines, ~{tok(text):,} tokens if read whole\n' + '\n'.join(rows) + extra
    record('outline', file_key(a.file), tok(text), tok(out))
    print(out)

# ---------- peek ----------

def merge_windows(idx, ctx, n):
    wins = []
    for i in idx:
        lo, hi = max(0, i - ctx), min(n - 1, i + ctx)
        if wins and lo <= wins[-1][1] + 1:
            wins[-1][1] = max(wins[-1][1], hi)
        else:
            wins.append([lo, hi])
    return wins

def cmd_peek(a):
    text = read_text(a.file)
    lines = split_lines(text)
    if lines and lines[-1] == '':
        lines = lines[:-1]
    total = len(lines)
    parts, label = [], ''
    if a.symbol:
        found, names = find_symbol(text, lines, a.symbol, a.file)
        if not found:
            close = difflib.get_close_matches(a.symbol, names, n=8, cutoff=0.4)
            sys.exit(f'cs: symbol {a.symbol!r} not found in {a.file}. '
                     + (f'Similar: {", ".join(close)}' if close else 'Try: cs.py outline ' + a.file))
        for s, e, note in found[:3]:
            parts.append((s, e, note))
        label = f'symbol {a.symbol}' + (f' (+{len(found) - 3} more matches)' if len(found) > 3 else '')
    elif a.lines:
        m = re.fullmatch(r'(\d+)(?:-(\d+)|\+(\d+))?', a.lines.strip())
        if not m:
            sys.exit('cs: --lines expects A, A-B or A+N')
        s = int(m.group(1))
        e = int(m.group(2)) if m.group(2) else (s + int(m.group(3)) if m.group(3) else s)
        if s < 1 or s > total:
            sys.exit(f'cs: line {s} out of range (file has {total} lines)')
        parts.append((s - 1, min(e, total) - 1, ''))
        label = f'lines {a.lines}'
    else:
        try:
            rx = re.compile(a.grep, re.I if a.i else 0)
        except re.error as ex:
            sys.exit(f'cs: bad regex: {ex}')
        idx = [i for i, l in enumerate(lines) if rx.search(l)]
        if not idx:
            sys.exit(f'cs: no line matches {a.grep!r} in {a.file}')
        cap = 20
        shown_idx = idx[:cap]
        for lo, hi in merge_windows(shown_idx, a.context, total):
            parts.append((lo, hi, ''))
        label = f'grep {a.grep!r}: {len(idx)} matching lines' + (f', showing first {cap}' if len(idx) > cap else '')
    body = []
    for s, e, note in parts:
        chunk = numbered(lines[s:e + 1], s + 1, a.max_width)
        body.append(chunk + (f'\n# note: {note}' if note else ''))
    shown_lines = sum(e - s + 1 for s, e, _ in parts)
    out = f'# {a.file} [{label}] {shown_lines} of {total} lines\n' + '\n# ...\n'.join(body)
    record('peek', file_key(a.file), tok(text), tok(out))
    print(out)

# ---------- squeeze ----------

def norm_digits(s):
    return re.sub(r'\d+', '#', s)

def cmd_squeeze(a):
    raw = sys.stdin.buffer.read().decode('utf-8', errors='replace')
    if not raw:
        return
    keep = re.compile(a.keep, re.I) if a.keep else None
    def important(l):
        return bool(IMPORTANT.search(l)) or bool(keep and keep.search(l))
    lines = []
    for l in raw.split('\n'):
        if '\r' in l:
            segs = [s for s in l.split('\r') if s.strip()]
            l = segs[-1] if segs else ''
        lines.append(ANSI.sub('', l).rstrip())
    if lines and lines[-1] == '':
        lines.pop()
    if len(lines) <= a.min_lines and max((len(l) for l in lines), default=0) <= 400:
        sys.stdout.write(raw if raw.endswith('\n') else raw + '\n')
        return
    # collapse runs into items; an item is (list of output lines, important?)
    items = []
    i = 0
    while i < len(lines):
        imp = important(lines[i])
        j = i
        while j + 1 < len(lines):
            nxt = lines[j + 1]
            same = (nxt == lines[i]) if imp else (not important(nxt) and norm_digits(nxt) == norm_digits(lines[i]))
            if not same:
                break
            j += 1
        cnt = j - i + 1
        identical = all(lines[k] == lines[i] for k in range(i, j + 1))
        if identical and cnt >= 3:
            items.append(([lines[i] + f'   [x{cnt}]'], imp))
        elif not imp and cnt >= 4:
            items.append(([lines[i], f'[... {cnt - 2} similar lines, numbers differ ...]', lines[j]], False))
        else:
            for k in range(i, j + 1):
                items.append(([lines[k]], imp))
        i = j + 1
    def fmt(t, imp):
        lim = 800 if imp else 300
        return t[:lim] + f' ...[+{len(t) - lim} chars]' if len(t) > lim else t
    n = len(items)
    if n <= a.min_lines:
        keep_idx = set(range(n))
    else:
        keep_idx = set(range(min(a.head, n))) | set(range(max(0, n - a.tail), n))
        imp_idx = [k for k, c in enumerate(items) if c[1]]
        if len(imp_idx) > 60:
            imp_idx = imp_idx[:40] + imp_idx[-20:]
        for k in imp_idx:
            for d in range(-2, 3):
                if 0 <= k + d < n:
                    keep_idx.add(k + d)
    out, prev = [], -1
    for k in sorted(keep_idx):
        if k != prev + 1:
            out.append(f'... [{k - prev - 1} entries omitted] ...')
        out.extend(fmt(t, items[k][1]) for t in items[k][0])
        prev = k
    if prev != n - 1:
        out.append(f'... [{n - 1 - prev} entries omitted] ...')
    d = state_dir()
    rawfile = os.path.join(d, f'raw-{int(time.time())}-{hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:6]}.txt')
    with open(rawfile, 'w') as f:
        f.write(raw)
    olds = sorted(p for p in os.listdir(d) if p.startswith('raw-'))
    for p in olds[:-20]:
        try:
            os.remove(os.path.join(d, p))
        except OSError:
            pass
    out.append(f'[context-saver: shortened from {len(lines)} to {len(out)} lines; full output: {rawfile}]')
    text = '\n'.join(out)
    record('squeeze', 's:' + hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()[:6], tok(raw), tok(text))
    print(text)

# ---------- report ----------

def cmd_report(a):
    p = ledger_path()
    rows = []
    if os.path.exists(p):
        with open(p) as f:
            for l in f:
                try:
                    rows.append(json.loads(l))
                except ValueError:
                    pass
    by_key = {}
    for r in rows:
        k = by_key.setdefault(r['key'], {'raw': 0, 'shown': 0, 'n': 0})
        k['raw'] = max(k['raw'], r['raw'])   # a source can only be read in full once
        k['shown'] += r['shown']             # but every partial view costs tokens
        k['n'] += 1
    raw = sum(k['raw'] for k in by_key.values())
    shown = sum(k['shown'] for k in by_key.values())
    n = sum(k['n'] for k in by_key.values())
    if raw - shown >= REPORT_MIN:
        pct = round(100 * (raw - shown) / raw)
        print(f'context-saver: ~{raw - shown:,} tokens avoided ({len(by_key)} sources, {n} views: '
              f'~{raw:,} if read in full -> ~{shown:,} shown, {pct}% less; estimate = chars/4)')
    if a.reset and os.path.exists(p):
        os.remove(p)

def main():
    ap = argparse.ArgumentParser(prog='cs.py')
    sub = ap.add_subparsers(dest='cmd', required=True)
    o = sub.add_parser('outline'); o.add_argument('file'); o.set_defaults(fn=cmd_outline)
    p = sub.add_parser('peek'); p.add_argument('file')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--symbol'); g.add_argument('--grep'); g.add_argument('--lines')
    p.add_argument('--context', type=int, default=3); p.add_argument('-i', action='store_true')
    p.add_argument('--max-width', type=int, default=MAX_WIDTH)
    p.set_defaults(fn=cmd_peek)
    s = sub.add_parser('squeeze')
    s.add_argument('--head', type=int, default=10); s.add_argument('--tail', type=int, default=15)
    s.add_argument('--min-lines', type=int, default=60); s.add_argument('--keep')
    s.set_defaults(fn=cmd_squeeze)
    r = sub.add_parser('report'); r.add_argument('--reset', action='store_true'); r.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    a.fn(a)

if __name__ == '__main__':
    main()
