"""Archiving and restoring: tar (+ zstd or gzip), verify, store locally or over ssh, leave a stub, record it."""

import contextlib
import fcntl
import os
import re
import shlex
import shutil
import subprocess
import time

from . import util
from .i18n import t

EXT = {"zstd": ".tar.zst", "gzip": ".tar.gz", "none": ".tar"}


class ArchiveError(RuntimeError):
    pass


def compressor(cfg):
    c = cfg["archive"]["compress"]
    if c == "auto":
        return "zstd" if shutil.which("zstd") else "gzip"
    return c


def comp_of(name):
    if name.endswith((".zst", ".zstd")):
        return "zstd"
    if name.endswith((".gz", ".tgz")):
        return "gzip"
    if name.endswith(".xz"):
        return "xz"
    return "none"


def _tar_filter(comp):
    if comp == "zstd":
        return ["-I", "zstd"]
    if comp == "gzip":
        return ["-I", "pigz"] if shutil.which("pigz") else ["-z"]
    if comp == "xz":
        return ["-J"]
    return []


def tar_create(src, out, comp):
    parent, base = os.path.split(src.rstrip("/"))
    if comp == "zstd":
        p1 = subprocess.Popen(["tar", "-C", parent, "-cf", "-", base], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["zstd", "-q", "-3", "-T2", "-f", "-o", out], stdin=p1.stdout)
        p1.stdout.close()
        rc2, rc1 = p2.wait(), p1.wait()
        rc = rc1 or rc2
    else:
        rc = subprocess.call(["tar"] + _tar_filter(comp) + ["-C", parent, "-cf", out, base])
    if rc != 0:
        raise ArchiveError(t("e_tar", rc=rc))


def tar_count(arc, comp):
    p = subprocess.Popen(["tar"] + _tar_filter(comp) + ["-tf", arc], stdout=subprocess.PIPE)
    with p.stdout:
        n = sum(1 for _ in p.stdout)
    if p.wait() != 0:
        raise ArchiveError(t("e_tar", rc=p.returncode))
    return n


def tar_extract(arc, parent, comp):
    rc = subprocess.call(["tar"] + _tar_filter(comp) + ["-C", parent, "-xf", arc])
    if rc != 0:
        raise ArchiveError(t("e_tar", rc=rc))


@contextlib.contextmanager
def transfer_lock(cfg, log=print):
    os.makedirs(cfg["state_dir"], exist_ok=True)
    f = open(os.path.join(cfg["state_dir"], "transfer.lock"), "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log(t("m_wait_lock"))
            fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        f.close()


# -- backends ---------------------------------------------------------------------------------------------

class LocalBackend:
    name = "local"

    def __init__(self, cfg):
        self.dir = cfg["archive"]["local_dir"]

    def target(self, rel):
        return os.path.join(self.dir, rel)

    def work_file(self, cfg, rel):
        p = self.target(rel) + ".partial"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def put(self, work, rel):
        os.replace(work, self.target(rel))

    def sha256(self, rel):
        return util.sha256_file(self.target(rel))

    def fetch(self, cfg, rel):
        p = self.target(rel)
        if not os.path.exists(p):
            raise ArchiveError(t("e_missing_archive", where=p))
        return p, False

    def describe(self, rel):
        return self.target(rel)

    def manual_restore(self, rel, parent):
        return "tar %s -C %s -xf %s" % (" ".join(_tar_filter(comp_of(rel))), shlex.quote(parent),
                                         shlex.quote(self.target(rel)))

    def copy_file(self, local, rel):
        os.makedirs(self.dir, exist_ok=True)
        shutil.copyfile(local, self.target(rel))


class SSHBackend:
    name = "ssh"

    def __init__(self, cfg):
        s = cfg["archive"]["ssh"]
        if not s["host"] or not s["remote_dir"]:
            raise ArchiveError(t("e_ssh_config"))
        self.host = s["host"]
        self.user = s["user"]
        self.port = str(s["port"])
        self.key = s["key"]
        self.remote_dir = s["remote_dir"].replace("\\", "/").rstrip("/")
        self.shell = s["shell"]
        self.opts = ["-o", "BatchMode=yes"] + (["-i", self.key, "-o", "IdentitiesOnly=yes"] if self.key else [])

    @property
    def dest(self):
        return "%s@%s" % (self.user, self.host) if self.user else self.host

    def rpath(self, rel):
        return self.remote_dir + "/" + rel.replace("\\", "/")

    def native(self, rel):
        p = self.rpath(rel)
        return p.replace("/", "\\") if self.shell == "cmd" else p

    def _ssh(self, command):
        r = subprocess.run(["ssh", "-p", self.port] + self.opts + [self.dest, command], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=600)
        return r.returncode, r.stdout.decode("utf-8", "ignore"), r.stderr.decode("utf-8", "ignore")

    def _scp(self, src, dst):
        rc = subprocess.call(["scp", "-q", "-P", self.port] + self.opts + [src, dst])
        if rc != 0:
            raise ArchiveError(t("e_scp", rc=rc))

    def work_file(self, cfg, rel):
        d = cfg["archive"]["tmp_dir"]
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, os.path.basename(rel))

    def put(self, work, rel):
        d = os.path.dirname(rel)
        if d:
            if self.shell == "cmd":
                q = '"%s"' % self.native(d)
                cmd = "if not exist %s mkdir %s" % (q, q)
            else:
                cmd = "mkdir -p %s" % shlex.quote(self.rpath(d))
            rc, _, err = self._ssh(cmd)
            if rc != 0:
                raise ArchiveError(t("e_ssh", err=err.strip()[:200]))
        self._scp(work, "%s:%s" % (self.dest, self.rpath(rel)))

    def sha256(self, rel):
        if self.shell == "cmd":
            rc, out, err = self._ssh('certutil -hashfile "%s" SHA256' % self.native(rel))
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            h = lines[1].replace(" ", "").lower() if len(lines) > 1 else ""
        else:
            rc, out, err = self._ssh("sha256sum %s" % shlex.quote(self.rpath(rel)))
            h = out.split()[0].lower() if out.split() else ""
        if rc != 0 or not re.fullmatch(r"[0-9a-f]{64}", h):
            raise ArchiveError(t("e_ssh", err=(err or out).strip()[:200]))
        return h

    def fetch(self, cfg, rel):
        local = self.work_file(cfg, rel)
        self._scp("%s:%s" % (self.dest, self.rpath(rel)), local)
        return local, True

    def describe(self, rel):
        return "%s:%s" % (self.dest, self.rpath(rel))

    def manual_restore(self, rel, parent):
        key = (" -i %s" % shlex.quote(self.key)) if self.key else ""
        name = os.path.basename(rel)
        return "scp -P %s%s %s:%s . && tar %s -C %s -xf %s" % (
            self.port, key, self.dest, self.rpath(rel), " ".join(_tar_filter(comp_of(rel))), shlex.quote(parent),
            shlex.quote(name))

    def copy_file(self, local, rel):
        try:
            self._scp(local, "%s:%s" % (self.dest, self.rpath(rel)))
        except ArchiveError:
            pass


def backend(cfg, name=None):
    name = name or cfg["archive"]["backend"]
    if name == "local":
        return LocalBackend(cfg)
    if name == "ssh":
        return SSHBackend(cfg)
    raise ArchiveError(t("e_no_backend"))


# -- records ----------------------------------------------------------------------------------------------

MD_HEADER = "| date | project | path | archive | entries | size | sha256 |\n|---|---|---|---|---|---|---|\n"


def md_row(rec):
    return "| %s | %s | `%s` | `%s` | %s | %s | `%s` |\n" % (
        time.strftime("%Y-%m-%d", time.localtime(rec["time"])), rec["project"], rec["path"], rec["where"],
        rec["members"], util.human(rec["archive_bytes"]), rec["sha256"])


def write_records(cfg, rec, back):
    md = os.path.join(cfg["state_dir"], "records.md")
    new = not os.path.exists(md)
    with open(md, "a", encoding="utf-8") as f:
        if new:
            f.write("# tuckaway records\n\n" + MD_HEADER)
        f.write(md_row(rec))
    for extra in cfg["archive"]["markdown_also"]:
        try:
            with open(extra, "a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(md_row(rec))
        except OSError:
            pass
    if cfg["archive"]["copy_records"]:
        back.copy_file(md, "records.md")


def insert_record(con, **kw):
    cols = ["time", "action", "path", "project", "backend", "location", "bytes", "archive_bytes", "members",
            "sha256", "plan", "actor", "note"]
    con.execute("INSERT INTO records(%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                [kw.get(c) for c in cols])
    con.commit()


def _safe(s):
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", s).strip("_") or "x"


def archive_name(cfg, path, project, comp, now):
    # named after the path below the nearest scan root (or home), so names stay short
    bases = sorted(set(cfg["scan"]["roots"] + [os.path.expanduser("~")]), key=len, reverse=True)
    base = next((b for b in bases if b != path and util.under(path, b)), None)
    rel = os.path.relpath(path, base) if base else path.lstrip("/")
    return "%s/%s-%s%s" % (_safe(project), _safe(rel.replace("/", "__")), time.strftime("%Y%m%d-%H%M",
                                                                                        time.localtime(now)), EXT[comp])


STUB = """tuckaway: archived on {date} ({how}).
tuckaway：本{kind}已于 {date} 归档（{how_zh}）。
Original / 原路径: {path}
Archive / 归档包: {where}
Size / 大小: {size} on disk, archive {asize}, {members} entries, SHA-256 {sha}
Restore / 恢复: tuck restore {path}
Without tuckaway / 不用 tuckaway: {manual}
"""


def archive_path(con, cfg, path, project, plan_id, actor, log=print):
    back = backend(cfg)
    comp = compressor(cfg)
    stub_suffixes = tuple([cfg["archive"]["stub_suffix"]] + list(cfg["archive"]["legacy_stub_suffixes"]))
    isdir = os.path.isdir(path) and not os.path.islink(path)
    newest0, count0, size0 = util.tree_state(path, stub_suffixes)
    now = time.time()
    rel = archive_name(cfg, path, project, comp, now)
    with transfer_lock(cfg, log):
        work = back.work_file(cfg, rel)
        try:
            log(t("m_packing", path=path, size=util.human(size0)))
            tar_create(path, work, comp)
            n = tar_count(work, comp)
            expected = count0 + (1 if isdir else 0)
            if n != expected:
                raise ArchiveError(t("e_count", n=n, expected=expected))
            sha = util.sha256_file(work)
            asize = os.path.getsize(work)
            log(t("m_storing", where=back.describe(rel), size=util.human(asize)))
            back.put(work, rel)
            remote = back.sha256(rel)
            if remote != sha:
                raise ArchiveError(t("e_sha", a=sha[:12], b=remote[:12]))
        finally:
            if os.path.exists(work):    # the ssh temp copy, or a local .partial left by a failure
                os.remove(work)
        newest1, count1, _ = util.tree_state(path, stub_suffixes)
        if newest1 > newest0 + 1e-6 or count1 != count0:
            raise ArchiveError(t("e_changed"))
        remove_path(path)
        manual = back.manual_restore(rel, os.path.dirname(path))
        with open(path.rstrip("/") + cfg["archive"]["stub_suffix"], "w", encoding="utf-8") as f:
            f.write(STUB.format(date=time.strftime("%Y-%m-%d %H:%M"), how="plan %s, %s" % (plan_id, actor),
                                how_zh="计划 %s，%s" % (plan_id, actor), kind="目录" if isdir else "文件",
                                path=path, where=back.describe(rel), size=util.human(size0),
                                asize=util.human(asize), members=count0, sha=sha, manual=manual))
        rec = {"time": now, "action": "archive", "path": path, "project": project, "backend": back.name,
               "location": back.target(rel) if back.name == "local" else rel, "bytes": size0,
               "archive_bytes": asize, "members": count0, "sha256": sha, "plan": plan_id, "actor": actor,
               "where": back.describe(rel)}
        insert_record(con, **rec)
        write_records(cfg, rec, back)
    return rec


def remove_path(path):
    def onerror(func, p, exc):
        try:
            os.chmod(os.path.dirname(p), 0o700)
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            raise exc[1]

    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, onerror=onerror)
    else:
        os.remove(path)


def find_record(con, path):
    rec = con.execute("SELECT * FROM records WHERE action='archive' AND path=? ORDER BY time DESC LIMIT 1",
                      (path,)).fetchone()
    if not rec:
        return None
    later = con.execute("SELECT 1 FROM records WHERE action='restore' AND path=? AND time>?",
                        (path, rec["time"])).fetchone()
    return None if later else rec


def restore(con, cfg, path, actor, log=print):
    path = os.path.abspath(os.path.expanduser(path)).rstrip("/")
    for suf in [cfg["archive"]["stub_suffix"]] + list(cfg["archive"]["legacy_stub_suffixes"]):
        if path.endswith(suf):
            path = path[:-len(suf)]
    rec = find_record(con, path)
    if not rec:
        raise ArchiveError(t("e_no_record", path=path))
    if os.path.lexists(path):
        raise ArchiveError(t("e_exists", path=path))
    back = backend(cfg, rec["backend"])
    loc = rec["location"]
    rel = loc
    if back.name == "local":    # local records keep the absolute path of the archive
        back.dir, rel = os.path.dirname(loc), os.path.basename(loc)
    with transfer_lock(cfg, log):
        log(t("m_fetching", where=back.describe(rel)))
        arc, temp = back.fetch(cfg, rel)
        try:
            if rec["sha256"]:
                sha = util.sha256_file(arc)
                if sha != rec["sha256"]:
                    raise ArchiveError(t("e_sha", a=rec["sha256"][:12], b=sha[:12]))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tar_extract(arc, os.path.dirname(path), comp_of(arc))
        finally:
            if temp and os.path.exists(arc):
                os.remove(arc)
    if not os.path.lexists(path):
        raise ArchiveError(t("e_restore_missing", path=path))
    for suf in [cfg["archive"]["stub_suffix"]] + list(cfg["archive"]["legacy_stub_suffixes"]):
        if os.path.exists(path + suf):
            os.remove(path + suf)
    insert_record(con, time=time.time(), action="restore", path=path, project=rec["project"],
                  backend=rec["backend"], location=loc, sha256=rec["sha256"], actor=actor)
    return rec


_ROW = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([^|]*?)\s*\|\s*`?([^|`]+)`?\s*\|\s*`?([^|`]+)`?\s*\|"
                  r"\s*(\d+)\s*\|\s*([^|]*?)\s*\|\s*`?([0-9a-f]{64})`?\s*\|")


def import_markdown(con, path, backend_name):
    """Import rows of a Markdown table (date | project | path | archive | files | size | sha256)."""
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _ROW.match(line.strip())
            if not m:
                continue
            date, project, opath, loc, members, _, sha = m.groups()
            loc = loc.strip().replace("\\", "/")
            if con.execute("SELECT 1 FROM records WHERE path=? AND sha256=?", (opath.strip(), sha)).fetchone():
                continue
            insert_record(con, time=time.mktime(time.strptime(date, "%Y-%m-%d")), action="archive",
                          path=opath.strip(), project=project, backend=backend_name, location=loc,
                          members=int(members), sha256=sha, actor="import", note="imported from " + path)
            n += 1
    return n
