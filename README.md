<h1 align="center">Mole WebUI v1.0</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Zero--Dependencies-2E7D32?style=flat-square" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/macOS-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS">
  <img src="https://img.shields.io/badge/HTML-CSS-JS-E34F26?style=flat-square" alt="HTML CSS JS">
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="MIT License">
</p>

<p align="center">
  把本机已安装的 Mole CLI 包成一个浏览器界面 —— 清理 / 优化 / 卸载 / 分析 / 状态 十三页全中文,连 Mole 的输出也一起汉化。<br>
  <b>零第三方依赖,只监听 127.0.0.1,卸载确认在网页上亲手点。</b>
</p>

---

## 功能特点

### 界面与交互

- **13 个页面 = 13 个 Mole 子命令**:左侧栏分「主菜单」「更多工具」两组,一个子命令一页,点开即用,不用记命令
- **默认落地「清理」页**:打开就是最常用的那一个
- **执行日志抽屉**:SSE 实时流 + 「已运行 X 分 X 秒」计时器,顶部把手可拖拽调高度,不会盖住页面
- **只读通道**:状态 / 历史 / 分析 / 应用列表 / Touch ID 状态 / Shell 补全脚本全部走只读端点,绝不误触发删除
- **零第三方依赖**:后端纯 Python 3 标准库,前端原生 HTML / CSS / JS,不需要 pip install,也不需要 npm
- **只监听回环**:绑 `127.0.0.1:7788`,外网访问不到

### 输出汉化层

- **2958 条主表 + 21 条归一化表**,放在项目目录里,**不碰 `~/.config/mole`**,Mole 升级冲不掉
- **三级匹配**:整行精确命中 → 数字归一化命中(解决 `Trash · would empty, 2293 items` 这类每次数字都变的行)→ 行内片段替换
- **路径保护**:路径区间内的匹配一律跳过 —— `Application Support` 既是清理分类标题、又是 `~/Library` 下的真实目录名,译了会破坏路径
- **实测覆盖率 89.9%**:剩下未译的 27 行全是本该保留英文的(纯路径 / 应用名+体积 / macOS 目录名 / 进程名)
- **只影响显示**:解析与执行始终用 Mole 原文,映射表缺失或损坏只会静默降级为「不翻译」,不影响任何功能

### 卸载的网页内确认

- Mole 的卸载确认**强制要求真终端**(`[[ ! -t 0 ]]`),源码里写明原因:「一个脚本或 agent 就能替没人审阅过的人确认删除」
- WebUI **不绕过这个保护,而是把它搬到网页上**:后端给 `uninstall` / `remove` 单独挂一个 **pty(伪终端)**,Mole 因此认为自己在终端里
- Mole 打印确认提示 → 后端识别出来 → 任务卡片里弹出**黄色确认条** → 点「确认执行」= 终端按回车,点「取消」= 按 ESC,字节写回 pty 主端,Mole 收到的是一次真实按键
- **语义完整保留**:确认仍然是你亲手点的,Mole「只有真终端能回答」这条规则没有被削弱,也没有动用 Mole 的测试后门 `MOLE_TEST_MODE`
- **二次确认**:选了「D 选择个人数据」后 Mole 会再问一次,确认条会重新弹出

### 权限与安全

- **WebUI 不把命令包装成 root**:一律以你的用户身份跑 `mo ...`,Mole 的入口脚本明确拒绝以 root 运行,提权完全由 Mole 自己在需要时申请
- **「授权管理员权限」按钮**:点一次弹 Touch ID,后端缓存票据并每 30 秒自动续期,之后所有提权静默通过
- **备用方案**:在服务所在的终端里 `sudo -v` —— macOS 的 sudo 票据按终端生效,换终端无效
- **危险操作有硬保护**:卸载默认进废纸篓可恢复,只有勾了 `--permanent` 才直接删;卸载与移除必须亲手点确认
- **不提供 `--dry-run` 预览**:点「真正执行」就是 Mole 真的动手,想先看会删什么请在终端跑 `mo <命令> --dry-run`

### Mole 更新后的维护

- `tools/mole_sync.py check`:一键核对命令白名单(新增子命令 / 新选项才会漏)
- `tools/mole_sync.py collect`:跑真实命令采集输出,算出汉化缺口并产出待翻译清单
- `tools/mole_sync.py merge`:合并译文(占位符序列对不上直接丢弃;译文退回英文原样时**不许覆盖已有中文**)
- `tools/refresh_i18n.py`:从 Mole 源码里的 echo/printf 字面量抽取并翻译,和 collect 互补

---

## 快速开始

### 1、前置条件

- macOS,已安装 [tw93/mole](https://github.com/tw93/mole)(本项目实测 1.71.0)
- Python 3(只用标准库,无需 pip install)

### 2、启动

**双击** `Mole-WebUI.command` —— 拉起服务并用默认浏览器自动打开页面;服务已经在跑时双击则进入管理菜单(打开 Web UI / 停止 / 重启 / 退出)。

或者在终端里:

```bash
bash run.sh
```

然后访问 <http://127.0.0.1:7788/>。

> ⚠️ **必须在前台运行**。后端派生的 Mole 子进程要继承同一个**控制终端**,Mole 提权时才能读写 `/dev/tty`,弹出的 macOS 授权框(Touch ID / 密码)也才能显示在桌面上。用 `nohup`、`&` 或 IDE 的非交互终端启动会失去控制终端,提权必然失败(报 `/dev/tty: Device not configured`)。后端启动时若检测到没有控制终端,会自己打印一行警告。

**停止**:在跑服务的那个终端窗口按 **Ctrl+C**。

### 3、管理员授权

需要管理员权限的操作(清理、优化、卸载、Touch ID、更新、移除 Mole)会弹 macOS 授权框。

**推荐**:点左侧栏顶部的**「授权管理员权限」**按钮 → 弹 Touch ID → 票据被缓存并每 30 秒自动续期,之后提权不再弹框。

**备用**:在**服务所在的终端**里执行 `sudo -v`。服务前台占着终端时,先 Ctrl+C 停掉 → `sudo -v` → 再 `bash run.sh`,这样票据和服务在同一个终端。

### 4、自行部署

项目不打包成 `.app`,拷贝整个目录即可:

```bash
git clone https://github.com/smryyyyy/mo-webui.git
cd mo-webui
bash run.sh
```

它依赖本机的 Mole CLI(装在 `/usr/local/bin/mole`,配置在 `~/.config/mole/`),WebUI 才会调得动它 —— 这是一个「包装器」,不是 Mole 的替代品。

### 5、建议的测试顺序

从最安全(纯只读)开始,逐步往危险操作走。

| 步骤 | 页面 | 为什么放在这个位置 |
|---|---|---|
| 1 | **状态** | 纯只读,最安全,用来确认前后端通了 |
| 2 | **状态 → 实时监控** | 只读流,顺带验证 SSE 通道 |
| 3 | **Touch ID 查看 / Shell 补全脚本生成** | 只读动作,确认只读通道正常 |
| 4 | **清理历史** | 只读,顺便确认历史记录能读出来 |
| 5 | **分析** | 只读探索,试试下钻 |
| 6 | **卸载 → 应用列表** | 只读列表,先不勾选 |
| 7 | **第一次「真正执行」** | 挑一个**最不敏感**的操作,确认授权弹窗和执行流程都正常 |
| 8 | **卸载 → 确认条** | 先用 `mo uninstall --dry-run` 试:日志里应当弹出确认条,点「取消」应当安全退出、不删任何东西 |

后面的「真正执行」(卸载应用、Touch ID 启用/禁用、更新、移除 Mole)建议等前面全通了再逐个试。

> 提醒:每一步执行完都看一眼终端里后端打印的日志,出错第一时间能在 CLI 里复现。

---

## 使用说明

| 操作 | 说明 |
|---|---|
| 双击 `Mole-WebUI.command` | 拉起服务 + 自动开浏览器;已在跑则进管理菜单(打开 / 停止 / 重启 / 退出) |
| 左侧栏切页 | 13 个页面;切页时上一页的实时连接自动关闭,未返回的接口结果只写进旧容器,不会串进新页 |
| 「真正执行」 | 实际动手,会改文件。命令以你的用户身份运行,Mole 需要权限时自己弹 macOS 授权框 |
| 卸载页勾选应用 | 表格勾选 → 勾可选项 → 「真正卸载」,日志里会弹出确认条,由你亲手点 |
| 日志抽屉 | 拖顶部把手调高度;头部有实时计时器,用来判断任务是否还活着 |
| 中止任务 | 任务卡片上的「中止」按钮,后端会杀整个进程组并回收被 reparent 的孙进程 |
| 终端里 `sudo -v` | 备用授权方式,**必须在服务所在的终端**里执行 |

### 13 个页面速查

| 页面 | 命令 | 干什么 | 配置 / 数据来源 |
|---|---|---|---|
| 清理 | `mo clean` | 删缓存、日志、临时文件、卸载残留 | 缓存白名单 `~/.config/mole/whitelist` |
| 卸载 | `mo uninstall` | 卸载应用及其残留 | `mo uninstall --list` |
| 优化 | `mo optimize` | 刷新系统缓存与服务索引 | 优化白名单 `~/.config/mole/whitelist_optimize` |
| 分析 | `mo analyze` | 磁盘占用探索,可下钻子目录 | `mo analyze` |
| 状态 | `mo status` | 实时监控 / 单次快照 | `mo status --watch --interval 2s` |
| 清理历史 | `mo history` | 过往清理记录(会话 + 删除明细) | `mo history --json` |
| 项目清理 | `mo purge` | 清构建产物、缓存、依赖目录 | `~/.config/mole/purge_paths` |
| 安装包清理 | `mo installer` | 找遗留的 .dmg / .pkg / .iso / .xip / .zip | `mo installer` |
| Intel 瘦身 | `mo slim` | 移除通用应用里的 Intel 代码 | `mo slim` |
| Touch ID | `mo touchid` | 管理 sudo 的触控 ID 验证 | `/etc/pam.d/sudo` |
| Shell 补全 | `mo completion` | 生成 bash / zsh / fish 补全脚本(只打印,不写盘) | `mo completion <shell>` |
| 更新 | `mo update` | 更新 Mole 本体 | GitHub release |
| 移除 Mole | `mo remove` | 把 Mole 彻底移除(会让 WebUI 自己也失效) | 两次确认 |

### 两个白名单的格式不一样

- **缓存白名单** `~/.config/mole/whitelist`:每行一个绝对路径(`~` 或 `$HOME` 开头也行),支持 `*` `?` `[]` 通配符。相对路径会被 Mole 直接跳过,后端写入前先校验,不合法当场报错。
- **优化白名单** `~/.config/mole/whitelist_optimize`:Mole 按**整行精确匹配**,一行要么是绝对路径,要么是 19 个内置优化任务 ID 之一(如 `cache_refresh`)。**不是内置 ID 的标识符既匹配不上路径也匹配不上任务,等于没写** —— 页面卡片里会把可用的 ID 列出来。

> ⚠️ 缓存白名单**首次保存会创建文件,并把 Mole 的默认保护项一并写进去**(gradle / ollama / playwright / JetBrains / surge / tldr 等)。Mole 的规则是「文件一旦存在就只读这个文件」,不再回落到默认项 —— 否则无意中加一条就会丢掉这些保护。之后只追加,不动已有内容。Mole 的**硬安全项**(字体注册 / Spotlight / CloudKit / kitty 等)由 Mole 自己无条件合并,WebUI 不写它们。

### 卸载页的可选项

| 选项 | 效果 |
|---|---|
| (不勾) | 应用进**废纸篓**,可以恢复 |
| 跳过废纸篓 `--permanent` | 直接 rm -rf,**不可恢复** |
| 同时删除应用数据 `--with-data` | 连该应用的个人数据一起删 |

确认弹窗的文案会**按勾选状态生成**,勾了 permanent 会写明「不可恢复、不进废纸篓」。

### 几个实现细节

- **Intel 瘦身先在副本上做**:副本通过签名和 Gatekeeper 检查后才替换原应用,原应用先进废纸篓,所以一般还能找回
- **项目清理会自动补 `--yes`**:Mole 在非交互环境(stdin 不是终端)下会直接拒绝执行,后端自动附加这个标志。**实际执行的命令会随任务日志里的 argv 一起显示** —— 看到的与跑的就是同一条命令
- **Mole 子进程保留控制终端**:后端用 `process_group=0` 只换进程组、不换会话,控制终端仍然继承,Mole 提权时 `/dev/tty` 打得开(早期版本用 `start_new_session=True` 会调 `setsid()` 脱离终端,提权必失败)
- **白名单写入自动备份**:每次写入都会在原目录留一份 `.bak`

---

## 项目结构

```
mo-webui/
├── server.py          # 后端(单文件, 纯标准库)
│                      #   COMMANDS 白名单 / 输出汉化 / pty 确认通道 / SSE / 白名单读写
├── run.sh             # 启动脚本(检查 python3 与 server.py 后 exec)
├── Mole-WebUI.command # 双击启动 / 管理菜单
├── i18n/
│   ├── mole_zh.json      # 汉化主表(2958 条)
│   ├── mole_zh_norm.json # 数字归一化表(21 条)
│   └── mole_zh.json.bak  # 合并前备份(删掉不影响运行)
├── tools/
│   ├── mole_sync.py    # 白名单核对 / 输出采集 / 译文合并 / 覆盖率报告
│   └── refresh_i18n.py # 从源码字面量抽取并翻译
├── web/
│   └── index.html      # 前端单页(13 页 + 全部 CSS/JS)
└── README.md
```

Mole 本体在项目外(`~/.config/mole/`),WebUI 只读取和调用它,不修改它的安装目录。

---

## 技术栈

| 组件 | 用途 |
|---|---|
| Python 3 标准库 (`http.server`) | 后端 HTTP 服务 + SSE 推送 |
| Python 3 标准库 (`pty` / `termios`) | 卸载确认的伪终端通道 |
| Python 3 标准库 (`subprocess` / `process_group`) | Mole 子进程调度与进程组回收 |
| 原生 HTML / CSS / JS | 前端单页,无框架、无构建步骤 |
| EventSource (SSE) | 任务日志与实时监控推送 |
| Mole CLI | 被包装的实际执行者 |

---

## 常见问题

### 页面提示「后端未连接」

先确认服务还在跑(终端窗口没退出),再确认 Mole 入口存在:

```bash
ls -l /usr/local/bin/mole
```

没输出说明 Mole 没装或路径变了,WebUI 调不到它。

### 报 `/dev/tty: Device not configured` 或「管理员权限被拒」

说明服务**没有控制终端**,Mole 提权时打不开 `/dev/tty`。

- 用 `nohup`、`&`、IDE 内置终端或后台任务启动的,必然没有控制终端 → 改成在终端里**前台**运行 `bash run.sh`
- 后端启动时会打印「本服务没有控制终端」警告,先看终端有没有这行
- 确认终端里没有这行但仍报错:服务可能是旧版本,重启一次即可

### 卸载页没有弹出确认条

`mo uninstall` / `mo remove` 会在**日志卡片里**弹一条黄色边框的确认条。

- **按钮延迟约 1 秒才出现是故意的**:Mole 在 `read` 之前会 `tcflush` 清空输入队列,确认字节若落在这个窗口里会被冲掉,Mole 就永远等不到按键、任务会无声卡死。等它过了这一关再让你点
- **没弹出**:先看日志里有没有 Mole 的确认提示(`➤ ... Enter confirm, ESC cancel:`)。没有提示说明还在扫描阶段
- **提示出现了但按钮没出现**:说明后端没识别到那行,把原文发出来即可定位
- **弹出红色「需要管理员密码」条**:说明系统的 Touch ID 弹框被取消了,sudo 退回向终端问密码,而网页给不了密码 —— 在系统对话框里选密码,或直接点「中止任务」

### 点「确认执行」后整条日志静止、也没有「已结束」

确认字节被冲掉了。点任务上的「中止」结束本次操作,重新发起一次(第二次通常就会正常)。

### 执行时日志长时间不刷新(不等于卡死)

清理类命令在**扫描阶段**逐项打印,**进入删除阶段后一行都不打印** —— 删几百 MB 缓存(大量小文件)可能要几分钟,日志看着像卡住,其实进程在正常干活。

执行日志头部会实时显示「已运行 X 分 X 秒」,用它判断最直接。也可以看进程:

```bash
ps -eo pid,stat,etime,%cpu,command | grep -E 'mole|clean\.sh'
```

- `%cpu` 在变化、`STAT` 是 `S`/`R` → 在干活,继续等
- `STAT` 显示 `T` → 被停住了(CPU 0%、永不返回)。原因:后台进程组读控制终端会触发 `SIGTTIN`,内核把进程 SIGSTOP 挂起。后端已给所有 Mole 子进程 `stdin=/dev/null`,正常不会再出现;万一遇到(被停住的进程**收不到 SIGTERM**):

```bash
kill -CONT <pid> && kill -9 <pid>
```

### Mole 更新后新命令不见了

`server.py` 的 `COMMANDS` 是**严格允许列表**:不在名单里的子命令页面里根本看不到,不在名单里的参数会直接报「命令 X 不支持参数 Y」。

```bash
cd mo-webui
python3 tools/mole_sync.py check   # 核对白名单与实际 Mole 是否一致
```

新增子命令要往 `COMMANDS` 里加一条,新增选项要加进对应的 `bool_flags` / `value_flags`。翻译表不用管 —— 它在项目目录里,Mole 更新不会碰它,查不到新词条只是那一句显示英文。

### 端口 7788 被占用

端口**写死**在 `server.py` 顶部的常量区:

```python
HOST = "127.0.0.1"
PORT = 7788
```

改 `PORT` 的值即可(改完重启),浏览器访问地址也要跟着改。

### 想恢复被改动的白名单 / purge_paths

后端写入时会**自动生成 `.bak` 备份**,直接在原目录找:

- `~/.config/mole/whitelist.bak`
- `~/.config/mole/whitelist_optimize.bak`
- `~/.config/mole/purge_paths.bak`

复制回原名即可恢复。

---

## 已知限制(如实说明)

1. **不提供 `--dry-run` 预览** —— 点「真正执行」就是 Mole 真的动手,中间没有可以反悔的一步
2. **「分析」页只能浏览和勾选,不能按勾选结果精确删除** —— Mole CLI 本身没这个能力,不是 WebUI 偷懒
3. **「Shell 补全」页只生成脚本、不写配置文件** —— 也不做 shell 自动检测(无参调用会直接改写你的 shell 配置,且需要交互按键)
4. **实时监控的刷新间隔固定 2 秒**(`--interval 2s`),页面上不能调
5. **前端页面文案是硬编码的中文** —— 只有「命令帮助原文」卡片是直接抓 `mo <命令> --help` 原文贴上去(再经汉化层翻译)。CLI 升级后帮助卡片会自动更新,页面固定说明不会
6. **「项目清理」页执行可能偏慢** —— Mole 1.71.0 起会额外扫描 AI agent 的 worktree 容器,实测缓存热时约 5 秒,冷启动曾超过 3 分钟(后端超时上限已提到 600 秒)
7. **各命令在非交互环境下的真实行为不一样**:
   - `purge` —— 没有 `--yes` 就拒绝执行(WebUI 已自动补上)
   - `slim` / `installer` —— 交互式清单菜单,stdin 是 `/dev/null` 时读到 EOF 就退出,**只列候选、不改动任何文件**(安全的空操作)
   - `clean` —— 非交互时直接执行(Mole 会打印 "Running in non-interactive mode")
   - `uninstall` / `remove` —— 需要你在网页上亲手确认
8. **移除 Mole 会让 WebUI 自己也失效** —— 它调用的就是这个被删掉的 CLI

---

## 许可证

MIT License

---

## 致谢

本项目包装 [tw93/mole](https://github.com/tw93/mole) —— 感谢原作者的出色设计。

---

*本软件由 AI 辅助编写。*
