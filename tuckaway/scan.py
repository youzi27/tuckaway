"""The scanner: one walk over the roots that records directory sizes and ages, symlinks, git repositories,
rule matches, and the paths that text files mention."""

import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import time

from . import db, rules, util

MB = 1 << 20
FLUSH = 50000

# Path-like tokens: a run of path characters around a slash; ~, $HOME and ${HOME} are expanded later.
# Searching for slashes first keeps data files (which rarely contain one) almost free.
PATH_CHARS = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.+@%${}~-/")
TRAIL = b".,:;)]}>'\""
DATA_EXTS = (".json", ".txt", ".csv", ".ipynb")


def path_tokens(data):
    """(start, token) for every run of path characters that contains a slash."""
    n = len(data)
    i = data.find(b"/")
    while i != -1:
        s = i
        while s > 0 and data[s - 1] in PATH_CHARS:
            s -= 1
        e = i + 1
        while e < n and data[e] in PATH_CHARS:
            e += 1
        yield s, data[s:e]
        i = data.find(b"/", e)


class ScanBusy(RuntimeError):
    pass


class Scanner:
    def __init__(self, cfg, con, verbose=False):
        self.cfg = cfg
        self.con = con
        self.verbose = verbose
        sc = cfg["scan"]
        roots = sorted(set(r.rstrip("/") or "/" for r in sc["roots"]))
        self.roots = [r for r in roots if not any(r != o and util.under(r, o) for o in roots)]
        self.exclude = util.Globs(sc["exclude"])
        self.one_fs = sc["one_filesystem"]
        self.store_min = int(sc["store_min_mb"] * MB)
        self.matcher = rules.Matcher(rules.load_rules(cfg))
        rf = cfg["refs"]
        self.refs_on = rf["enabled"]
        self.ref_exts = tuple(e.lower() for e in rf["extensions"])
        self.ref_names = set(rf["names"])
        self.ref_max = rf["max_kb"] * 1024
        self.data_max = rf["data_max_kb"] * 1024
        self.ref_skip = set(rf["skip_dirs"])
        # files that mention paths without using them: agent transcripts and scratch, tuckaway's own records
        self.ref_skip_paths = util.Globs(rf["skip_paths"] + [cfg["state_dir"] + "/**"])
        self.ref_skip_files = set(cfg["archive"]["markdown_also"] + [cfg.get("_path", "")])
        self.max_tokens = rf["max_tokens_per_file"]
        self.text_exts = self.ref_exts + (".c", ".h", ".cc", ".cpp", ".hpp", ".v", ".sv", ".vh", ".scala", ".java",
                                          ".rs", ".go", ".js", ".ts", ".html", ".css", ".log", ".csv", ".tsv")
        arch = cfg["archive"]
        self.stub_suffixes = tuple([arch["stub_suffix"]] + list(arch["legacy_stub_suffixes"]))
        self.home = os.path.expanduser("~")
        self.seen = set()
        self.files = 0
        self.errors = 0
        self.buf = {"links": [], "refs": [], "rrefs": [], "hits": [], "repos": []}
        self.dirs = []
        self.scan_id = None
        self.last_report = 0.0

    # -- bookkeeping --------------------------------------------------------------------------------------
    def _flush(self, force=False):
        sql = {
            "links": "INSERT INTO links(scan_id, link, target) VALUES (?,?,?)",
            "refs": "INSERT INTO refs(scan_id, token, source, source_mtime) VALUES (?,?,?,?)",
            "rrefs": "INSERT INTO rrefs(scan_id, token, source, source_mtime) VALUES (?,?,?,?)",
            "hits": "INSERT INTO hits(scan_id, rule, path, bytes, files, newest) VALUES (?,?,?,?,?,?)",
            "repos": "INSERT INTO repos(scan_id, path) VALUES (?,?)",
        }
        for k, rows in self.buf.items():
            if rows and (force or len(rows) >= FLUSH):
                self.con.executemany(sql[k], [(self.scan_id,) + tuple(r) for r in rows])
                rows.clear()

    def _under_root(self, p):
        return any(util.under(p, r) for r in self.roots) or util.under(p, self.home)

    def _progress(self, path):
        now = time.time()
        if self.verbose and now - self.last_report > 10:
            self.last_report = now
            sys.stderr.write("  ... %d files, %s\n" % (self.files, path[:120]))
            sys.stderr.flush()

    # -- references ---------------------------------------------------------------------------------------
    def _is_ref_file(self, name, size):
        if name in self.ref_names:
            return size <= self.ref_max
        low = name.lower()
        if not low.endswith(self.ref_exts):
            return False
        return size <= (self.data_max if low.endswith(DATA_EXTS) else self.ref_max)

    def _read_refs(self, path, st):
        try:
            with open(path, "rb") as f:
                data = f.read(self.ref_max)
        except OSError:
            return
        is_json = path.lower().endswith(".json")
        seen = set()
        for start, raw in path_tokens(data):
            raw = raw.rstrip(TRAIL)
            if raw in seen or len(raw) < 4 or raw.startswith(b"//") or data[max(0, start - 1):start] == b":":
                continue
            seen.add(raw)
            if len(seen) > self.max_tokens:
                break
            tok = raw.decode("utf-8", "ignore")
            for pre in ("~/", "$HOME/", "${HOME}/"):
                if tok.startswith(pre):
                    tok = self.home + "/" + tok[len(pre):]
                    break
            if tok.startswith("/"):
                tok = os.path.normpath(tok)
                if self._under_root(tok):
                    self.buf["refs"].append((tok, path, st.st_mtime))
            elif not is_json:
                tok = tok[2:] if tok.startswith("./") else tok
                if "$" not in tok[:1]:
                    self.buf["rrefs"].append((tok.rstrip("/"), path, st.st_mtime))

    # -- the walk -----------------------------------------------------------------------------------------
    def _dir(self, path, depth, st, refs_ok):
        total = st.st_blocks * 512
        files = 0
        newest = st.st_mtime
        newest_path = path
        exec_at = 0.0
        name = os.path.basename(path)
        rule = self.matcher.match_dir(path, name) if depth > 0 else None
        self._progress(path)
        try:
            it = os.scandir(path)
        except OSError:
            self.errors += 1
            return total, files, newest, newest_path, exec_at
        with it:
            for e in it:
                try:
                    n = e.name
                    if e.is_symlink():
                        s = e.stat(follow_symlinks=False)
                        total += s.st_blocks * 512
                        files += 1
                        try:
                            t = os.readlink(e.path)
                            t = os.path.normpath(t if t.startswith("/") else os.path.join(path, t))
                            if self._under_root(t):
                                self.buf["links"].append((e.path, t))
                        except OSError:
                            pass
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if self.exclude and self.exclude.match(e.path):
                            continue
                        s = e.stat(follow_symlinks=False)
                        if self.one_fs and s.st_dev != self.dev:
                            continue
                        if n == ".git":
                            self.buf["repos"].append((path,))
                        sub_ok = refs_ok and n not in self.ref_skip and not self.ref_skip_paths.match(e.path + "/x")
                        b, f, nw, nwp, xa = self._dir(e.path, depth + 1, s, sub_ok)
                        total += b
                        files += f
                        if nw > newest:
                            newest, newest_path = nw, nwp
                        if xa > exec_at:
                            exec_at = xa
                        continue
                    s = e.stat(follow_symlinks=False)
                    files += 1
                    self.files += 1
                    if s.st_nlink > 1:
                        key = (s.st_dev, s.st_ino)
                        if key in self.seen:
                            continue
                        self.seen.add(key)
                    size = s.st_blocks * 512
                    total += size
                    if s.st_mtime > newest and not n.endswith(self.stub_suffixes):
                        newest, newest_path = s.st_mtime, e.path
                    if (s.st_mode & 0o111 and stat.S_ISREG(s.st_mode) and s.st_atime > exec_at
                            and not n.lower().endswith(self.text_exts)):
                        exec_at = s.st_atime
                    if n == ".git":
                        self.buf["repos"].append((path,))
                    if refs_ok and self._is_ref_file(n, s.st_size) and e.path not in self.ref_skip_files \
                            and not n.endswith(self.stub_suffixes):
                        self._read_refs(e.path, s)
                    if self.matcher.file_rules:
                        r = self.matcher.match_file(n, size)
                        if r:
                            self.buf["hits"].append((r.id, e.path, size, 1, s.st_mtime))
                except OSError:
                    self.errors += 1
        if rule and total >= rule.min_bytes:
            self.buf["hits"].append((rule.id, path, total, files, newest))
        if total >= self.store_min or depth <= 1:
            self.dirs.append((path, self.root, depth, total, files, newest, newest_path, exec_at))
        if len(self.buf["refs"]) >= FLUSH or len(self.buf["rrefs"]) >= FLUSH:
            self._flush()
        return total, files, newest, newest_path, exec_at

    def run(self):
        lock = open(os.path.join(self.cfg["state_dir"], "scan.lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ScanBusy()
        started = time.time()
        cur = self.con.execute("INSERT INTO scans(started, roots, status) VALUES (?,?,?)",
                               (started, json.dumps(self.roots), "running"))
        self.scan_id = cur.lastrowid
        self.con.commit()
        total_bytes = 0
        total_files = 0
        for root in self.roots:
            try:
                st = os.lstat(root)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode):
                continue
            self.root, self.dev = root, st.st_dev
            b, f, _, _, _ = self._dir(root, 0, st, self.refs_on)
            total_bytes += b
            total_files += f
        self._flush(force=True)
        self.con.executemany(
            "INSERT OR REPLACE INTO dirs(scan_id, path, root, depth, bytes, files, newest, newest_path, exec_atime)"
            " VALUES (?,?,?,?,?,?,?,?,?)", [(self.scan_id,) + d for d in self.dirs])
        self.con.execute("UPDATE scans SET finished=?, files=?, bytes=?, errors=?, status='done' WHERE id=?",
                         (time.time(), total_files, total_bytes, self.errors, self.scan_id))
        self.con.commit()
        db.prune(self.con, self.cfg["scan"]["keep_scans"])
        lock.close()
        return self.scan_id


def lower_priority():
    try:
        os.nice(10)
    except OSError:
        pass
    if shutil.which("ionice"):
        subprocess.call(["ionice", "-c3", "-p", str(os.getpid())], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)


def scan(cfg, verbose=False):
    con = db.connect(cfg)
    lower_priority()
    return Scanner(cfg, con, verbose).run()
