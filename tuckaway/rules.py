"""Built-in rules for well-known garbage, caches and rebuildable intermediates.

A rule matches during the scan (by directory name, path glob, file name or extension); its check runs when
suggestions are made, with the live evidence at hand. The first rule that matches a directory wins.
"""

import glob
import json
import os
import re

from . import util

MB = 1 << 20


class Rule:
    def __init__(self, id, kind, category, action="delete", names=(), globs=(), exts=(), name_re=None,
                 min_mb=64, min_age=1, confidence="high", auto_ok=False, check=None, group=False):
        self.id = id
        self.kind = kind                # dir | file
        self.category = category        # garbage | cache | regen
        self.action = action            # delete | archive
        self.names = set(names)
        self.globs = list(globs)
        self.exts = tuple(exts)
        self.name_re = re.compile(name_re) if name_re else None
        self.min_bytes = min_mb * MB
        self.min_age = min_age          # days since the last change
        self.confidence = confidence
        self.auto_ok = auto_ok          # recommended for automatic deletion
        self.check = check              # name of a function below, or None
        self.group = group              # many small hits are shown as one suggestion
        self.custom_reason = ""
        self._globs = None

    def compiled_globs(self):
        if self._globs is None:
            self._globs = util.Globs([os.path.normpath(os.path.expanduser(g)) for g in self.globs])
        return self._globs


BUILTIN = [
    Rule("pycache", "dir", "cache", names=["__pycache__"], min_mb=0, min_age=1, auto_ok=True, group=True),
    Rule("pip-cache", "dir", "cache", globs=["~/.cache/pip", "~/.cache/uv", "~/.cache/pypoetry",
                                              "~/.npm/_cacache", "~/.cache/yarn"], auto_ok=True, min_age=0),
    Rule("conda-tmp", "dir", "garbage", globs=["/tmp/conda-lock-pkgs-*", "/tmp/conda-*-pkgs-*",
                                                "/tmp/tmp*conda*"], min_age=1, auto_ok=True),
    Rule("vscode-old-server", "dir", "garbage", globs=["~/.vscode-server/cli/servers/Stable-*",
                                                        "~/.vscode-server/bin/*", "~/.cursor-server/bin/*"],
         min_mb=16, min_age=1, auto_ok=True, check="old_vscode_server"),
    Rule("vscode-old-extension", "dir", "garbage", globs=["~/.vscode-server/extensions/*",
                                                           "~/.vscode/extensions/*", "~/.cursor-server/extensions/*"],
         min_mb=16, min_age=1, auto_ok=True, check="old_extension"),
    Rule("agent-tmp", "dir", "garbage", globs=["/tmp/claude-*/*/*"], min_mb=16, min_age=3,
         confidence="medium", check="dead_agent_session"),
    Rule("transfer-tmp", "dir", "garbage", globs=["~/.cache/to_external", "~/.cache/tuckaway/tmp"],
         min_mb=16, min_age=1, auto_ok=True),
    Rule("verilator-objdir", "dir", "regen", names=["obj_dir", "verilator-compile"], min_mb=256,
         min_age=14, confidence="medium", check="has_objects"),
    Rule("node-modules", "dir", "regen", names=["node_modules"], min_mb=256, min_age=14, confidence="medium"),
    Rule("trash", "dir", "garbage", globs=["~/.local/share/Trash"], min_mb=64, min_age=0, confidence="medium"),
    Rule("tool-cache", "dir", "cache", globs=["~/.cache/*"], min_mb=512, min_age=14, confidence="medium"),
    Rule("core-dump", "file", "garbage", name_re=r"^core(\.\d+)?$", min_mb=0, min_age=1, confidence="medium",
         check="elf_core", group=True),
    Rule("waveform", "file", "regen", exts=[".vcd", ".fst", ".fsdb", ".vpd", ".wlf"], min_mb=200, min_age=14,
         confidence="low"),
    Rule("dup-archive", "file", "garbage", exts=[".tar.zst", ".tar.gz", ".tgz", ".tar", ".zip", ".tar.xz"],
         min_mb=100, min_age=1, check="dup_archive"),
]


def load_rules(cfg):
    disabled = set(cfg["rules"]["disable"])
    rules = [r for r in BUILTIN if r.id not in disabled]
    for rid, spec in cfg["rules"]["custom"].items():
        rules.insert(0, Rule(rid, spec.get("kind", "dir"), spec.get("category", "garbage"),
                             action=spec.get("action", "delete"), globs=[spec["glob"]] if "glob" in spec else [],
                             names=spec.get("names", []), exts=spec.get("exts", []),
                             min_mb=spec.get("min_mb", 64), min_age=spec.get("min_age", 1),
                             confidence=spec.get("confidence", "medium"), auto_ok=spec.get("auto_ok", False)))
        rules[0].custom_reason = spec.get("reason", "")
    return rules


def by_id(rules):
    return {r.id: r for r in rules}


class Matcher:
    """Used by the scanner: which rule (if any) a directory or a file belongs to."""

    def __init__(self, rules):
        self.dir_rules = [r for r in rules if r.kind == "dir"]
        self.file_rules = [r for r in rules if r.kind == "file"]
        self.file_exts = tuple(e for r in self.file_rules for e in r.exts)

    def match_dir(self, path, name):
        for r in self.dir_rules:
            if name in r.names or (r.globs and r.compiled_globs().match(path)):
                return r
        return None

    def match_file(self, name, size):
        for r in self.file_rules:
            if size < r.min_bytes:
                continue
            if (r.exts and name.endswith(r.exts)) or (r.name_re and r.name_re.match(name)):
                return r
        return None


# ---------------------------------------------------------------------------------------------------------
# Checks: return (ok, extra reason key or None). `ctx` is recommend.Context.

def check_old_vscode_server(ctx, path):
    parent = os.path.dirname(path)
    siblings = [os.path.join(parent, n) for n in os.listdir(parent) if not n.startswith(".")]
    siblings = [s for s in siblings if os.path.isdir(s)]
    if len(siblings) < 2:
        return False, None
    newest = max(siblings, key=lambda s: os.stat(s).st_mtime)
    if path == newest:
        return False, None
    return True, None


_EXT_RE = re.compile(r"^(?P<id>[A-Za-z0-9-]+\.[A-Za-z0-9-]+)-(?P<ver>\d+(?:\.\d+)*)(?:-.*)?$")


def _ver(v):
    return tuple(int(x) for x in v.split("."))


def check_old_extension(ctx, path):
    m = _EXT_RE.match(os.path.basename(path))
    if not m:
        return False, None
    parent = os.path.dirname(path)
    best = None
    for n in os.listdir(parent):
        m2 = _EXT_RE.match(n)
        if m2 and m2.group("id") == m.group("id") and (best is None or _ver(m2.group("ver")) > best):
            best = _ver(m2.group("ver"))
    return (best is not None and _ver(m.group("ver")) < best), None


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def check_dead_agent_session(ctx, path):
    sid = os.path.basename(path)
    if not _UUID.match(sid):
        return False, None
    if ctx.session_alive(sid):
        return False, None
    return True, None


def check_has_objects(ctx, path):
    try:
        names = os.listdir(path)
    except OSError:
        return False, None
    return any(n.endswith((".o", ".a", ".mk", ".d")) for n in names), None


def check_elf_core(ctx, path):
    return util.is_elf_core(path), None


def check_dup_archive(ctx, path):
    size = os.path.getsize(path)
    shas = ctx.archived_sha_by_size.get(size)
    if not shas:
        return False, None
    return util.sha256_file(path) in shas, None


CHECKS = {
    "old_vscode_server": check_old_vscode_server,
    "old_extension": check_old_extension,
    "dead_agent_session": check_dead_agent_session,
    "has_objects": check_has_objects,
    "elf_core": check_elf_core,
    "dup_archive": check_dup_archive,
}


def claude_sessions_alive(home=None):
    """Session ids of running Claude Code processes (from ~/.claude/sessions/<pid>.json)."""
    alive = set()
    for f in glob.glob(os.path.join(home or os.path.expanduser("~"), ".claude", "sessions", "*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
            pid = int(d.get("pid", 0))
            if pid and os.path.exists("/proc/%d" % pid):
                alive.add(d.get("sessionId"))
        except (OSError, ValueError):
            continue
    return alive
