"""Command line: tuck <command> ..."""

import argparse
import getpass
import json
import os
import shutil
import sqlite3
import sys
import time

from . import __version__, archive, config, db, i18n, plans, recommend, report, rules, scan, util
from .i18n import t

CATEGORIES = ("scratch", "regen", "frozen", "keep", "paper")


def actor():
    who = getpass.getuser()
    owner = os.environ.get("TUCK_OWNER")
    return "%s:%s" % (who, owner) if owner else who


def need_scan(con):
    if not db.latest_scan(con):
        print(t("e_no_scan"))
        raise SystemExit(1)


def cmd_init(cfg, a):
    path = cfg["_path"]
    if os.path.exists(path) and not a.force:
        print(t("c_init_exists", path=path))
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    src = os.path.join(os.path.dirname(__file__), "config_template.toml")
    shutil.copyfile(src, path)
    print(t("c_init_done", path=path))
    return 0


def cmd_scan(cfg, a):
    con = db.connect(cfg)
    print(t("c_scanning", roots=", ".join(cfg["scan"]["roots"])))
    t0 = time.time()
    try:
        sid = scan.scan(cfg, verbose=a.verbose)
    except scan.ScanBusy:
        print(t("c_scan_busy"))
        return 1
    row = con.execute("SELECT * FROM scans WHERE id=?", (sid,)).fetchone()
    print(t("c_scan_done", id=sid, files=row["files"], size=util.human(row["bytes"]), secs=int(time.time() - t0),
            errors=row["errors"]))
    return 0


def cmd_report(cfg, a):
    con = db.connect(cfg)
    need_scan(con)
    ctx = recommend.compute(con, cfg)
    d = report.build(con, cfg, ctx)
    if a.html:
        with open(a.html, "w", encoding="utf-8") as f:
            f.write(report.page("tuckaway", report.html_report(d)))
        print(t("c_wrote", path=a.html))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"items": ctx.items, "watch": ctx.watch, "kept": ctx.kept}, f, ensure_ascii=False, indent=1)
        print(t("c_wrote", path=a.json))
    if not a.html and not a.json:
        print(report.text(d, max_items=a.max))
    return 0


def cmd_check(cfg, a):
    con = db.connect(cfg)
    need_scan(con)
    rc = 0
    for p in a.paths:
        card = recommend.check_path(con, cfg, p)
        ctx = card["ctx"]
        print(t("c_check_title", path=ctx.short(card["path"]), size=util.human(card["bytes"]),
                date=util.fmt_date(card["newest"], ctx.now), project=card["project"]))
        if card["tag"]:
            tg = card["tag"]
            print("  " + t("c_check_tag", cat=tg["category"], owner=tg["owner"] or "?", note=tg["note"] or "",
                           path=ctx.short(tg["path"])))
        for b in card["blockers"]:
            print("  - " + b)
        for w in card["warnings"]:
            print("  ! " + w)
        for f in card["facts"]:
            print("  + " + f)
        if card["blockers"]:
            print("  => " + t("c_verdict_block"))
            rc = 2
        else:
            print("  => " + t("c_verdict_ok"))
    return rc


def cmd_tag(cfg, a):
    con = db.connect(cfg)
    path = os.path.abspath(os.path.expanduser(a.path)).rstrip("/")
    if not os.path.lexists(path):
        print(t("v_missing") + ": " + path)
        return 1
    expires = util.parse_expiry(a.expires) if a.expires else None
    con.execute("INSERT OR REPLACE INTO tags(path, category, project, owner, note, expires, rebuild, created)"
                " VALUES (?,?,?,?,?,?,?,?)", (path, a.cat, a.project, a.owner or actor(), a.note, expires,
                                              a.rebuild, time.time()))
    con.commit()
    print(t("c_tagged", path=path, cat=a.cat))
    return 0


def cmd_untag(cfg, a):
    con = db.connect(cfg)
    path = os.path.abspath(os.path.expanduser(a.path)).rstrip("/")
    n = con.execute("DELETE FROM tags WHERE path=?", (path,)).rowcount
    con.commit()
    print(t("c_untagged", path=path) if n else t("c_no_tag", path=path))
    return 0


def cmd_tags(cfg, a):
    con = db.connect(cfg)
    q = "SELECT * FROM tags" + (" WHERE project=?" if a.project else "") + " ORDER BY path"
    for r in con.execute(q, (a.project,) if a.project else ()):
        exp = (" -> " + util.fmt_date(r["expires"])) if r["expires"] else ""
        print("%-7s %-14s %-16s %s%s  %s" % (r["category"], (r["project"] or "-")[:14], (r["owner"] or "-")[:16],
                                             r["path"], exp, r["note"] or ""))
    return 0


def print_plan(cfg, p):
    d = {"short": lambda x: x.replace(os.path.expanduser("~"), "~", 1)}
    print(t("plan_meta", created=util.fmt_time(p["created"]), creator=p["creator"],
            status=t("st_" + p["status"], default=p["status"]), by=p["approved_by"] or "-") + "  [" + p["id"] + "]")
    for it in p["items"]:
        lines = report.item_lines(d, it)
        if it.get("status") and it["status"] != "pending":
            lines[0] += "   <%s>" % t("st_" + it["status"], default=it["status"])
        print("\n".join(lines))
        for path, st, msg in it.get("results", []):
            print("      %s %s %s" % (st, path, msg))


def cmd_plan(cfg, a):
    con = db.connect(cfg)
    if a.plan_cmd == "show":
        print_plan(cfg, plans.get(con, a.id))
        return 0
    if a.plan_cmd == "add":
        items = [plans.manual_item(con, cfg, a.action, p) for p in a.paths]
        pid = plans.create(con, items, actor(), a.note or "")
    else:
        need_scan(con)
        ctx = recommend.compute(con, cfg)
        items = ctx.items
        if a.ids:
            ids = set(a.ids.split(","))
            items = [i for i in items if i["id"] in ids]
        elif a.action:
            items = [i for i in items if i["action"] == a.action]
        elif not a.all:
            print(t("c_plan_pick"))
            return 1
        pid = plans.create(con, items, actor(), a.note or "", ctx=ctx)
    p = plans.get(con, pid)
    print(t("c_plan_created", id=pid, n=len(p["items"]), size=util.human(sum(i["bytes"] for i in p["items"]))))
    print(t("c_plan_hint", id=pid))
    return 0


def cmd_plans(cfg, a):
    con = db.connect(cfg)
    for p in plans.all_plans(con, a.limit):
        live = [i for i in p["items"] if i.get("status") != "dropped"]
        print("%-10s %s  %-9s %3d  %8s  %s" % (p["id"], util.fmt_time(p["created"]),
                                               t("st_" + p["status"], default=p["status"]), len(live),
                                               util.human(sum(i["bytes"] for i in live)), p["creator"]))
    return 0


def cmd_approve(cfg, a):
    why = plans.approval_blocked(cfg)
    if why:
        print(why)
        return 3
    con = db.connect(cfg)
    p = plans.get(con, a.id)
    print_plan(cfg, p)
    only = set(a.only.split(",")) if a.only else None
    answer = input(t("c_confirm", id=a.id)).strip()
    if answer != a.id:
        print(t("c_not_confirmed"))
        return 1
    plans.approve(con, a.id, "console:" + getpass.getuser(), only=only)
    print(t("c_approved", id=a.id))
    if input(t("c_run_now")).strip().lower() in ("y", "yes", "是", "好"):
        plans.run(con, cfg, a.id, "console:" + getpass.getuser())
    return 0


def cmd_reject(cfg, a):
    con = db.connect(cfg)
    plans.reject(con, a.id, actor())
    print(t("c_rejected", id=a.id))
    return 0


def cmd_run(cfg, a):
    con = db.connect(cfg)
    p = plans.run(con, cfg, a.id, a.actor or actor(), log=lambda m: print(m, flush=True))
    print(t("c_run_done", id=a.id, status=t("st_" + p["status"], default=p["status"])))
    return 0


def cmd_auto(cfg, a):
    con = db.connect(cfg)
    need_scan(con)
    done = plans.run_auto(con, cfg, dry_run=a.dry_run, log=lambda m: print(m, flush=True))
    print(t("c_auto_done", n=len(done), size=util.human(sum(i["bytes"] for i in done))))
    return 0


def cmd_restore(cfg, a):
    con = db.connect(cfg)
    for p in a.paths:
        rec = archive.restore(con, cfg, p, actor(), log=lambda m: print(m, flush=True))
        print(t("c_restored", path=rec["path"]))
    return 0


def cmd_where(cfg, a):
    con = db.connect(cfg)
    like = "%" + a.text + "%"
    rows = con.execute("SELECT * FROM records WHERE path LIKE ? OR location LIKE ? ORDER BY time DESC LIMIT 200",
                       (like, like)).fetchall()
    for r in rows:
        restored = r["action"] == "archive" and archive.find_record(con, r["path"]) is None
        print("%s  %-8s %s\n      %s %s%s" % (util.fmt_time(r["time"]), t("act_" + r["action"], default=r["action"]),
                                              r["path"], r["backend"] or "", r["location"] or "",
                                              ("  (" + t("restored") + ")") if restored else ""))
    if not rows:
        print(t("c_nothing"))
    return 0


def cmd_records(cfg, a):
    con = db.connect(cfg)
    if a.import_md:
        n = archive.import_markdown(con, os.path.expanduser(a.import_md), a.backend)
        print(t("c_imported", n=n))
        return 0
    for r in con.execute("SELECT * FROM records ORDER BY time DESC LIMIT ?", (a.limit,)):
        print("%s  %-8s %8s  %s  %s" % (util.fmt_time(r["time"]), t("act_" + r["action"], default=r["action"]),
                                        util.human(r["bytes"]) if r["bytes"] else "", r["path"], r["location"] or ""))
    return 0


def cmd_rules(cfg, a):
    for r in rules.load_rules(cfg):
        print("%-22s %-8s %-7s %s" % (r.id, r.category, t("conf_" + r.confidence),
                                       t("auto_yes_short") if r.auto_ok else t("auto_no_short")))
        print("      " + (r.custom_reason or t("r_rule_" + r.id, default=r.id)))
        why = t(("auto_yes_" if r.auto_ok else "auto_no_") + r.id, default="")
        if why:
            print("      " + why)
    return 0


def cmd_serve(cfg, a):
    from . import web
    web.serve(cfg, a.host, a.port)
    return 0


def parser():
    ap = argparse.ArgumentParser(prog="tuck", description=t("help_main"))
    ap.add_argument("--config", help=t("help_config"))
    ap.add_argument("--version", action="version", version="tuckaway " + __version__)
    sp = ap.add_subparsers(dest="cmd")

    p = sp.add_parser("init", help=t("help_init"))
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("scan", help=t("help_scan"))
    p.add_argument("-v", "--verbose", action="store_true")
    p = sp.add_parser("report", help=t("help_report"))
    p.add_argument("--html")
    p.add_argument("--json")
    p.add_argument("--max", type=int, default=40)
    p = sp.add_parser("check", help=t("help_check"))
    p.add_argument("paths", nargs="+")
    p = sp.add_parser("tag", help=t("help_tag"))
    p.add_argument("path")
    p.add_argument("--cat", required=True, choices=CATEGORIES)
    p.add_argument("--project")
    p.add_argument("--owner")
    p.add_argument("--note")
    p.add_argument("--expires", help="7d, 12h, 2w, YYYY-MM-DD")
    p.add_argument("--rebuild")
    p = sp.add_parser("untag", help=t("help_untag"))
    p.add_argument("path")
    p = sp.add_parser("tags", help=t("help_tags"))
    p.add_argument("--project")

    p = sp.add_parser("plan", help=t("help_plan"))
    pp = p.add_subparsers(dest="plan_cmd", required=True)
    q = pp.add_parser("new")
    q.add_argument("--ids")
    q.add_argument("--all", action="store_true")
    q.add_argument("--action", choices=("delete", "archive"))
    q.add_argument("--note")
    q = pp.add_parser("add")
    q.add_argument("action", choices=("delete", "archive"))
    q.add_argument("paths", nargs="+")
    q.add_argument("--note")
    q = pp.add_parser("show")
    q.add_argument("id")
    p = sp.add_parser("plans", help=t("help_plans"))
    p.add_argument("--limit", type=int, default=30)
    p = sp.add_parser("approve", help=t("help_approve"))
    p.add_argument("id")
    p.add_argument("--only", help="item ids, comma separated")
    p = sp.add_parser("reject", help=t("help_reject"))
    p.add_argument("id")
    p = sp.add_parser("run", help=t("help_run"))
    p.add_argument("id")
    p.add_argument("--actor")
    p = sp.add_parser("auto", help=t("help_auto"))
    p.add_argument("--dry-run", action="store_true")
    p = sp.add_parser("restore", help=t("help_restore"))
    p.add_argument("paths", nargs="+")
    p = sp.add_parser("where", help=t("help_where"))
    p.add_argument("text")
    p = sp.add_parser("records", help=t("help_records"))
    p.add_argument("--import-md", metavar="FILE")
    p.add_argument("--backend", default="ssh", choices=("ssh", "local"))
    p.add_argument("--limit", type=int, default=50)
    sp.add_parser("rules", help=t("help_rules"))
    p = sp.add_parser("serve", help=t("help_serve"))
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    return ap


COMMANDS = {
    "init": cmd_init, "scan": cmd_scan, "report": cmd_report, "check": cmd_check, "tag": cmd_tag,
    "untag": cmd_untag, "tags": cmd_tags, "plan": cmd_plan, "plans": cmd_plans, "approve": cmd_approve,
    "reject": cmd_reject, "run": cmd_run, "auto": cmd_auto, "restore": cmd_restore, "where": cmd_where,
    "records": cmd_records, "rules": cmd_rules, "serve": cmd_serve,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # the language must be known before the help texts are built
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    known, _ = pre.parse_known_args(argv)
    try:
        cfg = config.load(config.expand(known.config) if known.config else None)
    except config.ConfigError as e:
        print("tuck: %s" % e, file=sys.stderr)
        return 2
    i18n.set_lang(cfg["lang"])
    a = parser().parse_args(argv)
    if not a.cmd:
        parser().print_help()
        return 0
    try:
        return COMMANDS[a.cmd](cfg, a) or 0
    except (plans.PlanError, archive.ArchiveError, recommend.NoScan, ValueError) as e:
        print("tuck: %s" % (str(e) or t("e_no_scan")), file=sys.stderr)
        return 1
    except sqlite3.OperationalError as e:
        print("tuck: database: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
