"""Live evidence that a path is in use: processes and git. (References and symlinks come from the scan.)"""

import os
import re
import subprocess

from . import util

PATH_VARS = ("PATH", "LD_LIBRARY_PATH", "LIBRARY_PATH", "PYTHONPATH", "CPATH", "PKG_CONFIG_PATH", "MANPATH",
             "CONDA_PREFIX", "VIRTUAL_ENV", "JAVA_HOME", "RISCV", "VERILATOR_ROOT")


ARG_SPLIT = re.compile(r"[\s=;:'\"`|&<>()]+")


def _readlink(p):
    try:
        return os.readlink(p)
    except OSError:
        return None


def own_ancestry():
    """This process and its parents: their arguments name the path being checked, which is not a use of it."""
    pids = []
    pid = os.getpid()
    while pid > 1 and len(pids) < 64:
        pids.append(pid)
        try:
            with open("/proc/%d/status" % pid) as f:
                pid = next(int(l.split()[1]) for l in f if l.startswith("PPid:"))
        except (OSError, StopIteration, ValueError):
            break
    return pids


def process_uses(skip_pids=()):
    """[(path, (pid, comm, how))] for every path a readable process uses: cwd, executable, open files,
    mapped files, path arguments and PATH-like environment entries."""
    home = os.path.expanduser("~")
    out = []
    skip = set(str(p) for p in skip_pids) | set(str(p) for p in own_ancestry())
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid in skip:
            continue
        base = "/proc/" + pid
        try:
            with open(base + "/comm") as f:
                comm = f.read().strip()
        except OSError:
            continue
        seen = set()

        def add(path, how):
            if path and path.startswith("/") and path not in seen:
                seen.add(path)
                out.append((path, (int(pid), comm, how)))

        cwd = _readlink(base + "/cwd")
        add(cwd, "cwd")
        add(_readlink(base + "/exe"), "exe")
        try:
            for fd in os.listdir(base + "/fd"):
                add(_readlink(base + "/fd/" + fd), "file")
        except OSError:
            pass
        try:
            with open(base + "/maps") as f:
                for line in f:
                    parts = line.split(None, 5)
                    if len(parts) == 6:
                        p = parts[5].strip()
                        if p.endswith(" (deleted)"):
                            p = p[:-10]
                        add(p, "map")
        except OSError:
            pass
        try:
            with open(base + "/cmdline", "rb") as f:
                args = f.read().split(b"\0")
            for a in args[1:]:
                for part in ARG_SPLIT.split(a.decode("utf-8", "ignore"))[:200]:
                    if "/" in part and len(part) < 4096 and not part.startswith("//"):
                        p = part if part.startswith("/") else os.path.join(cwd or "/", part)
                        add(os.path.normpath(p), "arg")
        except OSError:
            pass
        try:
            with open(base + "/environ", "rb") as f:
                env = f.read().split(b"\0")
            for kv in env:
                k, _, v = kv.decode("utf-8", "ignore").partition("=")
                if k in PATH_VARS:
                    for p in v.split(":"):
                        p = os.path.normpath(p) if p.startswith("/") else ""
                        # an entry that is home itself (or an ancestor) says nothing about any one directory
                        if p and not util.under(home, p):
                            add(p, "env " + k)
        except OSError:
            pass
    return out


class Git:
    def __init__(self, repo_paths=()):
        self.cache = {}
        self.repo_paths = util.PathIndex([(p, None) for p in repo_paths])

    def _run(self, repo, *args):
        try:
            r = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=60)
            return r.returncode, r.stdout.decode("utf-8", "ignore")
        except (OSError, subprocess.TimeoutExpired):
            return 1, ""

    def enclosing(self, path):
        p = path
        while True:
            if os.path.exists(os.path.join(p, ".git")):
                return p
            parent = os.path.dirname(p)
            if parent == p:
                return None
            p = parent

    def tracked_under(self, repo, path):
        rel = os.path.relpath(path, repo)
        try:
            proc = subprocess.Popen(["git", "-C", repo, "ls-files", "-z", "--", rel], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            with proc.stdout:
                first = proc.stdout.read(4096)
            proc.kill()
            proc.wait()
            return bool(first)
        except OSError:
            return False

    def state(self, repo):
        """{'dirty': n, 'ahead': n or None, 'no_upstream': [branches]} for a repository root."""
        if repo in self.cache:
            return self.cache[repo]
        rc, out = self._run(repo, "status", "--porcelain", "--untracked-files=no")
        dirty = len([l for l in out.splitlines() if l.strip()]) if rc == 0 else 0
        rc, out = self._run(repo, "for-each-ref", "--format=%(refname:short) %(upstream:short)", "refs/heads")
        no_up, ahead = [], 0
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2:
                no_up.append(parts[0] if parts else "?")
                continue
            rc2, cnt = self._run(repo, "rev-list", "--count", "%s..%s" % (parts[1], parts[0]))
            if rc2 == 0 and cnt.strip().isdigit():
                ahead += int(cnt.strip())
        st = {"dirty": dirty, "ahead": ahead, "no_upstream": no_up}
        self.cache[repo] = st
        return st

    def info(self, path):
        """What git says about a directory: tracked content in an enclosing repo, and repos inside it."""
        res = {"repo": None, "tracked": False, "nested": []}
        repo = self.enclosing(path)
        if repo and repo != path:
            res["repo"] = repo
            res["tracked"] = self.tracked_under(repo, path)
        elif repo == path:
            res["nested"].append((path, self.state(path)))
        for p, _ in self.repo_paths.under(path)[:20]:
            if p != path:
                res["nested"].append((p, self.state(p)))
        return res
