"""Suggestions: what can be deleted or archived, with the reasons for and against."""

import hashlib
import json
import os
import time
from collections import defaultdict

from . import db, evidence, rules, util
from .i18n import t

MB = 1 << 20

COMMON_NAMES = set("""build builds result results work data src source out output outputs logs log tmp temp test tests
scripts docs doc bin lib lib64 include config configs sim sims runs run cache dist target release debug assets images
figures figs models checkpoints examples vendor third_party external deps tools utils common archive backup old new
misc notes paper papers report reports latest main master install prefix share local env venv""".split())


NAME_MAX_SOURCES = 25   # a name mentioned by more files than this is too common to mean anything


class NoScan(RuntimeError):
    pass


def item_id(action, paths):
    return hashlib.sha1((action + "\0" + "\0".join(sorted(paths))).encode()).hexdigest()[:6]


class Context:
    def __init__(self, con, cfg, scan=None, now=None, live=True):
        self.con = con
        self.cfg = cfg
        self.scan = scan or db.latest_scan(con)
        if not self.scan:
            raise NoScan()
        self.sid = self.scan["id"]
        self.now = now or time.time()
        pol = cfg["policy"]
        self.recent = pol["recent_days"]
        self.cold = pol["cold_days"]
        self.min_cand = pol["min_candidate_mb"] * MB
        self.min_depth = pol["candidate_min_depth"]
        self.keep_globs = util.Globs(pol["keep"])
        self.keep_literals = [k for k in pol["keep"] if not any(c in k for c in "*?")]
        self.project_roots = util.Globs(pol["project_roots"])
        self.protect = cfg["safety"]["protect"]
        arch = cfg["archive"]
        self.roots = json.loads(self.scan["roots"])
        # never suggested for the archive: caches, the archive itself, and a scanned /tmp (rules suggest deletion)
        self.no_archive = [os.path.expanduser("~/.cache"), arch["local_dir"], arch["tmp_dir"], cfg["state_dir"]] + \
            [r for r in self.roots if r in ("/tmp", "/var/tmp")]
        self.rules = rules.by_id(rules.load_rules(cfg))
        self.dirs = {r["path"]: r for r in con.execute("SELECT * FROM dirs WHERE scan_id=?", (self.sid,))}
        self.children = defaultdict(list)
        for p in self.dirs:
            parent = os.path.dirname(p)
            if parent != p and parent in self.dirs:
                self.children[parent].append(p)
        self.roots = json.loads(self.scan["roots"])
        self.procs = util.PathIndex(evidence.process_uses()) if live else util.PathIndex()
        self.links = util.PathIndex([(r["target"], r["link"]) for r in
                                     con.execute("SELECT link, target FROM links WHERE scan_id=?", (self.sid,))])
        self.git = evidence.Git([r[0] for r in con.execute("SELECT path FROM repos WHERE scan_id=?", (self.sid,))])
        self.tags = {r["path"]: r for r in con.execute("SELECT * FROM tags")}
        self.sessions = rules.claude_sessions_alive()
        self.archived_sha_by_size = defaultdict(set)
        for r in con.execute("SELECT archive_bytes, sha256 FROM records WHERE action='archive'"):
            if r["sha256"]:
                self.archived_sha_by_size[r["archive_bytes"]].add(r["sha256"])
        self.doc_exts = tuple(e.lower() for e in cfg["refs"]["doc_extensions"])
        self.items = []
        self.kept = []
        self.watch = []
        self.covered = set()
        self._names = None
        self._dir_names = None

    # -- small queries ------------------------------------------------------------------------------------
    def session_alive(self, sid):
        if sid in self.sessions:
            return True
        return any(sid in p for p, _ in self.procs.items)

    def protected(self, p):
        return any(util.under(p, x) for x in self.protect)

    def contains_protected(self, p):
        return any(util.under(x, p) for x in self.protect)

    def tag_of(self, p):
        """The tag on p or its nearest tagged ancestor."""
        if p in self.tags:
            return self.tags[p]
        for a in util.ancestors(p):
            if a in self.tags:
                return self.tags[a]
        return None

    def kept_by_config(self, p):
        return bool(self.keep_globs) and any(self.keep_globs.match(a) for a in [p] + util.ancestors(p))

    def contains_kept(self, p):
        """A kept path (a literal policy.keep entry or a keep/paper tag) lies inside p."""
        if any(k != p and util.under(k, p) for k in self.keep_literals):
            return True
        return any(k != p and util.under(k, p) and tg["category"] in ("keep", "paper") for k, tg in self.tags.items())

    def is_covered(self, p):
        return p in self.covered or any(a in self.covered for a in util.ancestors(p))

    def root_of(self, p):
        for r in self.roots:
            if util.under(p, r):
                return r
        return "/"

    def depth(self, p):
        r = self.root_of(p)
        return 0 if p == r else len(os.path.relpath(p, r).split("/"))

    def is_project_level(self, p, d=None):
        """Directories the report treats as projects (for growth and for 'big but kept')."""
        if self.project_roots:
            return self.project_roots.match(os.path.dirname(p)) and not self.project_roots.match(p)
        return (self.depth(p) if d is None else d) == self.min_depth

    def project_of(self, p):
        tag = self.tag_of(p)
        if tag and tag["project"]:
            return tag["project"]
        if self.project_roots:
            for a in [p] + util.ancestors(p):
                if self.project_roots.match(os.path.dirname(a)):
                    return os.path.basename(a)
        r = self.root_of(p)
        parts = os.path.relpath(p, r).split("/") if p != r else []
        if len(parts) >= self.min_depth:
            return parts[self.min_depth - 1]
        return parts[-1] if parts else os.path.basename(r) or r

    def proc_desc(self, uses, limit=3):
        seen = []
        for _, (pid, comm, how) in uses:
            d = "%s(%d, %s)" % (comm, pid, how)
            if d not in seen:
                seen.append(d)
        return ", ".join(seen[:limit]) + (" ..." if len(seen) > limit else "")

    def refs_to(self, p):
        # one range query on the (scan_id, token) index: p itself, p + "-x" and p + "/..." sort between p and p + "0"
        rows = self.con.execute(
            "SELECT DISTINCT token, source, source_mtime FROM refs WHERE scan_id=? AND token>=? AND token<?",
            (self.sid, p, p + "0")).fetchall()
        out = {(r[1], r[2]) for r in rows if (r[0] == p or r[0].startswith(p + "/")) and not util.under(r[1], p)}
        return sorted(out, key=lambda x: -x[1])

    def name_refs(self, p):
        """Files that mention p's name inside a relative path. Names many directories share say nothing."""
        base = os.path.basename(p)
        if len(base) < 5 or base.lower() in COMMON_NAMES:
            return []
        if self._names is None:     # component -> sources, built once from the relative paths files mention
            self._names = defaultdict(set)
            for r in self.con.execute("SELECT token, source, source_mtime FROM rrefs WHERE scan_id=?", (self.sid,)):
                for comp in r[0].split("/"):
                    if len(comp) >= 5 and comp.lower() not in COMMON_NAMES:
                        self._names[comp].add((r[1], r[2]))
            self._dir_names = defaultdict(int)
            for d in self.dirs:
                self._dir_names[os.path.basename(d)] += 1
        found = self._names.get(base, ())
        if self._dir_names.get(base, 0) >= 3 or len(found) > NAME_MAX_SOURCES:
            return []
        return sorted([s for s in found if not util.under(s[0], p)], key=lambda x: -x[1])

    def short(self, path):
        home = os.path.expanduser("~")
        return "~" + path[len(home):] if util.under(path, home) else path

    def ex(self, refs, limit=2):
        return ", ".join("%s (%s)" % (self.short(s), util.fmt_date(m, self.now)) for s, m in refs[:limit]) + \
            (" ..." if len(refs) > limit else "")

    # -- assessment ---------------------------------------------------------------------------------------
    def assess(self, p, row, frozen=False, full=True):
        """Blockers (any one keeps p out of the suggestions), warnings and supporting facts,
        each a list of (code, text). With full=False it stops at the first cheap blockers."""
        B, W, F = [], [], []

        def add(lst, code, **kw):
            lst.append((code, t(code, **kw)))

        age = util.days_since(row["newest"], self.now)
        if not frozen:
            if age < self.recent:
                add(B, "b_recent", recent=self.recent, date=util.fmt_date(row["newest"], self.now))
            elif age < self.cold:
                add(B, "b_warm", date=util.fmt_date(row["newest"], self.now), cold=self.cold)
        if row["exec_atime"] and util.days_since(row["exec_atime"], self.now) < self.recent:
            add(B, "b_exec", date=util.fmt_date(row["exec_atime"], self.now))
        uses = self.procs.under(p)
        if uses:
            add(B, "b_proc", procs=self.proc_desc(uses))
        links = [(tgt, ln) for tgt, ln in self.links.under(p) if not util.under(ln, p)]
        if links:
            add(B, "b_links", n=len(links), ex=self.short(links[0][1]))
        refs = self.refs_to(p)
        tex_refs = [r for r in refs if r[0].endswith((".tex", ".bib"))]
        doc_refs = [r for r in refs if r[0].lower().endswith(self.doc_exts) and r not in tex_refs]
        recent_refs = [r for r in refs if util.days_since(r[1], self.now) < self.recent
                       and r not in tex_refs and r not in doc_refs]
        if recent_refs:
            add(B, "b_refs_recent", ex=self.ex(recent_refs))
        if tex_refs:
            add(B, "b_refs_tex", ex=self.ex(tex_refs))
        if B and not full:
            return B, W, F
        g = self.git.info(p)
        if g["tracked"]:
            add(B, "b_tracked", repo=self.short(g["repo"]))
        if B and not full:
            return B, W, F
        old_refs = [r for r in refs if r not in recent_refs and r not in tex_refs and r not in doc_refs]
        if old_refs:
            add(W, "w_refs_old", ex=self.ex(old_refs))
        if doc_refs:
            add(W, "w_refs_doc", ex=self.ex(doc_refs))
        names = [r for r in self.name_refs(p) if r[0] not in {x[0] for x in refs}]
        if names:
            add(W, "w_name", name=os.path.basename(p), n=len(names), ex=self.ex(names))
        for repo, st in g["nested"]:
            if st["dirty"] or st["ahead"] or st["no_upstream"]:
                add(W, "w_unpushed", repo=self.short(repo))
            else:
                add(F, "f_pushed", repo=self.short(repo))
        if not uses:
            add(F, "r_noproc")
        if not refs:
            add(F, "r_norefs")
        return B, W, F

    # -- suggestions --------------------------------------------------------------------------------------
    def add_item(self, paths, size, action, category, source, confidence, reasons, warnings, newest, auto=None,
                 rebuild=None):
        it = {
            "id": item_id(action, paths), "paths": paths, "bytes": size, "action": action, "category": category,
            "source": source, "confidence": confidence, "reasons": reasons, "warnings": warnings,
            "newest": newest, "project": self.project_of(paths[0]), "auto": auto or {"recommended": False},
            "rebuild": rebuild,
        }
        self.items.append(it)
        for p in paths:
            self.covered.add(p)
        return it

    def auto_info(self, rule, confidence, warnings):
        a = self.cfg["auto"]
        enabled = bool(a["enabled"]) and rule.id in a["rules"]
        if not rule.auto_ok:
            return {"recommended": False, "enabled": enabled, "eligible": False,
                    "why": t("auto_no_" + rule.id, default=t("auto_no_generic"))}
        return {"recommended": True, "enabled": enabled, "eligible": enabled and confidence == "high" and not warnings,
                "why": t("auto_yes_" + rule.id, default=t("auto_yes_generic"))}

    def from_hits(self):
        hits = self.con.execute("SELECT * FROM hits WHERE scan_id=? ORDER BY bytes DESC", (self.sid,)).fetchall()
        hitset = {h["path"] for h in hits}
        groups = defaultdict(list)
        for h in hits:
            p = h["path"]
            rule = self.rules.get(h["rule"])
            if not rule or any(a in hitset for a in util.ancestors(p)):
                continue
            if self.protected(p) or self.kept_by_config(p) or not os.path.lexists(p):
                continue
            tag = self.tag_of(p)
            if tag and tag["category"] in ("keep", "paper"):
                continue
            if util.days_since(h["newest"], self.now) < rule.min_age:
                continue
            if self.procs.under(p):
                continue
            row = self.dirs.get(p)
            if rule.category == "regen" and row and row["exec_atime"] and \
                    util.days_since(row["exec_atime"], self.now) < self.recent:
                continue
            if rule.check:
                try:
                    ok, _ = rules.CHECKS[rule.check](self, p)
                except OSError:
                    ok = False
                if not ok:
                    continue
            if rule.group:
                groups[rule.id].append(h)
                continue
            reasons = [rule.custom_reason or t("r_rule_" + rule.id, default=rule.id),
                       t("r_cold", date=util.fmt_date(h["newest"], self.now),
                         days=int(util.days_since(h["newest"], self.now))), t("r_noproc")]
            warnings = []
            conf = rule.confidence
            if rule.category == "regen":
                refs = self.refs_to(p)
                names = self.name_refs(p)
                if refs:
                    warnings.append(t("w_refs_old", ex=self.ex(refs)))
                if names:
                    warnings.append(t("w_name", name=os.path.basename(p), n=len(names), ex=self.ex(names)))
                if warnings:
                    conf = "low"
            self.add_item([p], h["bytes"], rule.action, rule.category, rule.id, conf, reasons, warnings,
                          h["newest"], self.auto_info(rule, conf, warnings))
        for rid, hs in groups.items():
            total = sum(h["bytes"] for h in hs)
            if total < MB:
                continue
            rule = self.rules[rid]
            newest = max(h["newest"] for h in hs)
            reasons = [t("r_rule_" + rid, default=rid), t("r_group", n=len(hs))]
            self.add_item([h["path"] for h in hs], total, rule.action, rule.category, rid, rule.confidence, reasons,
                          [], newest, self.auto_info(rule, rule.confidence, []))

    def from_tags(self):
        for p, tag in self.tags.items():
            if self.is_covered(p) or self.protected(p) or not os.path.lexists(p):
                continue
            row = self.dirs.get(p)
            if row:
                newest, size = row["newest"], row["bytes"]
            else:
                newest, _, size = util.tree_state(p, (self.cfg["archive"]["stub_suffix"],))
            uses = self.procs.under(p)
            owner = tag["owner"] or "?"
            if tag["category"] == "scratch" and tag["expires"] and tag["expires"] <= self.now:
                if uses:
                    self.kept.append({"path": p, "bytes": size, "blockers": [t("b_proc", procs=self.proc_desc(uses))]})
                    continue
                reasons = [t("r_tag_scratch", owner=owner, date=util.fmt_date(tag["expires"], self.now))]
                if tag["note"]:
                    reasons.append(tag["note"])
                self.add_item([p], size, "delete", "scratch", "tag", "high", reasons, [], newest)
            elif tag["category"] == "regen" and util.days_since(newest, self.now) >= self.cold and not uses:
                reasons = [t("r_tag_regen", owner=owner, rebuild=tag["rebuild"] or "?"),
                           t("r_cold", date=util.fmt_date(newest, self.now), days=int(util.days_since(newest, self.now)))]
                self.add_item([p], size, "delete", "regen", "tag", "medium", reasons, [], newest,
                              rebuild=tag["rebuild"])

    def cold_walk(self):
        # a directory holding something already suggested is not suggested as a whole (no double counting);
        # its other children still are
        covered_idx = util.PathIndex([(c, None) for c in self.covered])
        self.holds_covered = lambda p: any(c != p for c, _ in covered_idx.under(p))
        for root in self.roots:
            stack = [root]
            while stack:
                p = stack.pop()
                row = self.dirs.get(p)
                if row is None or row["bytes"] < self.min_cand or self.is_covered(p) or self.protected(p):
                    continue
                if any(util.under(p, x) for x in self.no_archive):
                    continue
                d = self.depth(p)
                tag = self.tag_of(p)
                project_level = self.is_project_level(p, d)
                if tag and tag["category"] in ("keep", "paper"):
                    if project_level or p == tag["path"]:
                        self.kept.append({"path": p, "bytes": row["bytes"],
                                          "blockers": [t("b_tag_keep", owner=tag["owner"] or "?",
                                                         note=tag["note"] or tag["category"])]})
                    continue
                if self.kept_by_config(p):
                    if project_level:
                        self.kept.append({"path": p, "bytes": row["bytes"], "blockers": [t("b_keep_cfg")]})
                    continue
                if d >= self.min_depth and not self.contains_protected(p):
                    frozen = bool(tag and tag["category"] == "frozen")
                    # holding something already suggested or kept, it is never suggested whole; its children may be
                    whole_ok = not self.holds_covered(p) and not self.contains_kept(p)
                    B, W, F = self.assess(p, row, frozen=frozen, full=False)
                    if not B and not whole_ok:
                        pass
                    elif not B:
                        age = util.days_since(row["newest"], self.now)
                        conf = "high" if (not W and age >= 2 * self.cold) else "medium"
                        if any(c == "w_unpushed" for c, _ in W):
                            conf = "low"
                        reasons = [t("r_cold", date=util.fmt_date(row["newest"], self.now), days=int(age))]
                        if frozen:
                            reasons.insert(0, t("r_tag_frozen", owner=tag["owner"] or "?"))
                        reasons += [x for _, x in F]
                        self.add_item([p], row["bytes"], "archive", "cold", "cold", conf, reasons,
                                      [x for _, x in W], row["newest"])
                        continue
                    elif all(c == "b_warm" for c, _ in B):
                        # only its age keeps it out: watch it if nothing else would
                        B2, _, _ = self.assess(p, row, frozen=True, full=False)
                        if not B2 and whole_ok:
                            self.watch.append({"path": p, "bytes": row["bytes"],
                                               "cold_on": row["newest"] + self.cold * util.DAY})
                        elif project_level and B2:
                            self.kept.append({"path": p, "bytes": row["bytes"], "blockers": [x for _, x in B2[:3]]})
                    elif project_level:
                        self.kept.append({"path": p, "bytes": row["bytes"], "blockers": [x for _, x in B[:3]]})
                stack.extend(self.children.get(p, []))

    def compute(self):
        self.from_hits()
        self.from_tags()
        self.cold_walk()
        order = {"delete": 0, "archive": 1}
        self.items.sort(key=lambda i: (order.get(i["action"], 2), -i["bytes"]))
        self.kept.sort(key=lambda k: -k["bytes"])
        # a watched directory inside another watched one adds nothing
        self.watch.sort(key=lambda w: w["path"])
        pruned = []
        for w in self.watch:
            if not any(util.under(w["path"], x["path"]) for x in pruned):
                pruned.append(w)
        self.watch = sorted(pruned, key=lambda w: -w["bytes"])
        return self


def compute(con, cfg, live=True):
    return Context(con, cfg, live=live).compute()


def check_path(con, cfg, path):
    """The evidence card of one path, for `tuck check`."""
    ctx = Context(con, cfg)
    path = os.path.abspath(os.path.expanduser(path)).rstrip("/") or "/"
    row = ctx.dirs.get(path)
    if row is None:
        newest, count, size = util.tree_state(path, (cfg["archive"]["stub_suffix"],))
        row = {"newest": newest, "exec_atime": 0, "bytes": size, "files": count}
    B, W, F = [[x for _, x in lst] for lst in ctx.assess(path, row)]
    if ctx.protected(path):
        B.insert(0, t("b_protected"))
    tag = ctx.tag_of(path)
    if tag and tag["category"] in ("keep", "paper"):
        B.insert(0, t("b_tag_keep", owner=tag["owner"] or "?", note=tag["note"] or tag["category"]))
    if ctx.kept_by_config(path):
        B.insert(0, t("b_keep_cfg"))
    return {"path": path, "bytes": row["bytes"], "newest": row["newest"], "project": ctx.project_of(path),
            "tag": dict(tag) if tag else None, "blockers": B, "warnings": W, "facts": F, "ctx": ctx}
