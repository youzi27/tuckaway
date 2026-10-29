---
name: tuckaway
description: Keep a shared disk from filling up with the `tuck` command. Use it when the disk is getting full or the user asks what can be cleaned up or moved, when you create a directory that may grow beyond about 1 GB (builds, simulators, run outputs, worktrees, downloads, scratch), before you delete or move anything you did not create in this session, and when someone asks where archived data went or wants it back.
---

# tuckaway (`tuck`)

`tuck` finds files nobody uses any more, explains why with evidence, and archives or deletes them only after
the user approves. You prepare and explain; the user decides.

`tuck` is on PATH when the tuckaway Claude Code plugin is enabled. If it is missing, ask the user before installing
it: `pip install --user git+https://github.com/youzi27/tuckaway` (Python 3.8+, Linux). Without a configuration it
scans the home directory and archives to `~/tuckaway-archive`; `tuck init` writes a configuration to edit.

## Rules

1. **Never approve.** Do not run `tuck approve` and do not press approve or run buttons in the web panel.
   Only the user approves, in their own terminal or panel. (`tuck approve` refuses in agent shells anyway.)
2. **Never delete or move by hand** what `tuck` would handle: no `rm -rf`, `mv` or ad-hoc archiving of other
   people's or other projects' data. Propose a plan instead.
3. **Check before touching** anything you did not create in this session: `tuck check <path>`. Every blocker it
   prints (recent change, programs run, a process using it, a script or paper naming it, symlinks, git-tracked
   files, a keep tag) means hands off. Warnings mean: tell the user.
4. **Tag what you create** when it may grow beyond about 1 GB, right away:
   ```bash
   tuck tag <dir> --cat <scratch|regen|frozen|keep|paper> --project <project> --owner <your session name> \
       --note "<what it is>" [--expires 7d] [--rebuild "<command, and how long it takes>"]
   ```
   `scratch` temporary (give `--expires`), `regen` rebuildable (give `--rebuild`), `keep` sources and evidence,
   `paper` read by a paper's figures or numbers, `frozen` finished and no longer read. Retag when that changes.

## When the user asks what can be cleaned up

1. `tuck report` (run `tuck scan` first if the last scan is more than a day old; it takes minutes, so start it
   in the background or detached).
2. Summarise for the user: the totals, the biggest delete and archive suggestions with their reasons and
   warnings (quote the item ids), what is turning cold soon, and which rules are recommended for automatic
   deletion and why. Point out anything rebuildable that is expensive to rebuild.
3. If the user wants to go ahead, make a plan of the items they chose and hand it over:
   ```bash
   tuck plan new --ids <id,id,...>          # or: tuck plan add delete|archive <path>
   ```
   Then tell them to approve it themselves: `tuck approve <plan>` in their own terminal, or in the panel
   (`tuck serve` started in their own terminal; "Run the selected" asks for confirmation and runs).
4. After they approved, you may run it: `tuck run <plan>` (archives can take long: run it detached and report
   the result from the log, `tuck plan show <plan>`).

## Finding and restoring

- `tuck where <name>`: where something archived or deleted went.
- `tuck restore <original path>`: fetch, verify and unpack an archive (only when the user wants it back).

## Commands

| Command | What it does |
|---|---|
| `tuck scan` | scan the configured roots |
| `tuck report` | suggestions and reasons (`--json` for machine-readable output) |
| `tuck check <path>` | evidence that a path is (not) in use |
| `tuck tag / untag / tags` | owner tags |
| `tuck plan new / add / show`, `tuck plans` | plans |
| `tuck run <plan>` | run a plan the user approved |
| `tuck where`, `tuck restore` | find and restore archives |
| `tuck rules` | built-in rules and the automatic-deletion advice |
