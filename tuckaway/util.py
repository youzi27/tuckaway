"""Small helpers: sizes, dates, path tests, globs, hashing."""

import bisect
import hashlib
import os
import re
import stat
import time

DAY = 86400.0


def human(n):
    n = float(n or 0)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return ("%.0f%s" if unit == "B" else "%.1f%s") % (n, unit)
        n /= 1024.0


def days_since(ts, now=None):
    if not ts:
        return 1e9
    return ((now or time.time()) - ts) / DAY


def fmt_date(ts, now=None):
    if not ts:
        return "-"
    lt = time.localtime(ts)
    if lt.tm_year != time.localtime(now or time.time()).tm_year:
        return time.strftime("%Y-%m-%d", lt)
    return time.strftime("%m-%d", lt)


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def under(path, base):
    """True when path is base or lies inside it."""
    base = base.rstrip("/") or "/"
    return path == base or path.startswith(base if base == "/" else base + "/")


def ancestors(path, stop="/"):
    """The parents of path, nearest first, up to and including stop."""
    out = []
    p = os.path.dirname(path)
    while p and p != path:
        out.append(p)
        if p == stop or p == "/":
            break
        path, p = p, os.path.dirname(p)
    return out


def glob_re(pattern):
    """A glob where * and ? stay inside one path component and ** may cross components."""
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append({"*": "[^/]*", "?": "[^/]"}.get(c, re.escape(c)))
        i += 1
    return re.compile("^" + "".join(out) + "$")


class Globs:
    def __init__(self, patterns):
        self.res = [glob_re(p) for p in patterns or []]

    def match(self, path):
        return any(r.match(path) for r in self.res)

    def __bool__(self):
        return bool(self.res)


def parse_expiry(text, now=None):
    """'7d', '12h', '2w' or 'YYYY-MM-DD' -> epoch seconds."""
    now = now or time.time()
    text = (text or "").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hdw])", text)
    if m:
        return now + float(m.group(1)) * {"h": 3600, "d": DAY, "w": 7 * DAY}[m.group(2)]
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%d"))
    except ValueError:
        raise ValueError("expected 7d, 12h, 2w or YYYY-MM-DD, got %r" % text)


def sha256_file(path, bufsize=4 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                return h.hexdigest()
            h.update(b)


def tree_state(path, ignore_suffixes=()):
    """(newest mtime, number of entries, bytes on disk) of a file or directory tree, without following links."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0.0, 0, 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_mtime, 1, st.st_blocks * 512
    newest, count, size = st.st_mtime, 0, st.st_blocks * 512
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    s = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                count += 1
                size += s.st_blocks * 512
                if not e.name.endswith(ignore_suffixes) and s.st_mtime > newest:
                    newest = s.st_mtime
                if stat.S_ISDIR(s.st_mode):
                    stack.append(e.path)
    return newest, count, size


class PathIndex:
    """Sorted paths with attached data; lists the entries that lie under a directory."""

    def __init__(self, pairs=()):
        self.items = sorted(pairs, key=lambda x: x[0])
        self.keys = [k for k, _ in self.items]

    def under(self, base):
        base = base.rstrip("/")
        out = []
        i = bisect.bisect_left(self.keys, base)
        if i < len(self.keys) and self.keys[i] == base:
            out.append(self.items[i])
        lo = bisect.bisect_left(self.keys, base + "/")
        hi = bisect.bisect_left(self.keys, base + "0")   # "0" follows "/" in ASCII
        out.extend(self.items[lo:hi])
        return out


def is_elf_core(path):
    try:
        with open(path, "rb") as f:
            head = f.read(18)
    except OSError:
        return False
    if len(head) < 18 or head[:4] != b"\x7fELF":
        return False
    e_type = int.from_bytes(head[16:18], "little" if head[5] == 1 else "big")
    return e_type == 4
