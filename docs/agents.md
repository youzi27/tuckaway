# Rules for AI agents (paste into CLAUDE.md, AGENTS.md or your agent's instructions)

```markdown
## Disk space: tuckaway (`tuck`)

The disk is shared and fills up quickly. Every session follows these rules.

- When you create a directory that may grow beyond about 1 GB (build trees, simulators, run outputs,
  worktrees, downloads, scratch), tag it right away:
  `tuck tag <dir> --cat <scratch|regen|frozen|keep|paper> --project <project> --owner <your session name> --note "<what it is>"`
  - `scratch`: temporary intermediate files; add `--expires 7d` (or a date). May be deleted after that.
  - `regen`: can be rebuilt; add `--rebuild "<command>"` and how long it takes in the note.
  - `keep`: sources, evidence, anything that cannot be recreated.
  - `paper`: read by a paper's figures, tables or numbers.
  - `frozen`: finished and no longer read; may go to the archive.
  Update the tag when that changes (`tuck tag` again, or `tuck untag`).
- Before you move or delete anything you did not create in this session, run `tuck check <path>` and respect
  every blocker it prints. Data of other projects is decided by their owners.
- Deleting and archiving go through plans: `tuck plan add delete|archive <path>` or `tuck plan new --ids ...`.
  Never run `tuck approve` and never press the approve buttons of the web panel: only the user approves.
  After the user approved a plan you may run it with `tuck run <plan>`.
- `tuck where <name>` tells where something archived went; `tuck restore <path>` brings it back.
```

## 中文版

```markdown
## 磁盘空间：tuckaway（命令 `tuck`）

磁盘是大家共用的，涨得很快。每个会话都遵守下面几条。

- 新建可能超过约 1 GB 的目录时（编译目录、仿真器、运行结果、worktree、下载、临时文件），马上挂标签：
  `tuck tag <目录> --cat <scratch|regen|frozen|keep|paper> --project <项目> --owner <你的会话名> --note "<这是什么>"`
  - `scratch`：临时中间文件，加 `--expires 7d`（或日期），到期后可以删。
  - `regen`：可以重建，加 `--rebuild "<命令>"`，在 note 里写重建要多久。
  - `keep`：源码、证据、不能重新生成的东西。
  - `paper`：论文的图、表或数字要读它。
  - `frozen`：已经定稿、不再读写，可以归档。
  情况变了就更新标签（再 `tuck tag` 一次，或 `tuck untag`）。
- 移动或删除不是本会话建的东西之前，先 `tuck check <路径>`，它列出的任何一条阻止理由都要遵守。别的项目的数据由那个项目决定。
- 删除和归档都走计划：`tuck plan add delete|archive <路径>` 或 `tuck plan new --ids ...`。
  不要运行 `tuck approve`，也不要点网页面板上的批准按钮：只有用户本人能批准。用户批准后可以用 `tuck run <计划号>` 执行。
- `tuck where <名字>` 查归档去了哪里；`tuck restore <路径>` 恢复。
```
