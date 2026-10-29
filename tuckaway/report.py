"""Reports: plain text for the console, HTML for files and the web panel."""

import html
import json
import os
import shutil

from . import __version__, db, rules, util
from .i18n import t

ACTION_ORDER = ("delete", "archive")


def disk_usage(roots):
    out, seen = [], set()
    for r in roots:
        try:
            dev = os.stat(r).st_dev
        except OSError:
            continue
        if dev in seen:
            continue
        seen.add(dev)
        u = shutil.disk_usage(r)
        # like df: used / (used + available), so the space reserved for root does not hide a full disk
        out.append({"root": r, "total": u.total, "used": u.used, "free": u.free,
                    "pct": 100.0 * u.used / (u.used + u.free) if u.used + u.free else 0})
    return out


def growth(con, ctx, hours=24):
    """Per-project growth since the scan closest to `hours` ago (at least 75% of that)."""
    prev = db.scan_near(con, ctx.scan["finished"] - hours * 3600 * 0.75)
    if not prev or prev["id"] == ctx.sid:
        return None
    old = {r["path"]: r["bytes"] for r in con.execute("SELECT path, bytes FROM dirs WHERE scan_id=?", (prev["id"],))
           if ctx.is_project_level(r["path"])}
    rows = [(p, r["bytes"] - old.get(p, 0)) for p, r in ctx.dirs.items() if ctx.is_project_level(p)]
    rows += [(p, -b) for p, b in old.items() if p not in ctx.dirs and b]
    rows.sort(key=lambda x: -abs(x[1]))
    total = ctx.scan["bytes"] - (prev["bytes"] or 0)
    return {"since": prev["finished"], "total": total, "rows": [r for r in rows if abs(r[1]) >= (64 << 20)][:8]}


def auto_suggestions(ctx):
    """Rules recommended for automatic deletion that have findings now."""
    out = {}
    for it in ctx.items:
        if it["auto"].get("recommended"):
            d = out.setdefault(it["source"], {"rule": it["source"], "n": 0, "bytes": 0, "why": it["auto"]["why"],
                                              "enabled": it["auto"]["enabled"]})
            d["n"] += len(it["paths"])
            d["bytes"] += it["bytes"]
    return sorted(out.values(), key=lambda d: -d["bytes"])


def duration(sc):
    secs = int((sc["finished"] or 0) - (sc["started"] or 0))
    return t("dur_s", n=secs) if secs < 120 else t("dur_m", n=secs // 60)


def build(con, cfg, ctx):
    items = ctx.items
    return {
        "version": __version__,
        "scan": dict(ctx.scan),
        "disks": disk_usage(ctx.roots),
        "growth": growth(con, ctx),
        "items": items,
        "delete_total": sum(i["bytes"] for i in items if i["action"] == "delete"),
        "archive_total": sum(i["bytes"] for i in items if i["action"] == "archive"),
        "auto": auto_suggestions(ctx),
        "watch": ctx.watch[:12],
        "kept": ctx.kept[:12],
        "short": ctx.short,
        "now": ctx.now,
        "cfg": cfg,
    }


# -- text -------------------------------------------------------------------------------------------------

def _conf(c):
    return t("conf_" + c)


def item_lines(d, it, limit_paths=3):
    sh = d["short"]
    head = "[%s] %s %7s  %s" % (it["id"], t("act_" + it["action"]), util.human(it["bytes"]), sh(it["paths"][0]))
    if len(it["paths"]) > 1:
        head += t("and_more", n=len(it["paths"]) - 1)
    lines = [head, "      " + t("item_meta", cat=t("cat_" + it["category"]), src=it["source"],
                                    conf=_conf(it["confidence"]), project=it["project"])]
    for r in it["reasons"]:
        lines.append("      + " + r)
    for w in it["warnings"]:
        lines.append("      ! " + w)
    if it.get("rebuild"):
        lines.append("      " + t("rebuild", cmd=it["rebuild"]))
    a = it["auto"]
    if a.get("recommended"):
        lines.append("      " + t("auto_line_on" if a.get("enabled") else "auto_line_rec", why=a["why"]))
    elif a.get("why"):
        lines.append("      " + t("auto_line_no", why=a["why"]))
    return lines


def text(d, max_items=40):
    out = []
    sc = d["scan"]
    out.append(t("rep_title", v=d["version"], when=util.fmt_time(d["now"])))
    out.append(t("rep_scan", when=util.fmt_time(sc["finished"]), dur=duration(sc), files=sc["files"],
                 size=util.human(sc["bytes"])))
    for k in d["disks"]:
        out.append(t("rep_disk", root=k["root"], total=util.human(k["total"]), used=util.human(k["used"]),
                     pct=k["pct"], free=util.human(k["free"])))
    g = d["growth"]
    if g:
        out.append(t("rep_growth", since=util.fmt_time(g["since"]), delta=("+" if g["total"] >= 0 else "") +
                     util.human(g["total"])))
        for p, b in g["rows"]:
            out.append("    %s%-8s %s" % ("+" if b >= 0 else "-", util.human(abs(b)), d["short"](p)))
    out.append("")
    n_del = len([i for i in d["items"] if i["action"] == "delete"])
    n_arc = len([i for i in d["items"] if i["action"] == "archive"])
    out.append(t("rep_summary", ndel=n_del, del_=util.human(d["delete_total"]), narc=n_arc,
                 arc=util.human(d["archive_total"])))
    for action in ACTION_ORDER:
        its = [i for i in d["items"] if i["action"] == action]
        if not its:
            continue
        out.append("")
        out.append("== " + t("sec_" + action) + " ==")
        for it in its[:max_items]:
            out.extend(item_lines(d, it))
        if len(its) > max_items:
            out.append(t("more_items", n=len(its) - max_items))
    if d["auto"]:
        out.append("")
        out.append("== " + t("sec_auto") + " ==")
        for a in d["auto"]:
            out.append("  %-22s %3d  %8s  %s" % (a["rule"], a["n"], util.human(a["bytes"]),
                                                  t("on") if a["enabled"] else t("off")))
            out.append("      " + a["why"])
        if any(not a["enabled"] for a in d["auto"]):
            out.append("  " + t("auto_howto", rules=", ".join('"%s"' % a["rule"] for a in d["auto"])))
    if d["watch"]:
        out.append("")
        out.append("== " + t("sec_watch") + " ==")
        for w in d["watch"]:
            out.append("  %8s  %s  %s" % (util.human(w["bytes"]), d["short"](w["path"]),
                                          t("cold_on", date=util.fmt_date(w["cold_on"], d["now"]))))
    if d["kept"]:
        out.append("")
        out.append("== " + t("sec_kept") + " ==")
        for k in d["kept"]:
            out.append("  %8s  %s" % (util.human(k["bytes"]), d["short"](k["path"])))
            for b in k["blockers"]:
                out.append("      - " + b)
    out.append("")
    out.append(t("next_steps"))
    return "\n".join(out)


# -- html -------------------------------------------------------------------------------------------------

CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e2dd;--del:#b4372f;--arc:#2f6db4;
--ok:#2e7d4f;--warn:#a86b00;--chip:#efeee9}
@media (prefers-color-scheme:dark){:root{--bg:#171716;--fg:#ecebe6;--muted:#a3a29b;--card:#222220;
--line:#34332f;--del:#e0675e;--arc:#6fa3e6;--ok:#62b585;--warn:#d9a441;--chip:#2c2b28}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Noto Sans CJK SC",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:17px;margin:28px 0 10px}.muted{color:var(--muted)}.grid{display:grid;
grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:14px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat b{display:block;font-size:22px}.card{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:12px 14px;margin:8px 0}.card .top{display:flex;gap:10px;align-items:baseline;
flex-wrap:wrap}.path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;word-break:break-all}
.size{font-weight:600;min-width:64px}.act{font-size:12px;padding:0 6px;border-radius:4px;border:1px solid}
.act.delete{color:var(--del)}.act.archive{color:var(--arc)}.sel{font-size:13px;margin-left:10px;padding:2px 10px}.chip{font-size:12px;background:var(--chip);
padding:1px 8px;border-radius:99px;color:var(--muted)}ul{margin:6px 0 0;padding-left:20px}
li.r{color:var(--ok)}li.w{color:var(--warn)}li.r span,li.w span{color:var(--fg)}
.auto{font-size:13px;color:var(--muted);margin-top:4px}table{border-collapse:collapse;width:100%}
td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}
button{font:inherit;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);
color:var(--fg);cursor:pointer}button.primary{background:var(--arc);color:#fff;border-color:var(--arc)}
button.danger{background:var(--del);color:#fff;border-color:var(--del)}.bar{position:sticky;bottom:0;
background:var(--bg);padding:10px 0;border-top:1px solid var(--line);display:flex;gap:10px;align-items:center;
flex-wrap:wrap}nav a{margin-right:14px;color:var(--arc)}.note{background:var(--chip);border-radius:8px;
padding:8px 12px;margin:10px 0}input[type=checkbox]{transform:scale(1.2);margin-right:6px}
"""


PANEL_JS = """<script>
function tuckSel(){var b=document.querySelectorAll("input[name=ids]:checked"),s=0;
 b.forEach(function(x){s+=+x.dataset.bytes});return [b.length,s]}
function tuckHuman(n){var u=["B","K","M","G","T"],i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i?1:0)+u[i]}
function tuckFmt(m,r){return m.replace("{n}",r[0]).replace("{size}",tuckHuman(r[1]))}
function tuckUpd(){var e=document.getElementById("tucksel");if(e)e.textContent=tuckFmt(%(sel)s,tuckSel())}
function tuckAll(a){var b=document.querySelectorAll("input[name=ids][data-action="+a+"]"),on=false;
 b.forEach(function(x){if(!x.checked)on=true});b.forEach(function(x){x.checked=on});tuckUpd()}
function tuckConfirm(){var r=tuckSel();if(!r[0]){alert(%(none)s);return false}return confirm(tuckFmt(%(confirm)s,r))}
document.addEventListener("change",tuckUpd);
</script>"""


def esc(s):
    return html.escape(str(s), quote=True)


def js_str(s):
    return json.dumps(s, ensure_ascii=False)


def page(title, body, nav=None, head=""):
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>%s<title>%s</title><style>%s</style>"
            "</head><body><main>%s%s</main></body></html>" % (head, esc(title), CSS, nav or "", body))


def item_card(d, it, selectable=False, checked=False):
    sh = d["short"]
    paths = it["paths"]
    p0 = esc(sh(paths[0])) + (esc(t("and_more", n=len(paths) - 1)) if len(paths) > 1 else "")
    box = "<input type='checkbox' name='ids' value='%s' data-bytes='%d' data-action='%s'%s>" % (
        esc(it["id"]), it["bytes"], esc(it["action"]), " checked" if checked else "") if selectable else ""
    lis = "".join("<li class='r'><span>%s</span></li>" % esc(r) for r in it["reasons"])
    lis += "".join("<li class='w'><span>%s</span></li>" % esc(w) for w in it["warnings"])
    if it.get("rebuild"):
        lis += "<li><span>%s</span></li>" % esc(t("rebuild", cmd=it["rebuild"]))
    a = it["auto"]
    auto = ""
    if a.get("recommended"):
        auto = t("auto_line_on" if a.get("enabled") else "auto_line_rec", why=a["why"])
    elif a.get("why"):
        auto = t("auto_line_no", why=a["why"])
    st = it.get("status")
    status = " <span class='chip'>%s</span>" % esc(t("st_" + st, default=st)) if st else ""
    return ("<div class='card'><div class='top'><label>%s<span class='act %s'>%s</span></label>"
            "<span class='size'>%s</span><span class='path'>%s</span>%s</div>"
            "<div class='muted' style='font-size:13px'>%s</div><ul>%s</ul>%s</div>") % (
        box, esc(it["action"]), esc(t("act_" + it["action"])), esc(util.human(it["bytes"])), p0, status,
        esc(t("item_meta", cat=t("cat_" + it["category"]), src=it["source"], conf=_conf(it["confidence"]),
              project=it["project"]) + " · id " + it["id"]),
        lis, "<div class='auto'>%s</div>" % esc(auto) if auto else "")


def html_report(d, panel=False, token="", can_run=True):
    sc = d["scan"]
    parts = ["<h1>tuckaway</h1><div class='muted'>%s</div>" % esc(
        t("rep_scan", when=util.fmt_time(sc["finished"]), dur=duration(sc), files=sc["files"],
          size=util.human(sc["bytes"])))]
    stats = []
    for k in d["disks"]:
        stats.append("<div class='stat'><span class='muted'>%s</span><b>%.0f%%</b>%s</div>" % (
            esc(k["root"]), k["pct"], esc(t("free_of", free=util.human(k["free"]), total=util.human(k["total"])))))
    stats.append("<div class='stat'><span class='muted'>%s</span><b>%s</b>%s</div>" % (
        esc(t("sec_delete")), esc(util.human(d["delete_total"])),
        esc(t("n_items", n=len([i for i in d["items"] if i["action"] == "delete"])))))
    stats.append("<div class='stat'><span class='muted'>%s</span><b>%s</b>%s</div>" % (
        esc(t("sec_archive")), esc(util.human(d["archive_total"])),
        esc(t("n_items", n=len([i for i in d["items"] if i["action"] == "archive"])))))
    g = d["growth"]
    if g:
        stats.append("<div class='stat'><span class='muted'>%s</span><b>%s%s</b>%s</div>" % (
            esc(t("growth_label")), "+" if g["total"] >= 0 else "", esc(util.human(g["total"])),
            esc(t("since", when=util.fmt_time(g["since"])))))
    parts.append("<div class='grid'>%s</div>" % "".join(stats))
    if g and g["rows"]:
        parts.append("<h2>%s</h2><table>%s</table>" % (esc(t("growth_label")), "".join(
            "<tr><td class='size'>%s%s</td><td class='path'>%s</td></tr>" % (
                "+" if b >= 0 else "-", esc(util.human(abs(b))), esc(d["short"](p))) for p, b in g["rows"])))
    if panel:
        parts.append("<form method='post' action='/plan/new'><input type='hidden' name='t' value='%s'>" % esc(token))
    for action in ACTION_ORDER:
        its = [i for i in d["items"] if i["action"] == action]
        if its:
            sel = ("<button type='button' class='sel' onclick=\"tuckAll('%s')\">%s</button>" % (
                action, esc(t("btn_select_all")))) if panel else ""
            parts.append("<h2>%s%s</h2>" % (esc(t("sec_" + action)), sel))
            parts.extend(item_card(d, it, selectable=panel) for it in its)
    if panel:
        dis = "" if can_run else " disabled"
        parts.append(
            "<div class='bar'><button class='danger' type='submit' name='go' value='run'%s "
            "onclick='return tuckConfirm()'>%s</button><button type='submit' name='go' value='plan'>%s</button>"
            "<span class='muted' id='tucksel'>%s</span></div><p class='muted'>%s</p></form>" % (
                dis, esc(t("btn_run_selected")), esc(t("btn_plan_only")), esc(t("sel_count", n=0, size="0B")),
                esc(t("hint_make_plan"))))
        parts.append(PANEL_JS % {"sel": js_str(t("sel_count")), "none": js_str(t("sel_none")),
                                 "confirm": js_str(t("confirm_run"))})
    if d["auto"]:
        rows = "".join("<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(a["rule"]), a["n"], esc(util.human(a["bytes"])), esc(t("on") if a["enabled"] else t("off")),
            esc(a["why"])) for a in d["auto"])
        parts.append("<h2>%s</h2><table>%s</table>" % (esc(t("sec_auto")), rows))
        if any(not a["enabled"] for a in d["auto"]):
            parts.append("<div class='note'>%s</div>" % esc(
                t("auto_howto", rules=", ".join('"%s"' % a["rule"] for a in d["auto"]))))
    if d["watch"]:
        parts.append("<h2>%s</h2><table>%s</table>" % (esc(t("sec_watch")), "".join(
            "<tr><td class='size'>%s</td><td class='path'>%s</td><td>%s</td></tr>" % (
                esc(util.human(w["bytes"])), esc(d["short"](w["path"])),
                esc(t("cold_on", date=util.fmt_date(w["cold_on"], d["now"])))) for w in d["watch"])))
    if d["kept"]:
        parts.append("<h2>%s</h2><table>%s</table>" % (esc(t("sec_kept")), "".join(
            "<tr><td class='size'>%s</td><td><div class='path'>%s</div><div class='muted'>%s</div></td></tr>" % (
                esc(util.human(k["bytes"])), esc(d["short"](k["path"])), esc("；".join(k["blockers"])))
            for k in d["kept"])))
    return "".join(parts)
