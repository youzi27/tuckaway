"""The web panel: the report with checkboxes, plans with approve / reject buttons, and the records.

It listens on 127.0.0.1 by default and every request needs the token printed when it starts. When the panel
was started from an AI agent's shell, approving and running are switched off.

Suggestions take a while to compute on a big disk, so they are computed in a background thread and pages are
served from the last result; only the report page waits for the first one.
"""

import getpass
import os
import secrets
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db, plans, recommend, report, util
from .i18n import t

esc = report.esc
FRESH = 600     # seconds a computed report is served before it is recomputed in the background


def short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if util.under(path, home) else path


class Panel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = secrets.token_urlsafe(18)
        self.blocked = plans.approval_blocked(cfg, isatty=True)
        self.cache = None
        self.cache_at = 0
        self.error = None
        self.computing = False
        self.lock = threading.Lock()
        self.user = getpass.getuser()

    def _compute(self):
        try:
            con = db.connect(self.cfg)
            ctx = recommend.compute(con, self.cfg)
            self.cache = (ctx, report.build(con, self.cfg, ctx))
            self.cache_at = time.time()
            self.error = None
        except Exception as e:     # shown on the page instead of killing the thread silently
            self.error = str(e) or t("e_no_scan")
        finally:
            self.computing = False

    def refresh(self, force=False):
        """Start a background computation when the result is missing or old."""
        with self.lock:
            if self.computing:
                return
            if force or not self.cache or time.time() - self.cache_at > FRESH:
                self.computing = True
                threading.Thread(target=self._compute, daemon=True).start()

    def data(self):
        self.refresh()
        return self.cache

    def nav(self):
        return ("<nav class='muted' style='margin-bottom:14px'><a href='/'>%s</a><a href='/plans'>%s</a>"
                "<a href='/records'>%s</a></nav>") % (esc(t("nav_report")), esc(t("nav_plans")), esc(t("nav_records")))

    def spawn_run(self, pid, actor):
        logdir = os.path.join(self.cfg["state_dir"], "logs")
        os.makedirs(logdir, exist_ok=True)
        log = open(os.path.join(logdir, "%s.log" % pid), "a")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + \
            (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        if self.cfg.get("_path"):
            env["TUCK_CONFIG"] = self.cfg["_path"]
        subprocess.Popen([sys.executable, "-m", "tuckaway", "run", pid, "--actor", actor], stdout=log,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        log.close()


def make_handler(panel):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _authed(self, form=None):
            q = parse_qs(urlparse(self.path).query)
            c = SimpleCookie(self.headers.get("Cookie", ""))
            got = (q.get("t") or [None])[0] or (form or {}).get("t", [None])[0] or \
                (c["tuck_token"].value if "tuck_token" in c else None)
            return got and secrets.compare_digest(got, panel.token)

        def _send(self, body, status=200, cookie=False):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", "tuck_token=%s; HttpOnly; SameSite=Strict; Path=/" % panel.token)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, where):
            self.send_response(303)
            self.send_header("Location", where)
            self.end_headers()

        def do_GET(self):
            if not self._authed():
                return self._send(report.page("tuckaway", "<p>%s</p>" % esc(t("web_need_token"))), 403)
            url = urlparse(self.path)
            q = parse_qs(url.query)
            cookie = "t" in q
            try:
                if url.path == "/":
                    return self._send(self.report_page(force="refresh" in q), cookie=cookie)
                if url.path == "/plans":
                    return self._send(report.page("tuckaway", self.plans_page(), panel.nav()), cookie=cookie)
                if url.path.startswith("/plan/"):
                    pid = url.path.split("/")[2]
                    body, live = self.plan_page(pid)
                    head = "<meta http-equiv='refresh' content='3'>" if live else ""
                    return self._send(report.page(pid, body, panel.nav(), head=head), cookie=cookie)
                if url.path == "/records":
                    return self._send(report.page("tuckaway", self.records_page(), panel.nav()), cookie=cookie)
            except plans.PlanError as e:
                return self._send(report.page("tuckaway", "<p>%s</p>" % esc(str(e)), panel.nav()))
            self._send(report.page("404", "<p>404</p>", panel.nav()), 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            form = parse_qs(self.rfile.read(n).decode("utf-8"))
            if not self._authed(form) or (form.get("t") or [""])[0] != panel.token:
                return self._send(report.page("tuckaway", "<p>%s</p>" % esc(t("web_need_token"))), 403)
            parts = urlparse(self.path).path.strip("/").split("/")
            con = db.connect(panel.cfg)
            who = "web:" + panel.user
            try:
                if parts == ["plan", "new"]:
                    ids = set(form.get("ids", []))
                    cache = panel.cache
                    if not cache:
                        return self._redirect("/")
                    ctx = cache[0]
                    items = [i for i in ctx.items if i["id"] in ids]
                    pid = plans.create(con, items, who, ctx=ctx)
                    if (form.get("go") or [""])[0] == "run" and not panel.blocked:
                        plans.approve(con, pid, who)
                        panel.spawn_run(pid, who)
                    return self._redirect("/plan/" + pid)
                if len(parts) == 3 and parts[0] == "plan":
                    pid, op = parts[1], parts[2]
                    if op in ("approve", "approve_run", "run") and panel.blocked:
                        return self._send(report.page(pid, "<p>%s</p>" % esc(panel.blocked), panel.nav()), 403)
                    if op in ("approve", "approve_run"):
                        only = set(form.get("ids", []))
                        plans.approve(con, pid, who, only=only or None)
                        if op == "approve_run":
                            panel.spawn_run(pid, who)
                    elif op == "reject":
                        plans.reject(con, pid, who)
                    elif op == "run":
                        if plans.get(con, pid)["status"] in ("approved", "partial"):
                            panel.spawn_run(pid, who)
                    return self._redirect("/plan/" + pid)
            except plans.PlanError as e:
                return self._send(report.page("tuckaway", "<p>%s</p>" % esc(str(e)), panel.nav()), 400)
            self._send(report.page("404", "<p>404</p>", panel.nav()), 404)

        def report_page(self, force=False):
            panel.refresh(force=force)
            cache = panel.cache
            if not cache:
                msg = panel.error if (panel.error and not panel.computing) else t("web_computing")
                head = "" if panel.error and not panel.computing else "<meta http-equiv='refresh' content='5'>"
                return report.page("tuckaway", "<h1>tuckaway</h1><div class='note'>%s</div>" % esc(msg),
                                   panel.nav(), head=head)
            _, d = cache
            body = ""
            if panel.blocked:
                body += "<div class='note'>%s</div>" % esc(t("web_agent_note", why=panel.blocked))
            note = t("web_stale", when=util.fmt_time(panel.cache_at)) if panel.computing else \
                t("web_as_of", when=util.fmt_time(panel.cache_at))
            note = "<div class='muted' style='font-size:13px'>%s <a href='/?refresh=1'>%s</a></div>" % (
                esc(note), esc(t("web_refresh")))
            body += report.html_report(d, panel=True, token=panel.token, can_run=not panel.blocked, note=note)
            return report.page("tuckaway", body, panel.nav())

        def plans_page(self):
            con = db.connect(panel.cfg)
            rows = []
            for p in plans.all_plans(con):
                live = [i for i in p["items"] if i.get("status") != "dropped"]
                rows.append("<tr><td><a href='/plan/%s'>%s</a></td><td>%s</td><td>%s</td><td>%d</td><td>%s</td>"
                            "<td>%s</td></tr>" % (esc(p["id"]), esc(p["id"]), esc(util.fmt_time(p["created"])),
                                                  esc(t("st_" + p["status"], default=p["status"])), len(live),
                                                  esc(util.human(sum(i["bytes"] for i in live))), esc(p["creator"])))
            return "<h1>%s</h1><table>%s</table>" % (esc(t("nav_plans")), "".join(rows) or
                                                     "<tr><td>%s</td></tr>" % esc(t("no_plans")))

        def plan_page(self, pid):
            """(html, still running) for one plan. Never waits for the suggestions to be computed."""
            con = db.connect(panel.cfg)
            p = plans.get(con, pid)
            d = {"short": short}
            live = p["status"] in ("approved", "running")
            head = "<h1>%s</h1><div class='muted'>%s</div>" % (esc(pid), esc(t(
                "plan_meta", created=util.fmt_time(p["created"]), creator=p["creator"],
                status=t("st_" + p["status"], default=p["status"]), by=p["approved_by"] or "-")))
            if live:
                head += "<div class='note'>%s</div>" % esc(t("web_running"))
            pending = p["status"] == "pending"
            cards = "".join(report.item_card(d, it, selectable=pending, checked=pending) for it in p["items"])
            body = head
            for it in p["items"]:
                for path, st, msg in it.get("results", []):
                    body += "<div class='muted path'>%s: %s %s</div>" % (
                        esc(t("st_" + st, default=st)), esc(short(path)), esc(msg))
            if pending:
                tok = "<input type='hidden' name='t' value='%s'>" % esc(panel.token)
                if panel.blocked:
                    body += "<div class='note'>%s</div>" % esc(t("web_agent_note", why=panel.blocked))
                body += "<form method='post' action='/plan/%s/approve_run'>%s<p class='muted'>%s</p>%s" % (
                    esc(pid), tok, esc(t("hint_approve")), cards)
                dis = " disabled" if panel.blocked else ""
                body += ("<div class='bar'><button class='danger' type='submit'%s onclick=\"return confirm(%s)\">%s"
                         "</button><button type='submit' formaction='/plan/%s/approve'%s>%s</button>"
                         "<button type='submit' formaction='/plan/%s/reject'>%s</button></div></form>") % (
                    dis, esc(report.js_str(t("confirm_plan"))), esc(t("btn_approve_run")), esc(pid), dis,
                    esc(t("btn_approve")), esc(pid), esc(t("btn_reject")))
            else:
                body += cards
                if p["status"] in ("approved", "partial") and not live:
                    body += ("<form method='post' action='/plan/%s/run'><input type='hidden' name='t' value='%s'>"
                             "<div class='bar'><button class='primary'>%s</button></div></form>") % (
                        esc(pid), esc(panel.token), esc(t("btn_run")))
                log = os.path.join(panel.cfg["state_dir"], "logs", "%s.log" % pid)
                if os.path.exists(log):
                    with open(log, encoding="utf-8", errors="replace") as f:
                        body += "<h2>log</h2><pre style='white-space:pre-wrap'>%s</pre>" % esc(f.read()[-20000:])
            if p["status"] in ("done", "partial"):
                panel.refresh(force=panel.cache_at < (p["finished_at"] or 0))
            return body, live

        def records_page(self):
            con = db.connect(panel.cfg)
            rows = "".join(
                "<tr><td>%s</td><td>%s</td><td class='path'>%s</td><td>%s</td><td class='path'>%s</td></tr>" % (
                    esc(util.fmt_time(r["time"])), esc(t("act_" + r["action"], default=r["action"])),
                    esc(short(r["path"])), esc(util.human(r["bytes"]) if r["bytes"] else ""),
                    esc(r["location"] or ""))
                for r in con.execute("SELECT * FROM records ORDER BY time DESC LIMIT 500"))
            return "<h1>%s</h1><p class='muted'>%s</p><table>%s</table>" % (
                esc(t("nav_records")), esc(t("records_hint")), rows)

    return H


def serve(cfg, host=None, port=None):
    panel = Panel(cfg)
    host = host or cfg["web"]["host"]
    port = port or cfg["web"]["port"]
    httpd = ThreadingHTTPServer((host, port), make_handler(panel))
    httpd.daemon_threads = True
    panel.refresh()     # start computing right away, so the first page is ready sooner
    print(t("web_url", url="http://%s:%d/?t=%s" % (host, port, panel.token)), flush=True)
    if panel.blocked:
        print(t("web_agent_note", why=panel.blocked), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
