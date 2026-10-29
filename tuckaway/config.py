"""Configuration: defaults, loading, and a small TOML reader for Python versions without tomllib."""

import copy
import json
import os
import re

try:  # Python 3.11+
    import tomllib as _toml
except ImportError:  # pragma: no cover - depends on the interpreter
    try:
        import tomli as _toml
    except ImportError:
        _toml = None

CONFIG_ENV = "TUCK_CONFIG"
DEFAULT_CONFIG = "~/.config/tuckaway/config.toml"

DEFAULTS = {
    "lang": "zh",
    "state_dir": "~/.local/share/tuckaway",
    "scan": {
        # Directories to scan. Each is scanned without crossing into other file systems.
        "roots": ["~"],
        # Paths (globs, * stays inside one path component, ** crosses components) that are skipped entirely.
        "exclude": [],
        "one_filesystem": True,
        # Directories at least this big are kept in the index (smaller ones only count towards their parents).
        "store_min_mb": 256,
        "keep_scans": 60,
    },
    "refs": {
        # Text files searched for paths, so that a directory a script or a paper still names is not suggested.
        "enabled": True,
        "extensions": [".py", ".sh", ".bash", ".zsh", ".tex", ".bib", ".md", ".mk", ".toml", ".yaml", ".yml",
                       ".json", ".cfg", ".ini", ".txt", ".tcl", ".do", ".f", ".rst", ".ipynb", ".r", ".jl"],
        "names": ["Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", "Dockerfile", ".bashrc", ".zshrc",
                  ".profile", ".bash_profile"],
        "max_kb": 512,
        "data_max_kb": 64,          # .json .txt .csv .ipynb: usually data, rarely worth reading in full
        "skip_dirs": [".git", "node_modules", "__pycache__", "obj_dir", ".venv", "venv", "site-packages",
                      ".cache", ".tox", ".mypy_cache"],
        "max_tokens_per_file": 4000,
        # never read for references: transcripts and scratch of AI agents mention every path they looked at
        "skip_paths": ["~/.claude/**", "~/.codex/**", "~/.cursor/**", "/tmp/claude-*/**", "/tmp/codex-*/**"],
        # files that only describe (documentation): a mention there is a warning, not a sign of use
        "doc_extensions": [".md", ".txt", ".rst", ".html", ".log"],
    },
    "policy": {
        "recent_days": 7,       # modified (or programs run) within this many days: never suggested
        "cold_days": 14,        # not modified for this many days: suggested for the archive
        "min_candidate_mb": 1024,
        "candidate_min_depth": 2,   # how deep below a scan root a whole directory may be suggested
        # Never suggested, whatever their age (toolchains, datasets read in place, ...).
        "keep": [],
        # Directories whose children are projects, for grouping the report (globs).
        "project_roots": [],
    },
    "archive": {
        "backend": "local",     # local | ssh | none
        "compress": "auto",     # auto (zstd when installed, else gzip) | zstd | gzip | none
        "local_dir": "~/tuckaway-archive",
        "tmp_dir": "~/.cache/tuckaway/tmp",
        "stub_suffix": ".TUCKED.txt",
        "legacy_stub_suffixes": [],
        "copy_records": True,
        # Extra Markdown tables to append one row per archived directory to (same columns as records.md).
        "markdown_also": [],
        "ssh": {
            "host": "",
            "user": "",
            "port": 22,
            "key": "",
            "remote_dir": "",
            "shell": "posix",   # posix (sha256sum) | cmd (Windows, certutil)
        },
    },
    "auto": {
        # Rules whose findings may be deleted without approval when `tuck auto` runs.
        "enabled": False,
        "rules": [],
        "max_gb_per_run": 50,
    },
    "web": {"host": "127.0.0.1", "port": 8765},
    "safety": {
        # Approvals are refused when one of these variables is set: they mark a shell started by an AI agent.
        "agent_env_markers": ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_MANAGED_BY_NPM",
                              "CURSOR_AGENT", "AIDER_MODEL", "GEMINI_CLI"],
        # Never deleted or archived, and never part of a suggestion.
        "protect": ["~/.ssh", "~/.gnupg", "~/.config", "~/.claude", "~/.codex", "~/.local/share/tuckaway",
                    "~/.vscode-server/data", "~/.bashrc", "~/.profile"],
    },
    "rules": {
        "disable": [],
        # Extra rules: [rules.custom.<id>] glob = "...", category = "garbage|cache|regen", reason = "..."
        "custom": {},
    },
}

PATH_KEYS = [
    ("state_dir",), ("scan", "roots"), ("scan", "exclude"), ("policy", "keep"), ("policy", "project_roots"),
    ("refs", "skip_paths"), ("archive", "local_dir"), ("archive", "tmp_dir"), ("archive", "markdown_also"),
    ("archive", "ssh", "key"),
    ("safety", "protect"),
]


class ConfigError(ValueError):
    pass


def expand(path):
    if not path:
        return path
    p = os.path.expandvars(os.path.expanduser(path))
    return os.path.normpath(p) if p.startswith("/") else p


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _expand_paths(cfg):
    for keys in PATH_KEYS:
        node = cfg
        for k in keys[:-1]:
            node = node[k]
        v = node[keys[-1]]
        node[keys[-1]] = [expand(x) for x in v] if isinstance(v, list) else expand(v)
    for rule in cfg["rules"]["custom"].values():
        if "glob" in rule:
            rule["glob"] = expand(rule["glob"])


def config_path():
    return expand(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    path = path or config_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                _merge(cfg, loads_toml(f.read()))
            except ValueError as e:
                raise ConfigError("%s: %s" % (path, e))
    cfg["_path"] = path
    _expand_paths(cfg)
    return cfg


# ---------------------------------------------------------------------------------------------------------
# A small TOML reader (tables, dotted keys, strings, numbers, booleans, arrays, inline tables) for Python 3.8-3.10
# without tomli. Dates and multi-line strings are not supported; the configuration does not need them.

def loads_toml(text):
    if _toml is not None:
        return _toml.loads(text)
    return _MiniToml(text).parse()


_BARE = re.compile(r"[A-Za-z0-9_-]+")
_NUM = re.compile(r"[+-]?(0x[0-9A-Fa-f_]+|[0-9_]+(\.[0-9_]+)?([eE][+-]?[0-9_]+)?)")


class _MiniToml:
    def __init__(self, text):
        self.s = text.replace("\r\n", "\n")
        self.i = 0
        self.n = len(self.s)

    def err(self, msg):
        line = self.s.count("\n", 0, self.i) + 1
        raise ValueError("line %d: %s" % (line, msg))

    def parse(self):
        root = {}
        cur = root
        while True:
            self.skip(newlines=True)
            if self.i >= self.n:
                return root
            if self.s.startswith("[[", self.i):
                self.i += 2
                keys = self.keys("]]")
                parent = self.descend(root, keys[:-1])
                lst = parent.setdefault(keys[-1], [])
                if not isinstance(lst, list):
                    self.err("%s is not an array of tables" % ".".join(keys))
                cur = {}
                lst.append(cur)
            elif self.s[self.i] == "[":
                self.i += 1
                cur = self.descend(root, self.keys("]"))
            else:
                keys = self.keys("=")
                val = self.value()
                tbl = self.descend(cur, keys[:-1])
                if keys[-1] in tbl:
                    self.err("duplicate key %s" % keys[-1])
                tbl[keys[-1]] = val
            self.eol()

    def descend(self, tbl, keys):
        for k in keys:
            nxt = tbl.setdefault(k, {})
            if isinstance(nxt, list):
                nxt = nxt[-1]
            if not isinstance(nxt, dict):
                self.err("%s is not a table" % k)
            tbl = nxt
        return tbl

    def skip(self, newlines=False):
        while self.i < self.n:
            c = self.s[self.i]
            if c in " \t" or (newlines and c == "\n"):
                self.i += 1
            elif c == "#":
                while self.i < self.n and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def eol(self):
        self.skip()
        if self.i < self.n and self.s[self.i] != "\n":
            self.err("unexpected text %r" % self.s[self.i:self.i + 20])

    def keys(self, term):
        out = []
        while True:
            self.skip()
            c = self.s[self.i:self.i + 1]
            if c == '"':
                out.append(self.basic_string())
            elif c == "'":
                out.append(self.literal_string())
            else:
                m = _BARE.match(self.s, self.i)
                if not m:
                    self.err("bad key")
                out.append(m.group(0))
                self.i = m.end()
            self.skip()
            if self.s.startswith(term, self.i):
                self.i += len(term)
                return out
            if self.s[self.i:self.i + 1] != ".":
                self.err("expected %r" % term)
            self.i += 1

    def basic_string(self):
        if self.s.startswith('"""', self.i):
            self.err("multi-line strings are not supported without tomllib")
        j = self.i + 1
        while j < self.n and self.s[j] != '"':
            if self.s[j] == "\\":
                j += 1
            if self.s[j] == "\n":
                self.err("unterminated string")
            j += 1
        raw = self.s[self.i:j + 1]
        self.i = j + 1
        return json.loads(raw)

    def literal_string(self):
        j = self.s.find("'", self.i + 1)
        if j < 0 or "\n" in self.s[self.i:j]:
            self.err("unterminated string")
        out = self.s[self.i + 1:j]
        self.i = j + 1
        return out

    def value(self):
        self.skip()
        c = self.s[self.i:self.i + 1]
        if c == '"':
            return self.basic_string()
        if c == "'":
            return self.literal_string()
        if c == "[":
            self.i += 1
            out = []
            while True:
                self.skip(newlines=True)
                if self.s[self.i:self.i + 1] == "]":
                    self.i += 1
                    return out
                out.append(self.value())
                self.skip(newlines=True)
                c = self.s[self.i:self.i + 1]
                if c == ",":
                    self.i += 1
                elif c != "]":
                    self.err("expected , or ] in array")
        if c == "{":
            self.i += 1
            out = {}
            self.skip()
            if self.s[self.i:self.i + 1] == "}":
                self.i += 1
                return out
            while True:
                keys = self.keys("=")
                self.descend(out, keys[:-1])[keys[-1]] = self.value()
                self.skip()
                c = self.s[self.i:self.i + 1]
                self.i += 1
                if c == "}":
                    return out
                if c != ",":
                    self.err("expected , or } in inline table")
        for word, val in (("true", True), ("false", False)):
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return val
        m = _NUM.match(self.s, self.i)
        if not m:
            self.err("unsupported value")
        self.i = m.end()
        txt = m.group(0).replace("_", "")
        if txt.lower().startswith(("0x", "+0x", "-0x")):
            return int(txt, 16)
        if any(ch in txt for ch in ".eE"):
            return float(txt)
        return int(txt)
