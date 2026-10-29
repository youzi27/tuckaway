"""End-to-end tests on a temporary tree. Run: python3 -m unittest discover -s tests"""

import copy
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tuckaway import archive, config, db, i18n, plans, recommend, scan, util  # noqa: E402

DAY = 86400


def write(path, size=1024, age_days=0, text=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w" if text is not None else "wb") as f:
        f.write(text if text is not None else os.urandom(size))
    if age_days:
        ts = time.time() - age_days * DAY
        os.utime(path, (ts, ts))


def age_tree(path, age_days):
    ts = time.time() - age_days * DAY
    for d, dirs, files in os.walk(path, topdown=False):
        for f in files:
            os.utime(os.path.join(d, f), (ts, ts), follow_symlinks=False)
        os.utime(d, (ts, ts))


class Base(unittest.TestCase):
    def setUp(self):
        i18n.set_lang("en")
        self.tmp = tempfile.mkdtemp(prefix="tuck-test-")
        self.root = os.path.join(self.tmp, "home")
        os.makedirs(self.root)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["state_dir"] = os.path.join(self.tmp, "state")
        cfg["scan"]["roots"] = [self.root]
        cfg["scan"]["store_min_mb"] = 0
        cfg["policy"]["min_candidate_mb"] = 0
        cfg["policy"]["candidate_min_depth"] = 2
        cfg["archive"]["local_dir"] = os.path.join(self.tmp, "archive")
        cfg["archive"]["tmp_dir"] = os.path.join(self.tmp, "atmp")
        cfg["safety"]["protect"] = []
        cfg["_path"] = os.path.join(self.tmp, "config.toml")
        self.cfg = cfg
        self.con = db.connect(cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self):
        return scan.Scanner(self.cfg, self.con).run()

    def items(self):
        return recommend.compute(self.con, self.cfg).items

    def paths(self, items, action=None):
        return {p for i in items if action in (None, i["action"]) for p in i["paths"]}


class TestToml(unittest.TestCase):
    def test_mini_parser(self):
        text = '''
lang = "en"  # comment
[scan]
roots = ["~", '/tmp',
   "/data"]
store_min_mb = 1_024
[archive.ssh]
port = 2222
[rules.custom.x]
glob = "~/a/*"
inline = { a = 1, b = true }
'''
        d = config._MiniToml(text).parse()
        self.assertEqual(d["lang"], "en")
        self.assertEqual(d["scan"]["roots"], ["~", "/tmp", "/data"])
        self.assertEqual(d["scan"]["store_min_mb"], 1024)
        self.assertEqual(d["archive"]["ssh"]["port"], 2222)
        self.assertEqual(d["rules"]["custom"]["x"]["inline"], {"a": 1, "b": True})

    def test_template_parses_the_same_with_both_readers(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tuckaway",
                            "config_template.toml")
        with open(path) as f:
            text = f.read()
        mini = config._MiniToml(text).parse()
        self.assertEqual(mini["lang"], "en")
        self.assertEqual(mini["archive"]["ssh"]["shell"], "posix")
        self.assertEqual(config.loads_toml(text), mini)

    def test_glob(self):
        g = util.Globs(["/a/*/c", "/x/**"])
        self.assertTrue(g.match("/a/b/c"))
        self.assertFalse(g.match("/a/b/b/c"))
        self.assertTrue(g.match("/x/y/z"))


class TestRecommend(Base):
    def test_cold_and_blockers(self):
        r = self.root
        write(r + "/proj/old/data.bin", 4096)
        age_tree(r + "/proj/old", 40)
        write(r + "/proj/new/data.bin", 4096)                      # recent
        write(r + "/proj/linked/data.bin", 4096)
        age_tree(r + "/proj/linked", 40)
        os.symlink(r + "/proj/linked/data.bin", r + "/elsewhere-link")   # symlink from outside
        write(r + "/proj/named/data.bin", 4096)
        age_tree(r + "/proj/named", 40)
        write(r + "/other/run.sh", text="cat %s/proj/named/data.bin\n" % r)   # a recent script names it
        write(r + "/proj/kept/data.bin", 4096)
        age_tree(r + "/proj/kept", 40)
        self.con.execute("INSERT INTO tags(path, category, owner) VALUES (?,?,?)", (r + "/proj/kept", "keep", "me"))
        self.con.commit()
        self.scan()
        arch = self.paths(self.items(), "archive")
        self.assertIn(r + "/proj/old", arch)
        for p in ("new", "linked", "named", "kept"):
            self.assertNotIn(r + "/proj/" + p, arch, p)

    def test_old_reference_is_a_warning(self):
        r = self.root
        write(r + "/proj/old/data.bin", 4096)
        age_tree(r + "/proj/old", 40)
        write(r + "/proj2/run.sh", text="ls %s/proj/old\n" % r, age_days=30)
        age_tree(r + "/proj2", 30)
        self.scan()
        its = [i for i in self.items() if r + "/proj/old" in i["paths"]]
        self.assertEqual(len(its), 1)
        self.assertTrue(any("run.sh" in w for w in its[0]["warnings"]))

    def test_rules_and_scratch_tag(self):
        r = self.root
        write(r + "/code/pkg/__pycache__/m.cpython-39.pyc", 2 << 20, age_days=3)
        age_tree(r + "/code/pkg/__pycache__", 3)
        write(r + "/proj/tmpwork/x.bin", 2048)
        self.con.execute("INSERT INTO tags(path, category, owner, expires) VALUES (?,?,?,?)",
                         (r + "/proj/tmpwork", "scratch", "agent-a", time.time() - 10))
        self.con.commit()
        self.scan()
        its = self.items()
        dele = self.paths(its, "delete")
        self.assertIn(r + "/code/pkg/__pycache__", dele)
        self.assertIn(r + "/proj/tmpwork", dele)
        py = [i for i in its if i["source"] == "pycache"][0]
        self.assertTrue(py["auto"]["recommended"])


class TestPlans(Base):
    def test_gate(self):
        self.assertIsNotNone(plans.approval_blocked(self.cfg, env={"CLAUDECODE": "1"}, isatty=True))
        self.assertIsNotNone(plans.approval_blocked(self.cfg, env={}, isatty=False))
        self.assertIsNone(plans.approval_blocked(self.cfg, env={}, isatty=True))

    def test_archive_restore_roundtrip(self):
        r = self.root
        write(r + "/proj/old/a.bin", 50000)
        write(r + "/proj/old/sub/b.txt", text="hello")
        os.symlink("a.bin", r + "/proj/old/link")
        age_tree(r + "/proj/old", 40)
        self.scan()
        ctx = recommend.compute(self.con, self.cfg)
        its = [i for i in ctx.items if i["paths"] == [r + "/proj/old"]]
        pid = plans.create(self.con, its, "test", ctx=ctx)
        with self.assertRaises(plans.PlanError):
            plans.run(self.con, self.cfg, pid, "test")         # not approved yet
        plans.approve(self.con, pid, "test")
        p = plans.run(self.con, self.cfg, pid, "test", log=lambda m: None)
        self.assertEqual(p["status"], "done")
        self.assertFalse(os.path.exists(r + "/proj/old"))
        self.assertTrue(os.path.exists(r + "/proj/old" + self.cfg["archive"]["stub_suffix"]))
        archive.restore(self.con, self.cfg, r + "/proj/old", "test", log=lambda m: None)
        with open(r + "/proj/old/sub/b.txt") as f:
            self.assertEqual(f.read(), "hello")
        self.assertTrue(os.path.islink(r + "/proj/old/link"))
        self.assertFalse(os.path.exists(r + "/proj/old" + self.cfg["archive"]["stub_suffix"]))

    def test_changed_after_plan_is_skipped(self):
        r = self.root
        write(r + "/proj/old/a.bin", 4096)
        age_tree(r + "/proj/old", 40)
        self.scan()
        ctx = recommend.compute(self.con, self.cfg)
        its = [dict(i, action="delete") for i in ctx.items if i["paths"] == [r + "/proj/old"]]
        pid = plans.create(self.con, its, "test", ctx=ctx)
        plans.approve(self.con, pid, "test")
        write(r + "/proj/old/new.bin", 10)                      # someone writes after the plan
        p = plans.run(self.con, self.cfg, pid, "test", log=lambda m: None)
        self.assertEqual(p["items"][0]["status"], "skipped")
        self.assertTrue(os.path.exists(r + "/proj/old/a.bin"))

    def test_gzip_backend(self):
        self.cfg["archive"]["compress"] = "gzip"
        r = self.root
        write(r + "/proj/old/a.bin", 4096)
        age_tree(r + "/proj/old", 40)
        rec = archive.archive_path(self.con, self.cfg, r + "/proj/old", "proj", "P", "test", log=lambda m: None)
        self.assertTrue(rec["location"].endswith(".tar.gz"))
        archive.restore(self.con, self.cfg, r + "/proj/old", "test", log=lambda m: None)
        self.assertTrue(os.path.exists(r + "/proj/old/a.bin"))

    def test_import_markdown(self):
        md = os.path.join(self.tmp, "legacy.md")
        with open(md, "w") as f:
            f.write("| date | project | path | archive | files | size | sha |\n|---|---|---|---|---|---|---|\n"
                    "| 2026-10-24 | fossil | `/x/tools` | `fossil\\tools.tar.zst` | 22189 | 1.3G | `%s` |\n" % ("a" * 64))
        self.assertEqual(archive.import_markdown(self.con, md, "ssh"), 1)
        self.assertEqual(archive.import_markdown(self.con, md, "ssh"), 0)
        rec = archive.find_record(self.con, "/x/tools")
        self.assertEqual(rec["location"], "fossil/tools.tar.zst")


if __name__ == "__main__":
    unittest.main()
