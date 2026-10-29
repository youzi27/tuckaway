"""Plans: suggestions become a plan, the user approves it (console or web panel), then it runs.

Everything that deletes or archives goes through a plan, except deletions by rules the user switched on
for `tuck auto`. Every path is checked again right before it is touched.
"""

import json
import os
import sys
import time

from . import archive, evidence, recommend, util
from .i18n import t


class PlanError(RuntimeError):
    pass


def approval_blocked(cfg, env=None, isatty=None):
    """Why this process may not approve plans, or None."""
    env = os.environ if env is None else env
    for k in cfg["safety"]["agent_env_markers"]:
        if env.get(k):
            return t("e_agent_env", var=k)
    if isatty is None:
        isatty = sys.stdin.isatty()
    if not isatty:
        return t("e_no_tty")
    return None


def _new_id(con, now):
    base = "P" + time.strftime("%m%d", time.localtime(now))
    n = con.execute("SELECT COUNT(*) FROM plans WHERE id LIKE ?", (base + "-%",)).fetchone()[0]
    while True:
        n += 1
        pid = "%s-%02d" % (base, n)
        if not con.execute("SELECT 1 FROM plans WHERE id=?", (pid,)).fetchone():
            return pid


def _snapshot(item, ctx=None):
    snap = {}
    for p in item["paths"]:
        row = ctx.dirs.get(p) if ctx else None
        snap[p] = row["newest"] if row else util.tree_state(p)[0]
    item["snap"] = snap
    item["status"] = "pending"
    return item


def create(con, items, creator, note="", ctx=None):
    if not items:
        raise PlanError(t("e_empty_plan"))
    now = time.time()
    pid = _new_id(con, now)
    items = [_snapshot(dict(i), ctx) for i in items]
    con.execute("INSERT INTO plans(id, created, creator, note, status, items) VALUES (?,?,?,?,?,?)",
                (pid, now, creator, note, "pending", json.dumps(items, ensure_ascii=False)))
    con.commit()
    return pid


def manual_item(con, cfg, action, path):
    card = recommend.check_path(con, cfg, path)
    p = card["path"]
    if not os.path.lexists(p):
        raise PlanError(t("v_missing"))
    return {
        "id": recommend.item_id(action, [p]), "paths": [p], "bytes": card["bytes"], "action": action,
        "category": "manual", "source": "manual", "confidence": "low" if card["blockers"] else "medium",
        "reasons": card["facts"], "warnings": card["blockers"] + card["warnings"], "newest": card["newest"],
        "project": card["project"], "auto": {"recommended": False}, "rebuild": None,
    }


def get(con, pid):
    row = con.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
    if not row:
        raise PlanError(t("e_no_plan", id=pid))
    d = dict(row)
    d["items"] = json.loads(d["items"])
    return d


def all_plans(con, limit=50):
    out = []
    for row in con.execute("SELECT * FROM plans ORDER BY created DESC LIMIT ?", (limit,)):
        d = dict(row)
        d["items"] = json.loads(d["items"])
        out.append(d)
    return out


def _save(con, plan):
    con.execute("UPDATE plans SET status=?, items=?, approved_by=?, approved_at=?, finished_at=? WHERE id=?",
                (plan["status"], json.dumps(plan["items"], ensure_ascii=False), plan["approved_by"],
                 plan["approved_at"], plan["finished_at"], plan["id"]))
    con.commit()


def approve(con, pid, actor, only=None):
    plan = get(con, pid)
    if plan["status"] != "pending":
        raise PlanError(t("e_not_pending", id=pid, status=plan["status"]))
    if only:
        for it in plan["items"]:
            if it["id"] not in only:
                it["status"] = "dropped"
    plan["status"] = "approved"
    plan["approved_by"] = actor
    plan["approved_at"] = time.time()
    _save(con, plan)
    return plan


def reject(con, pid, actor):
    plan = get(con, pid)
    if plan["status"] not in ("pending", "approved"):
        raise PlanError(t("e_not_pending", id=pid, status=plan["status"]))
    plan["status"] = "rejected"
    plan["approved_by"] = actor
    plan["approved_at"] = time.time()
    _save(con, plan)
    return plan


def revalidate(cfg, uses, path, snap_newest):
    """Why path may not be touched now, or None."""
    if not os.path.lexists(path):
        return t("v_missing")
    roots = cfg["scan"]["roots"]
    if not any(util.under(path, r) for r in roots):
        return t("v_outside")
    home = os.path.expanduser("~")
    if path in roots or path in ("/", home) or len(path.strip("/").split("/")) < 2:
        return t("v_toplevel")
    if any(util.under(path, x) or util.under(x, path) for x in cfg["safety"]["protect"]):
        return t("v_protected")
    used = uses.under(path)
    if used:
        return t("b_proc", procs=", ".join("%s(%d)" % (c, p) for _, (p, c, _) in used[:3]))
    newest = util.tree_state(path, (cfg["archive"]["stub_suffix"],))[0]
    if snap_newest and newest > snap_newest + 1:
        return t("v_changed", date=util.fmt_time(newest))
    return None


def _do_item(con, cfg, it, uses, plan_id, actor, log):
    results = []
    for p in it["paths"]:
        why = revalidate(cfg, uses, p, it.get("snap", {}).get(p))
        if why:
            results.append((p, "skipped", why))
            log(t("m_skip", path=p, why=why))
            continue
        try:
            if it["action"] == "delete":
                size = util.tree_state(p)[2]
                archive.remove_path(p)
                archive.insert_record(con, time=time.time(), action="delete", path=p, project=it.get("project"),
                                      bytes=size, plan=plan_id, actor=actor, note=it.get("source"))
                results.append((p, "done", util.human(size)))
                log(t("m_deleted", path=p, size=util.human(size)))
            elif it["action"] == "archive":
                rec = archive.archive_path(con, cfg, p, it.get("project") or "misc", plan_id, actor, log)
                results.append((p, "done", rec["where"]))
                log(t("m_archived", path=p, where=rec["where"]))
            else:
                results.append((p, "skipped", it["action"]))
        except (OSError, archive.ArchiveError) as e:
            results.append((p, "failed", str(e)))
            log(t("m_failed", path=p, err=e))
    states = {r[1] for r in results}
    it["status"] = "done" if states == {"done"} else ("failed" if "failed" in states else
                                                       ("partial" if "done" in states else "skipped"))
    it["results"] = results
    return it


def run(con, cfg, pid, actor, log=print):
    plan = get(con, pid)
    if plan["status"] not in ("approved", "partial"):
        raise PlanError(t("e_not_approved", id=pid, status=plan["status"]))
    plan["status"] = "running"
    _save(con, plan)
    uses = util.PathIndex(evidence.process_uses())
    try:
        for it in plan["items"]:
            if it.get("status") in ("done", "dropped"):
                continue
            _do_item(con, cfg, it, uses, pid, actor, log)
            _save(con, plan)
    finally:
        states = {it.get("status") for it in plan["items"] if it.get("status") != "dropped"}
        plan["status"] = "done" if states <= {"done"} else "partial"
        plan["finished_at"] = time.time()
        _save(con, plan)
    return plan


def run_auto(con, cfg, dry_run=False, log=print):
    """Delete what the rules switched on in [auto] found; no approval needed for those."""
    a = cfg["auto"]
    if not a["enabled"] or not a["rules"]:
        log(t("m_auto_off"))
        return []
    ctx = recommend.compute(con, cfg)
    budget = a["max_gb_per_run"] * (1 << 30)
    done = []
    for it in ctx.items:
        if not it["auto"].get("eligible") or it["action"] != "delete":
            continue
        if it["bytes"] > budget:
            log(t("m_auto_budget", path=it["paths"][0]))
            continue
        if dry_run:
            log(t("m_would_delete", n=len(it["paths"]), size=util.human(it["bytes"]), rule=it["source"]))
            done.append(it)
            continue
        _snapshot(it, ctx)
        _do_item(con, cfg, it, ctx.procs, "auto", "auto:" + it["source"], log)
        budget -= it["bytes"]
        done.append(it)
    return done
