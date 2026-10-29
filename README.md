# tuckaway

**找出没人用的文件，说清楚为什么，经过批准再删掉或收起来。**

共用服务器上的磁盘总是满得很快：编译目录、仿真器、实验结果、临时文件，还有 AI agent 跑完留下的中间文件。
清理时最难的不是找大文件，而是判断"这个还有没有人在用"。tuckaway 给每个候选项附上证据：

- 最后一次改动是什么时候，里面的程序最近有没有运行过；
- 有没有进程正在用它（工作目录、打开的文件、映射的库、命令行参数、`PATH` / `LD_LIBRARY_PATH`）；
- 有没有脚本、配置或论文 `.tex` 写着它的路径，那些文件最近改过没有；
- 有没有符号链接从外面指向它；
- git 有没有跟踪里面的文件，里面的仓库是否都已推送；
- 主人给它挂的标签（临时、可重建、已冻结、保留、论文要用）。

然后给出三类建议：**删除**（垃圾、缓存、到期的临时文件），**归档**（冷数据，压缩后存到本地目录、移动硬盘或另一台机器），
以及**为什么不建议动**的大目录。删除和归档都要经过"计划 → 批准 → 执行"；批准只能由人在终端或网页面板里完成。

只用 Python 标准库（3.8 及以上）和系统自带的 `tar`、`ssh`；有 `zstd` 就用 zstd 压缩，没有就用 gzip。目前支持 Linux。

## 安装

```bash
git clone https://github.com/youzi27/tuckaway.git
ln -s "$PWD/tuckaway/tuck" ~/.local/bin/tuck    # 或者：pip install --user ./tuckaway
tuck init                                       # 写一份配置到 ~/.config/tuckaway/config.toml
```

先改配置：扫描哪些目录（`[scan] roots`），归档放哪里（`[archive]`），哪些目录永远不动（`[policy] keep`）。

## 日常使用

```bash
tuck scan          # 扫描一遍（约 550 万个文件、800G：满载的机器上用最低优先级约 10 分钟）
tuck report        # 看建议和理由（约半分钟）
tuck serve         # 网页面板：勾选 → 生成计划 → 批准并执行
```

建议每晚定时扫描（`crontab -e`）：

```cron
30 3 * * * $HOME/.local/bin/tuck scan >/dev/null 2>&1 && $HOME/.local/bin/tuck auto >> $HOME/.local/share/tuckaway/auto.log 2>&1
```

报告里每一项都像这样：

```text
[3f9a1c] 归档    8.2G  ~/proj/fpga-v/chipyard-v
      冷数据 · cold · 可信度中 · 项目 fpga-v
      + 10-13 以后没改过（16 天）
      + 没有进程在用
      ! 名字 chipyard-v 出现在 1 个文件里（如 ~/proj/fpga-v/chipyard/run_a11.sh (10-13)），可能是用相对路径读它
```

`tuck check <路径>` 给出一个路径的完整证据；动别人的东西之前先查一下。

## 标签

谁建的大目录谁挂标签，这比任何猜测都准：

```bash
tuck tag work/bisect-123 --cat scratch --expires 7d --project myproj --note "二分定位用的 worktree"
tuck tag build/sim       --cat regen   --rebuild "make sim  # 约 40 分钟"
tuck tag results/raw     --cat frozen  --note "论文数字已经从 summary.json 算好"
tuck tag results/summary --cat paper
```

| 类别 | 含义 | tuckaway 怎么处理 |
|---|---|---|
| `scratch` | 临时中间文件 | 到期后建议删除 |
| `regen` | 可以重建 | 两周没用后建议删除，附上重建命令 |
| `frozen` | 已定稿，不再读写 | 建议归档（不用等两周） |
| `keep` | 源码、证据、不能重建 | 永远不建议 |
| `paper` | 论文的图表数字要读 | 永远不建议 |

## 计划、批准和执行

```bash
tuck plan new --ids 3f9a1c,8be210     # 或 --action delete / --all
tuck plan add archive ~/old/dataset   # 手动加，证据照样附上
tuck approve P1029-01                 # 只能在交互式终端里，由人输入计划号确认
tuck run P1029-01                     # 执行已批准的计划（也可以在面板上点"批准并执行"）
```

执行时每个路径都会再查一遍：路径没了、有进程在用、或者生成计划之后又被改过，就跳过。
如果命令是从 AI agent 的 shell 里发出的（环境里有 `CLAUDECODE` 等标记），`tuck approve` 会拒绝；
由 agent 启动的网页面板也会关掉批准按钮。这是为了防止误操作，不是安全边界：能登录你账号的人什么都能做。

### 自动删除

有些东西删了没有任何损失，比如 `__pycache__`、pip 缓存、旧版 VS Code 服务端。`tuck rules` 列出每条内置规则，
并说明建不建议自动删除、为什么。在配置里打开：

```toml
[auto]
enabled = true
rules = ["pycache", "pip-cache", "conda-tmp", "vscode-old-server", "vscode-old-extension"]
max_gb_per_run = 50
```

之后 `tuck auto`（可以放进 cron）只删这些规则找到、可信度高、没有任何提醒的项，每一项都写进记录。

## 归档到哪里

```toml
[archive]
backend = "local"              # 压缩后留在本机（local_dir 可以是挂载的移动硬盘）
local_dir = "~/tuckaway-archive"

# 或者传到另一台机器（Linux / macOS / Windows 的 OpenSSH 都可以）
backend = "ssh"
[archive.ssh]
host = "nas.example.org"
user = "me"
key = "~/.ssh/id_ed25519"
remote_dir = "/data/archive"   # Windows 写 "D:/archive"，并设 shell = "cmd"
```

归档的步骤：打包 → 核对包里的条目数 → 计算 SHA-256 → 存过去 → 在目标端再算一次 SHA-256 →
确认打包期间原目录没被改过 → 删除原目录 → 在原位置留一个 `<目录>.TUCKED.txt`（写着包在哪、怎么恢复）→ 写记录。
任何一步不对，原目录都保留。

```bash
tuck where dataset             # 查某样东西归档到哪里了
tuck restore ~/old/dataset     # 取回、校验、解包
```

记录在 `~/.local/share/tuckaway/records.md`（也会复制一份到归档位置）。以前用别的脚本归档过的东西，
如果有 Markdown 表格记录（日期 | 项目 | 路径 | 包 | 文件数 | 大小 | SHA-256），可以 `tuck records --import-md <文件>` 导进来，之后也能用 `tuck restore` 恢复。

## 给 AI agent 的规则

让每个 agent 会话建大目录时挂标签、动别人的东西前先 `tuck check`、从不自己批准：把
[docs/agents.md](docs/agents.md) 里的那段贴进 `CLAUDE.md`、`AGENTS.md` 或你的 agent 的说明里。

## 内置规则

| 规则 | 找什么 | 建议自动删除 |
|---|---|---|
| `pycache` | `__pycache__` | 是 |
| `pip-cache` | pip / uv / npm 下载缓存 | 是 |
| `conda-tmp` | `/tmp/conda-lock-pkgs-*` 等 conda 临时文件 | 是 |
| `vscode-old-server` | 旧版 VS Code 远程服务端 | 是 |
| `vscode-old-extension` | 旧版 VS Code 插件 | 是 |
| `transfer-tmp` | 中断的转移留下的临时包 | 是 |
| `agent-tmp` | 已结束的 Claude Code 会话在 `/tmp` 里的目录 | 否：可能有没保存出去的结果 |
| `verilator-objdir` | Verilator 的 `obj_dir` | 否：要重新编译 |
| `node-modules` | `node_modules` | 否 |
| `trash` | 回收站 | 否 |
| `tool-cache` | `~/.cache` 下其他大缓存 | 否：下次要重新下载或编译 |
| `core-dump` | core dump | 否 |
| `waveform` | 大的 `.vcd` / `.fst` / `.fsdb` 波形 | 否 |
| `dup-archive` | 和已归档的包 SHA-256 相同的本地压缩包 | 否 |

自定义规则写在配置的 `[rules.custom.<名字>]` 里。

## 局限

- 判断"在用"靠的是证据，不是保证。只读不改的数据（比如原地使用的数据集）看起来会像冷数据，
  所以请把这类目录写进 `[policy] keep` 或挂 `keep` 标签。
- 程序最近有没有运行，看的是可执行文件的访问时间。如果有人用 `grep -r` 全盘搜过，这个时间会被刷新，
  结果是少建议，而不是多建议。
- 引用检查只认绝对路径（含 `~` 和 `$HOME`）和比较独特的目录名；用相对路径拼出来的路径可能查不到，这时会给出名字匹配的提醒。
- 只看得到自己账号的进程。

## License

MIT

---

## English summary

tuckaway finds files nobody uses any more on a shared machine, explains why with evidence (age, programs run,
processes, paths named by scripts and papers, symlinks, git state, owner tags), and suggests deleting garbage
and caches or archiving cold data. Deleting and archiving go through plans that a person approves in a
terminal or in the local web panel; AI agents can tag, check and propose, but not approve. Archives are tar +
zstd/gzip, verified by entry count and SHA-256 on both sides, stored in a local directory (or mounted disk) or
over ssh (Linux or Windows), with a stub left in place and a record kept; `tuck restore` brings them back.
Standard library only, Python 3.8+, Linux.

```bash
tuck init && tuck scan && tuck report      # or: tuck serve
```
