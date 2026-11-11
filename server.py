#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mole WebUI 后端 —— macOS 本地 127.0.0.1:7788

设计约束:
  - 仅 Python 3 标准库, 零第三方依赖
  - 只监听 127.0.0.1, 不对外暴露
  - 复用本机已安装的 Mole CLI, 不另建翻译表
  - /api/run 只放行白名单内的 13 个 Mole 子命令
    (前端共 14 个页面 = 13 个子命令 + 1 个仪表盘)
"""

import atexit
import codecs
import fcntl
import json
import os
import pty
import queue
import re
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 常量

HOST = "127.0.0.1"
PORT = 7788

SUDO_KEEPALIVE_INTERVAL = 30  # 秒
INSTALL_DIR = os.environ.get("MOLE_SCRIPT_DIR", "/Users/sunshuo/.config/mole")
MOLE_BIN = "/usr/local/bin/mole"
ANALYZE_BIN = os.path.join(INSTALL_DIR, "bin", "analyze-go")
STATUS_BIN = os.path.join(INSTALL_DIR, "bin", "status-go")
WHITELIST_CACHE = os.path.join(INSTALL_DIR, "whitelist")
WHITELIST_PROJECT = os.path.join(INSTALL_DIR, "whitelist_optimize")
PURGE_PATHS_FILE = os.path.join(INSTALL_DIR, "purge_paths")

# AI agent worktree 容器: Mole 1.71.0 起自动纳入 purge 扫描范围
# (lib/clean/purge_shared.sh 的 MOLE_PURGE_DEFAULT_SEARCH_PATHS)
WORKTREE_FIXED_CONTAINERS = [
    ("~/.codex/worktrees", "Codex"),
    ("~/.claude/worktrees", "Claude Code"),
    ("~/.t3/worktrees", "T3 Code"),
]
# 项目根下 6 层深的 .claude/worktrees 会被扫描
WORKTREE_PROJECT_ROOTS = [
    "code", "Code", "dev", "Projects", "GitHub", "Workspace",
    "Repos", "Development", "Developer", "www", "src",
]
WORKTREE_SCAN_TIMEOUT = 20  # 秒
UPDATE_MESSAGE_FILE = os.path.join(os.path.expanduser("~"), ".cache", "mole", "update_message")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

# 用户家目录。写白名单时的 $HOME/... 形式、前端显示时替换成 ~ 都以它为准。
USER_HOME = os.path.expanduser("~")

READONLY_TIMEOUT = 600    # /api/readonly 同步执行超时(秒)
                           # 1.71.0 的 purge 会扫 AI agent worktree 容器(6 层深度 find),
                           # 冷启动时实测超过 3 分钟(缓存热时约 5 秒), 所以上限从 180 提到 600。
RUN_TIMEOUT = 3600         # /api/run 后台任务超时(秒)

# 需要"真终端确认"的命令: Mole 强制要求 stdin 是 tty, 否则直接拒绝执行。
# 源码里明确写了原因(lib/uninstall/batch.sh 与 lib/manage/remove.sh):
#   "A read from a non-terminal fails and used to become an empty key, which
#    the case below treats as Enter: a script or agent could confirm an
#    uninstall nobody reviewed. Only a real terminal may answer."
# 这里给它们挂一个 pty, 把确认动作搬到 WebUI 页面上完成 ——
# 人依然是在自己审阅过清单之后亲手点确认, 语义没有丢。
INTERACTIVE_COMMANDS = {"uninstall", "remove"}
# Mole 的确认提示是 echo -ne 打印的, **不带换行**, 所以不能靠等行首来发现,
# 只能直接在字节流里匹配提示文本(纯 ASCII, 用字节正则最稳)。
# uninstall: "➤ Remove 2 apps, 16.9MB  Enter confirm, ESC cancel: "
# remove:    "➤ Press Enter to confirm, ESC to cancel: "
CONFIRM_PROMPT_RE = re.compile(
    r"(?:Enter\s+confirm,\s*ESC\s+cancel|Press\s+Enter\s+to\s+confirm,\s*ESC\s+to\s+cancel)")
# sudo 密码提示: 主人若在系统弹框里按了"取消", sudo 会退回向终端问密码,
# 而那个终端正是我们这个 pty —— 网页这边给不了密码, 所以要检测出来提示人。
SUDO_PROMPT_RE = re.compile(r"(?:\[sudo\]\s*password\s+for\s+\S+|Password:)", re.IGNORECASE)
# 确认字节写进去之后, 过这么久还没有任何新输出, 就认为被 drain 吃掉了, 补写一次
CONFIRM_RETRY_DELAY = 4.0
# 确认提示出现后, 过这么久才把确认按钮放出来(等 Mole 的 drain 跑完)
CONFIRM_EVENT_DELAY = 1.2

# ---------------------------------------------------------------- 命令白名单与参数闸门

# dry_run: 该命令是否支持 --dry-run（只读端点靠它保证不真的动手）
# danger : low(只读) / medium(可恢复删除) / high(不可逆)
COMMANDS = {
    "clean": {
        "title": "释放磁盘空间",
        "summary": "通过删除缓存、日志、临时文件以及已卸载应用的残留来释放磁盘空间。",
        "bool_flags": ["--dry-run", "-n", "--whitelist", "--debug"],
        "value_flags": ["--external"],
        "positional": 0,
        "dry_run": True,
        "danger": "medium",
        "sudo": True,
    },
    "uninstall": {
        "title": "彻底卸载应用",
        "summary": "交互式移除应用程序及其残留文件。也可直接指定一个或多个应用名称进行卸载。",
        "bool_flags": ["--list", "--dry-run", "--permanent", "--with-data", "--whitelist", "--debug"],
        "value_flags": [],
        "positional": 20,
        "dry_run": True,
        "danger": "medium",
        "sudo": True,
    },
    "optimize": {
        "title": "刷新缓存和服务",
        "summary": "刷新系统缓存和服务，修复安全维护问题。",
        "bool_flags": ["--dry-run", "-n", "--whitelist", "--debug"],
        "value_flags": [],
        "positional": 0,
        "dry_run": True,
        "danger": "medium",
        "sudo": True,
    },
    "analyze": {
        "title": "探索磁盘使用",
        "summary": "探索磁盘占用。不带路径时扫描整机概览。",
        "bool_flags": ["--json"],
        "value_flags": [],
        "positional": 1,
        "dry_run": False,
        "danger": "low",
        "sudo": False,
    },
    "status": {
        "title": "监控系统健康",
        "summary": "监控系统健康状态。不带选项时打开交互式仪表盘；当标准输出不是终端时，改为输出单份 JSON 快照。",
        "bool_flags": ["--json", "--watch", "--proc-cpu-alerts"],
        "value_flags": ["--interval", "--proc-cpu-threshold", "--proc-cpu-window"],
        "positional": 0,
        "dry_run": False,
        "danger": "low",
        "sudo": False,
    },
    "history": {
        "title": "查看清理活动",
        "summary": "查看 Mole 近期的操作和删除活动。",
        "bool_flags": ["--json"],
        "value_flags": ["--limit"],
        "positional": 0,
        "dry_run": False,
        "danger": "low",
        "sudo": False,
    },
    "purge": {
        "title": "清理旧项目产物",
        "summary": "Mole 项目清理，清理旧项目构建产物",
        "bool_flags": ["--paths", "--dry-run", "--include-empty", "--yes", "--debug"],
        "value_flags": [],
        "positional": 0,
        "dry_run": True,
        "danger": "medium",
        "sudo": False,
    },
    "installer": {
        "title": "查找并移除安装包文件",
        "summary": "查找并移除安装包文件（.dmg、.pkg、.iso、.xip、.zip）。",
        "bool_flags": ["--dry-run", "--debug"],
        "value_flags": [],
        "positional": 0,
        "dry_run": True,
        "danger": "medium",
        "sudo": False,
    },
    "slim": {
        "title": "移除通用应用中的 Intel 代码",
        "summary": "移除 Apple Silicon 上通用应用中的 Intel 代码。每个应用都在副本上瘦身，副本必须通过代码签名和 Gatekeeper 检查后才能替换原应用，原应用将移入废纸篓。",
        "bool_flags": ["--dry-run", "-n", "--debug"],
        "value_flags": [],
        "positional": 0,
        "dry_run": True,
        "danger": "medium",
        "sudo": False,
    },
    "touchid": {
        "title": "配置 sudo 触控 ID",
        "summary": "配置 sudo 认证的触控 ID。",
        "bool_flags": ["--dry-run"],
        "value_flags": [],
        "positional": 1,
        "positional_allowed": ["enable", "disable", "status"],
        "dry_run": True,
        "danger": "medium",
        "sudo": True,
    },
    "completion": {
        "title": "设置 shell 补全",
        "summary": "为 mole 和 mo 命令设置 shell 补全。",
        "bool_flags": ["--dry-run"],
        "value_flags": [],
        "positional": 1,
        "positional_allowed": ["bash", "zsh", "fish"],
        "dry_run": True,
        "danger": "medium",
        "sudo": False,
    },
    "update": {
        "title": "更新到最新版本",
        "summary": "将 Mole 更新到最新版本。",
        "bool_flags": ["--force", "-f", "--nightly"],
        "value_flags": [],
        "positional": 0,
        "dry_run": False,
        "danger": "high",
        "sudo": True,
        "confirm": "更新会替换已安装的 Mole 版本（当前 {version}），确认继续？",
    },
    "remove": {
        "title": "从系统中移除 Mole",
        "summary": "从本机移除 Mole。操作日志会保留在 ~/Library/Logs/mole。",
        "bool_flags": ["--dry-run", "-n"],
        "value_flags": [],
        "positional": 0,
        "dry_run": True,
        "danger": "high",
        "sudo": True,
        "confirm": "这会从系统中卸载 Mole 本体及其全部配置，确认继续？",
    },
}

# 主菜单 5 项 —— 文案与顺序取自 mo 主菜单实测输出
PRIMARY_MENU = [
    {"key": "clean", "title": "清理", "desc": "释放磁盘空间"},
    {"key": "uninstall", "title": "卸载", "desc": "彻底卸载应用"},
    {"key": "optimize", "title": "优化", "desc": "刷新缓存和服务"},
    {"key": "analyze", "title": "分析", "desc": "探索磁盘使用"},
    {"key": "status", "title": "状态", "desc": "监控系统健康"},
]

# ---------------------------------------------------------------- ANSI 处理

# 同时覆盖颜色码(\x1b[31m)与光标控制序列(\x1b[H / \x1b[2K / \x1b[J)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# 行首图标 -> 行类型
LINE_ICONS = {
    "\u27a4": "section", "\u25cb": "item", "\u2299": "item", "\u25cf": "item",
    "\u2713": "item", "\u25ce": "item", "\u2298": "item", "\u2717": "item",
    "\u2718": "item", "\u2699": "info", "\u26a0": "info", "\u2139": "info",
    "\u2605": "info", "\u2192": "item", "\u25b8": "item",
}


def strip_ansi(text):
    """去掉 ANSI 颜色与光标控制序列"""
    if not text:
        return ""
    text = OSC_RE.sub("", text)
    return ANSI_RE.sub("", text)


# ---------------------------------------------------------------- 输出汉化层
# Mole 官方版是英文的, WebUI 把它自己的界面做成了中文。这里再补一层:
# 把 Mole 输出的英文文案按映射表换成中文, 只影响"显示", 不影响解析与执行。
#
# 映射表 i18n/mole_zh.json 由 Mole 1.71.0 的源码文案抽取 + 翻译而来,
# 放在交付目录里, 不修改 ~/.config/mole —— Mole 升级不会把它冲掉。
I18N_DIR = os.path.join(BASE_DIR, "i18n")
I18N_FILE = os.path.join(I18N_DIR, "mole_zh.json")
# 归一化表: 键是"把数字换成 #0 #1 ..."后的整行, 用于同一模板但数字不同的行
# (例如 "Trash · would empty, 2293 items" 与 "Trash · would empty, 41 items")
I18N_NORM_FILE = os.path.join(I18N_DIR, "mole_zh_norm.json")
_INLINE_MAX_LEN = 80      # 更长的词条只走整行匹配, 避免行内替换误伤长句
_ZH_MAP = {}
_ZH_RE = None
_ZH_READY = False
_NORM_MAP = {}
_NORM_NUM = re.compile(r"#(\d+)")
# 行内替换时需要避开的路径区间
_PATH_SPAN = re.compile(r"(?:^|[\s(])(~?/[^\s,;)]*)")
# Mole 输出里的行首图标, 归一化匹配前先剥离(与离线抽取脚本一致)
_ZH_SYMBOL_PREFIX = re.compile(
    "^([\u25cb\u2713\u2299\u2699\u25ce\u27a4\u25cd\u25aa\u2022\u2691"
    "\u2605\u2714\u2716\u26a0\u25c9\u2023]+[ \t]*)(.+)$")
_NUM_TOKEN = re.compile(r"\d+(?:\.\d+)?")


def _load_zh_map():
    """加载中英映射表。文件缺失时静默降级为"不翻译", 不影响任何功能。"""
    global _ZH_MAP, _ZH_RE, _ZH_READY, _NORM_MAP
    if _ZH_READY:
        return
    _ZH_READY = True
    raw = {}
    try:
        with open(I18N_FILE, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            raw = loaded
    except Exception:
        raw = {}
    clean = {}
    for en, zh in raw.items():
        if not isinstance(en, str) or not isinstance(zh, str):
            continue
        en = en.strip()
        zh = zh.strip()
        if not en or not zh or zh == en:
            continue
        clean[en] = zh
    _ZH_MAP = clean
    # 归一化表独立加载: 缺失或损坏都只是少一层匹配, 不影响其它功能
    norm = {}
    try:
        with open(I18N_NORM_FILE, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            for k, v in loaded.items():
                if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                    norm[k.strip()] = v.strip()
    except Exception:
        norm = {}
    _NORM_MAP = norm
    # 行内替换合成一条正则: 一次扫描找出所有命中, 避免逐条 find 的 O(词条数 x 行数)
    inline = [k for k in clean if len(k) <= _INLINE_MAX_LEN and "\n" not in k]
    inline.sort(key=len, reverse=True)      # 长词条优先, 避免短词条把长句切碎
    if inline:
        try:
            _ZH_RE = re.compile(
                r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(k) for k in inline) + r")(?![A-Za-z0-9_])"
            )
        except Exception:
            _ZH_RE = None


def _norm_key(line):
    """把行里的数字按出现顺序换成 #0 #1 ..., 同时返回原数字列表。

    键的构造必须与 tools/refresh_i18n.py 和离线抽取脚本完全一致:
    只替换数字本身(不带小数点后的单位), 序号从 0 开始。
    """
    nums = []

    def rep(m):
        nums.append(m.group(0))
        return "#%d" % (len(nums) - 1)

    return _NUM_TOKEN.sub(rep, line), nums


def _fill_numbers(text, nums):
    """把译文里的 #N 换回原行的数字"""
    def rep(m):
        i = int(m.group(1))
        return nums[i] if 0 <= i < len(nums) else m.group(0)

    return _NORM_NUM.sub(rep, text)


def _translate_inline(line):
    """行内词条替换: 只替换落在词边界上的完整词, 且不碰路径与标识符"""
    if _ZH_RE is None or not line:
        return line
    # 路径区间内的匹配一律跳过: 目录名(如 Application Support、Time Machine)
    # 常常既是清理分类标题、又是 ~/Library 下的真实目录名, 译了会破坏路径
    spans = [(m.start(1), m.end(1)) for m in _PATH_SPAN.finditer(line)]
    out = []
    last = 0
    for m in _ZH_RE.finditer(line):
        if m.start() > last:
            out.append(line[last:m.start()])
        if any(s <= m.start() < e for s, e in spans):
            out.append(m.group(0))          # 路径里, 原样保留
        else:
            out.append(_ZH_MAP.get(m.group(0), m.group(0)))
        last = m.end()
    if last < len(line):
        out.append(line[last:])
    return "".join(out)


def translate_text(text):
    """把 Mole 的英文输出换成中文: 整行精确命中优先, 其次行内词条替换。"""
    if not text:
        return text
    if not _ZH_READY:
        _load_zh_map()
    if not _ZH_MAP:
        return text
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped in _ZH_MAP:
            indent = line[:len(line) - len(line.lstrip())]
            lines.append(indent + _ZH_MAP[stripped])
            continue
        if _NORM_MAP:
            # 数字不同的同一模板行: 归一化后查表, 再把原数字填回译文
            # 行首图标(○ ✓ ➤ …)先剥离, 与离线抽取脚本的规则保持一致
            prefix, core = "", stripped
            ms = _ZH_SYMBOL_PREFIX.match(stripped)
            if ms:
                prefix, core = ms.group(1), ms.group(2)
            key, nums = _norm_key(core)
            if key in _NORM_MAP:
                indent = line[:len(line) - len(line.lstrip())]
                lines.append(indent + prefix + _fill_numbers(_NORM_MAP[key], nums))
                continue
        lines.append(_translate_inline(line))
    return "\n".join(lines)


def _split_columns(line):
    """按 2 个以上空格切分表格列；不足 2 列返回 None"""
    if "  " not in line:
        return None
    parts = [p.strip() for p in re.split(r"\s{2,}", line.strip())]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    return parts


def parse_output(text):
    """
    把剥离 ANSI 后的终端文本还原成结构化行数组。

    每项形如:
      {"type": "section"|"item"|"info"|"text"|"table", ...}
    """
    clean = strip_ansi(text)
    rows = []
    table_buf = []

    def flush_table():
        if not table_buf:
            return
        if len(table_buf) >= 2:
            rows.append({
                "type": "table",
                "header": [translate_text(c) for c in table_buf[0]],
                "rows": [[translate_text(c) for c in row] for row in table_buf[1:]],
            })
        else:
            for cols in table_buf:
                rows.append({"type": "text", "text": "  ".join(cols)})
        table_buf.clear()

    normalized = clean.replace("\r\n", "\n").replace("\r", "\n")
    for raw in normalized.split("\n"):
        line = raw.rstrip()
        if line.strip() == "":
            flush_table()
            continue

        first = line.strip()[0]
        cols = _split_columns(line)
        # 表格候选行: 不含行首图标, 且能切出 2 列以上
        if cols and first not in LINE_ICONS and not line.startswith(" "):
            table_buf.append(cols)
            continue

        flush_table()

        stripped = line.strip()
        icon = stripped[0]
        kind = LINE_ICONS.get(icon)

        if kind:
            body = stripped[1:].strip()
            entry = {"type": kind, "text": translate_text(body), "icon": icon}
            if "\u00b7" in body:
                name, _, detail = body.partition("\u00b7")
                entry["name"] = translate_text(name.strip())
                entry["detail"] = translate_text(detail.strip())
            rows.append(entry)
        else:
            indent = len(line) - len(line.lstrip())
            entry = {"type": "text", "text": translate_text(stripped)}
            if indent >= 2:
                entry["indent"] = indent
            rows.append(entry)

    flush_table()
    return rows


# ---------------------------------------------------------------- 子进程执行

def mole_env():
    """子进程环境: 补 PATH 与 MOLE_SCRIPT_DIR, 保留用户 HOME"""
    env = os.environ.copy()
    env["MOLE_SCRIPT_DIR"] = INSTALL_DIR
    env["PATH"] = env.get("PATH") or "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    env.setdefault("LANG", "en_US.UTF-8")
    return env


# ---------------------------------------------------------------- 子进程启动方式（重要，勿改回 start_new_session）
# start_new_session=True 会调用 setsid(), 子进程脱离控制终端, /dev/tty 随即打不开
# (ENXIO: Device not configured)。Mole 提权时必须读写 /dev/tty
# (lib/core/sudo.sh 的 request_sudo_access), 没有控制终端就直接失败,
# 表现为「更新已中止，管理员权限被拒」。
# process_group=0 (setpgid) 只换进程组、不换会话: 控制终端仍在, /dev/tty 可用,
# macOS 按 tty 匹配的 sudo 票据也能复用; 同时 _kill_group 的 killpg 仍能整组回收,
# 不会留下孤儿进程。要求 Python 3.11+。
#
# 但子进程因此不在前台进程组里: 它一旦 read() 控制终端, 内核发 SIGTTIN,
# 进程会被 SIGSTOP 停住(STAT 显示 T, CPU 0%), 表现为「一直转圈, 永远不返回」。
# 所以 stdin 必须给 DEVNULL: Mole 的交互检查是 [[ ! -t 0 ]], 拿到 DEVNULL
# 会判定为非交互并直接拒绝, 而不是去读终端把自己挂起。
# 提权路径不受影响: 它读的是 /dev/tty(显式打开, 不是继承的 stdin),
# 而且有 sudo 票据时走 sudo -n, 根本不读终端。

def run_sync(argv, timeout=READONLY_TIMEOUT, cwd=None):
    """同步执行, 返回 (stdout, stderr, exit_code, elapsed_ms)"""
    started = time.time()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd or os.path.expanduser("~"),
            env=mole_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            process_group=0,
        )
    except OSError as exc:
        return "", "无法启动进程: %s" % exc, 127, int((time.time() - started) * 1000)

    try:
        out, err = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        err = (err or "") + "\n[WebUI] 执行超时（%d 秒）已中止" % timeout
        code = 124
    elapsed = int((time.time() - started) * 1000)
    return out or "", err or "", code, elapsed


def _descendant_pids(pid):
    """列出 pid 的所有后代进程（Mole 是 bash 脚本, 会 fork 出子进程,
    那些子进程可能在进程组被杀前就被 reparent 到 init, 那时已经找不到了,
    所以必须在 kill 之前收集）"""
    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        )
    except Exception:
        return []
    children = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
        except ValueError:
            continue
    found = []
    stack = list(children.get(pid, []))
    while stack:
        cur = stack.pop()
        found.append(cur)
        stack.extend(children.get(cur, []))
    return found


def _kill_group(proc):
    """结束整个进程组 + 所有后代进程（避免留下孤儿进程）"""
    if proc.poll() is not None:
        return
    descendants = _descendant_pids(proc.pid)
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    # 同组保护: 万一子进程没有独立进程组, killpg 会连本进程一起杀掉
    if pgid is not None and pgid == os.getpgid(0):
        pgid = None
    try:
        if pgid is None:
            proc.terminate()
        else:
            os.killpg(pgid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass
    # 进程组杀完后仍存活的孤儿（脚本 fork 出去又脱离进程组的那些）
    for child in descendants:
        try:
            os.kill(child, signal.SIGKILL)
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
        return
    except Exception:
        pass
    try:
        if pgid is None:
            proc.kill()
        else:
            os.killpg(pgid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------- 参数校验（危险操作闸门）

def validate_args(command, args):
    """
    校验命令与参数, 只放行 COMMANDS 白名单内的子命令及其合法参数。
    返回 (argv, error)
    """
    if command not in COMMANDS:
        return None, "不允许的命令: %s" % command
    spec = COMMANDS[command]
    if not isinstance(args, list):
        return None, "args 必须是数组"

    argv = [MOLE_BIN, command]
    positional = []
    i = 0
    while i < len(args):
        arg = str(args[i])
        if arg.startswith("-"):
            if arg in spec["value_flags"]:
                if i + 1 >= len(args):
                    return None, "参数 %s 缺少取值" % arg
                value = str(args[i + 1])
                if command == "clean" and arg == "--external":
                    if not value.startswith("/"):
                        return None, "--external 需要绝对路径"
                argv.extend([arg, value])
                i += 2
                continue
            if arg in spec["bool_flags"]:
                argv.append(arg)
                i += 1
                continue
            return None, "命令 %s 不支持参数 %s" % (command, arg)
        positional.append(arg)
        i += 1

    if len(positional) > spec["positional"]:
        return None, "命令 %s 最多接受 %d 个位置参数" % (command, spec["positional"])

    allowed_pos = spec.get("positional_allowed")
    for pos in positional:
        if len(pos) > 200:
            return None, "位置参数过长"
        if allowed_pos and pos not in allowed_pos:
            return None, "命令 %s 的位置参数只能是: %s" % (command, " / ".join(allowed_pos))
        if command == "analyze" and not pos.startswith("/"):
            return None, "analyze 的路径必须是绝对路径"

    argv.extend(positional)
    return argv, None


def build_sudo_argv(argv, home=None):
    """
    不对整条命令提权, 原样返回 argv。

    Mole 的入口脚本 (/usr/local/bin/mole) 明确拒绝以 root 运行:
        "请不要用 sudo 运行 Mole；需要权限时它会自行申请。"
    所以这里既不能用 sudo -n, 也不能用 osascript do shell script
    (后者同样以 root 运行), 否则 Mole 会立刻退出。

    正确做法: 命令以当前用户身份运行, 由 Mole 自己在需要时提权
    (lib/core/sudo.sh 的 request_sudo_access: 先 sudo -n 复用已有票据,
     没有就在控制终端上 sudo -v 触发 Touch ID 或密码)。
    """
    return list(argv), False


def sudo_credential_hint():
    """探测是否已有可用 sudo 凭据; 没有就给出可操作的提示 (不是错误)。"""
    if _sudo_cached():
        return None
    return ("[WebUI] 当前没有可用的 sudo 凭据缓存。授权方式任选其一：\n"
            "  ① 在【启动本服务的那个终端】里执行一次 sudo -v（Touch ID 或密码），"
            "票据有效期内所有提权操作直接通过；\n"
            "  ② 直接点执行，由 Mole 尝试弹出 macOS 授权框，确认即可。\n"
            "  注意：macOS 的 sudo 票据按终端生效，换另一个终端执行 sudo -v 对本服务无效。")


def _sudo_cached():
    """探测是否已有可用的 sudo 缓存凭据"""
    try:
        proc = subprocess.run(
            ["sudo", "-n", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _has_controlling_tty():
    """本进程是否有控制终端（决定 Mole 能否走 /dev/tty 提权路径）"""
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return False
    try:
        os.close(fd)
    except OSError:
        pass
    return True


def _request_touch_id(timeout=120):
    """
    在控制终端上执行 sudo -v, 触发 macOS 的 Touch ID / 密码授权框。

    授权框本身是系统 GUI, 不依赖终端输入; 但 sudo 的 PAM 会话需要一个
    可读写的 tty 才能走完流程, 所以 stdin/stdout/stderr 全指向 /dev/tty。
    macOS 的票据按 tty 匹配: 授权成功后, 本服务(同一控制终端)以及它
    启动的 Mole 子进程都能直接复用。
    """
    if not _has_controlling_tty():
        return False, "本服务没有控制终端，无法弹授权框。请在终端里前台运行 run.sh 后重试。"
    try:
        in_fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError as exc:
        return False, "无法访问控制终端：%s" % exc
    try:
        out_fd = os.open("/dev/tty", os.O_WRONLY)
    except OSError as exc:
        os.close(in_fd)
        return False, "无法访问控制终端：%s" % exc
    try:
        # 用裸 fd 而不是 Python 文件对象：/dev/tty 是字符设备, 不可 seek,
        # open("/dev/tty", "r+b") 会抛 "File or stream is not seekable"。
        proc = subprocess.run(
            ["sudo", "-v"],
            stdin=in_fd, stdout=out_fd, stderr=out_fd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "授权超时（%d 秒未确认），请重试。" % timeout
    finally:
        for fd in (in_fd, out_fd):
            try:
                os.close(fd)
            except OSError:
                pass
    if proc.returncode == 0:
        return True, "授权成功，已缓存管理员凭据。"
    return False, "授权未通过（被拒绝或密码不正确）。"


def _sudo_keepalive_loop():
    """
    每 30 秒用 sudo -n -v 刷新票据时间戳。

    macOS 的 sudo 票据默认 5 分钟过期, 且按控制终端匹配。主人只要在
    启动本服务的那个终端授权过一次, 这里就能把有效期续成滑动窗口,
    之后所有提权操作都直接通过, 不再弹框。sudo -n 不会弹框, 没票据
    时只会静默失败。
    """
    while True:
        try:
            subprocess.run(
                ["sudo", "-n", "-v"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception:
            pass
        time.sleep(SUDO_KEEPALIVE_INTERVAL)


def _cleanup_running_tasks():
    """退出时回收仍在运行的 Mole 子进程组, 避免留下孤儿进程"""
    for task in list(TASKS.values()):
        proc = getattr(task, "proc", None)
        if proc is not None:
            try:
                _kill_group(proc)
            except Exception:
                pass


# ---------------------------------------------------------------- 后台任务与 SSE

class Task(object):
    def __init__(self, task_id, command, args, sudo):
        self.id = task_id
        self.command = command
        self.args = args
        self.sudo = sudo
        self.q = queue.Queue()
        self.proc = None
        self.lock = threading.Lock()
        self.aborted = False
        self.finished = False
        self.exit_code = None
        self.started_at = time.time()
        self.elapsed_ms = 0
        self.argv = None
        self.master = None       # pty 主端(交互式命令用), 用来回写确认按键
        self.awaiting_confirm = False   # 是否正停在 Mole 的确认提示上
        self.last_pty_activity = 0.0    # pty 最后一次读到数据的时间
        self.confirm_written_ts = 0.0   # 最后一次写入确认字节的时间

    def emit(self, payload):
        self.q.put(payload)

    def abort(self):
        with self.lock:
            if self.finished:
                return False
            self.aborted = True
            proc = self.proc
        if proc is not None:
            _kill_group(proc)
        return True


TASKS = {}
TASK_SEQ = [0]
TASK_LOCK = threading.Lock()


def _new_task_id():
    with TASK_LOCK:
        TASK_SEQ[0] += 1
        return "t%d-%d" % (int(time.time() * 1000), TASK_SEQ[0])


def start_task(command, args, sudo):
    """启动后台任务, 立即返回 task_id"""
    argv, err = validate_args(command, args)
    if err:
        return None, err
    argv = _apply_noninteractive_flags(command, argv)
    task_id = _new_task_id()
    task = Task(task_id, command, args, sudo)
    TASKS[task_id] = task
    thread = threading.Thread(target=_task_worker, args=(task, argv, sudo), daemon=True)
    thread.start()
    return task_id, None


def _delayed_confirm(task, prompt):
    """延后把确认动作交给网页 —— 等 Mole 过了 drain 阶段。

    Mole 在 read 之前会 drain_pending_input: 它用 tcflush(TCIFLUSH) 把输入队列
    整个清掉。我们的确认字节如果正好落在那个窗口里, 就会被冲掉, read 永远
    等不到 —— 任务既不输出也不结束, 看起来像卡死。

    所以确认按钮**延后**再放出来: 等 Mole 自己过了 drain 进了 read,
    主人点按钮的时候字节才是安全的。
    """
    def fire():
        with task.lock:
            if task.finished:
                return
            task.awaiting_confirm = True
        task.emit({"type": "confirm", "command": task.command, "prompt": prompt})

    timer = threading.Timer(CONFIRM_EVENT_DELAY, fire)
    timer.daemon = True
    timer.start()


def _terminal_line(line):
    """把 pty 里的一行整理成"终端上最后显示的内容"。

    终端重绘靠 \r 回到行首再重写, 一个逻辑行里可能叠了十几帧
    (Mole 的扫描进度条就是 \r\x1b[2K 刷的), 直接整行塞进网页会变成
    一大坨重复乱码 —— 只留最后一个 \r 之后的部分, 再剥掉 ANSI 色码。
    """
    line = line.rstrip("\r")
    if "\r" in line:
        line = line.split("\r")[-1]
    return strip_ansi(line)


def send_task_confirm(task_id, action):
    """把网页上的"确认 / 取消"动作写回 pty 主端。

    Mole 的确认是 `read -r -s -n1`: 回车 = 确认, ESC = 取消。
    写进去的是**一个字节**, 不是一行 —— 写多了后面的 read 会读到残留。

    只在"当前正停在确认提示上"时才允许写, 防止重复点击把多余字节
    塞进 pty 输入队列, 被后面的交互读到。
    """
    task = TASKS.get(task_id)
    if task is None:
        return None, "task 不存在"
    if task.command not in INTERACTIVE_COMMANDS:
        return None, "%s 不需要交互确认" % task.command
    if action == "confirm":
        data = b"\n"
    elif action == "cancel":
        data = b"\x1b"
    else:
        return None, "action 必须是 confirm 或 cancel"
    with task.lock:
        if task.finished or task.proc is None:
            return None, "任务已结束"
        if task.master is None:
            return None, "该任务没有交互提示"
        if not task.awaiting_confirm:
            return None, "当前没有等待确认的提示"
        try:
            os.write(task.master, data)
        except OSError as exc:
            return None, "写入确认动作失败: %s" % exc
        task.awaiting_confirm = False
        task.confirm_written_ts = time.time()
    # 看门狗: Mole 在 read 之前会 drain 掉挂起输入, 我们的字节正好落进那个
    # 窗口就会被吃掉, read 永远等不到 -> 任务无声卡死(既没输出也不结束)。
    # 现象是"写完之后一点动静都没有", 所以过几秒还没动静就补写一次;
    # Mole 自己在 read 之后还会 drain 一次, 多写的字节会被它清掉。
    threading.Timer(CONFIRM_RETRY_DELAY, _confirm_watchdog,
                    args=(task_id, data)).start()
    if action == "cancel":
        # 光看退出码分不清"取消"和"成功", 在日志里明说一句
        task.emit({"type": "line", "stream": "stdout",
                   "text": "[WebUI] 已发送取消（ESC），Mole 将中止本次操作"})
    return {"ok": True, "task_id": task_id, "action": action}, None


def _confirm_watchdog(task_id, data):
    """确认字节可能被 Mole 的 drain 吃掉 —— 没动静就再补一次。"""
    task = TASKS.get(task_id)
    if task is None:
        return
    with task.lock:
        if task.finished or task.proc is None or task.master is None:
            return
        if task.last_pty_activity > task.confirm_written_ts:
            return          # 之后有新输出 -> key 已经被消费, 不用补
        try:
            os.write(task.master, data)
        except OSError:
            return
        task.confirm_written_ts = time.time()


def _task_worker(task, argv, sudo):
    started = time.time()
    task.emit({
        "type": "start",
        "task_id": task.id,
        "command": task.command,
        "args": task.args,
        "sudo": sudo,
        "argv": argv,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

    if sudo:
        # 命令以当前用户身份运行; Mole 需要权限时会自己弹 macOS 授权框
        task.emit({
            "type": "line", "stream": "stderr",
            "text": "[WebUI] 命令将以当前用户身份运行。Mole 需要权限时会在控制终端上请求授权（Touch ID 或密码）；"
                    "若还没缓存票据，先点「授权管理员权限」按钮。",
        })
        hint = sudo_credential_hint()
        if hint:
            task.emit({"type": "line", "stream": "stderr", "text": hint})
    task.argv = argv

    use_pty = task.command in INTERACTIVE_COMMANDS
    master = None
    try:
        if use_pty:
            # 交互式命令挂 pty: Mole 认为自己在真终端里, 才会把确认动作交给人;
            # 我们读到它的确认提示后转成页面上的按钮, 由人点确认。
            master, slave = pty.openpty()
            try:
                # 给个正常的窗口大小, 免得程序按 0x0 终端渲染出错
                try:
                    fcntl.ioctl(slave, termios.TIOCSWINSZ,
                                struct.pack("HHHH", 24, 80, 0, 0))
                except (OSError, AttributeError):
                    pass
                proc = subprocess.Popen(
                    argv,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    cwd=os.path.expanduser("~"),
                    env=mole_env(),
                    close_fds=True,
                    process_group=0,
                )
            finally:
                os.close(slave)
            task.master = master
        else:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.path.expanduser("~"),
                env=mole_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                process_group=0,
            )
    except OSError as exc:
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        task.emit({"type": "line", "stream": "stderr", "text": "无法启动进程: %s" % exc})
        task.exit_code = 127
        task.elapsed_ms = int((time.time() - started) * 1000)
        task.finished = True
        task.emit({"type": "end", "exit_code": 127, "elapsed_ms": task.elapsed_ms,
                   "aborted": False, "error": str(exc)})
        return

    task.proc = proc

    def pump(stream_name, pipe):
        try:
            for line in pipe:
                line = line.rstrip("\n").rstrip("\r")
                task.emit({"type": "line", "stream": stream_name, "text": translate_text(line)})
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def pump_pty():
        """读 pty 主端。按行输出, 并识别 Mole 的确认提示。

        Mole 的提示是 echo -ne 打印的, **不带换行**, 所以不能靠等行首来发现 ——
        只能在残留文本里直接匹配提示语(先剥 ANSI 色码)。

        提示可能出现**多次**: uninstall 里选 "d 选个人数据" 后会回到循环顶部
        再问一次, 所以这里不能只报一次 —— 每匹配到一段新提示就报一次。
        """
        pending = ""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        sudo_reported = False
        try:
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                task.last_pty_activity = time.time()
                pending += decoder.decode(chunk)
                if len(pending) > 262144:
                    pending = pending[-65536:]
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    line = _terminal_line(line)
                    if line.strip():
                        task.emit({"type": "line", "stream": "stdout",
                                   "text": translate_text(line)})
                clean = strip_ansi(pending)
                if not clean.strip():
                    continue
                if CONFIRM_PROMPT_RE.search(clean):
                    prompt = clean.strip()
                    # 整段提示已经被原样报出去了, 清掉, 避免后面被当成新行重复输出
                    pending = ""
                    task.emit({"type": "line", "stream": "stdout",
                               "text": translate_text(prompt)})
                    _delayed_confirm(task, prompt)
                    continue
                if not sudo_reported and SUDO_PROMPT_RE.search(clean):
                    sudo_reported = True
                    task.emit({"type": "sudo_prompt", "command": task.command,
                               "prompt": clean.strip()})
        except Exception:
            pass

    if use_pty:
        threading.Thread(target=pump_pty, daemon=True).start()
    else:
        t_out = threading.Thread(target=pump, args=("stdout", proc.stdout), daemon=True)
        t_err = threading.Thread(target=pump, args=("stderr", proc.stderr), daemon=True)
        t_out.start()
        t_err.start()

    try:
        code = proc.wait(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        code = 124
        task.emit({"type": "line", "stream": "stderr",
                   "text": "[WebUI] 执行超时（%d 秒）已中止" % RUN_TIMEOUT})

    if not use_pty:
        t_out.join(timeout=5)
        t_err.join(timeout=5)
    if task.master is not None:
        try:
            os.close(task.master)
        except OSError:
            pass
        task.master = None
    task.exit_code = code
    task.elapsed_ms = int((time.time() - started) * 1000)
    task.finished = True
    task.emit({
        "type": "end",
        "exit_code": code,
        "elapsed_ms": task.elapsed_ms,
        "aborted": task.aborted,
    })


# ---------------------------------------------------------------- 配置读写

# 白名单 / purge_paths 的读改写必须串行, 否则并发请求会互相覆盖
WRITE_LOCK = threading.Lock()

WHITELIST_HEADERS = {
    "cache": (
        "# Mole 白名单 - 受保护的路径不会被删除\n"
        "# 保存的选择会替换可选默认项；内置的安全保护仍然生效。\n"
        "# 每行添加一个模式以保护相应项目。"
    ),
    "project": "# Mole 优化白名单 - 优化时将跳过这些检查",
}


def _whitelist_path(kind):
    return WHITELIST_CACHE if kind == "cache" else WHITELIST_PROJECT


def read_whitelist(kind):
    """读取白名单条目"""
    kind = "cache" if kind == "cache" else "project"
    path = _whitelist_path(kind)
    if not os.path.exists(path):
        return {"kind": kind, "path": path, "exists": False, "entries": [], "raw": ""}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    entries = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(stripped)
    return {"kind": kind, "path": path, "exists": True, "entries": entries, "raw": raw}


# ---------------------------------------------------------------- 白名单格式（Mole 侧真实规则）
#
# 下列规则来自只读的 Mole 源码（~/.config/mole）, WebUI 写入时按同一套规则做规范化与校验,
# 避免写出 Mole 读不进去的行。
#
# 1) clean 白名单   ~/.config/mole/whitelist
#    读取方: lib/core/base.sh 的 load_mole_whitelist()
#    - 每行一个模式; 跳过空行与 # 开头的注释行
#    - ~ / $HOME / ${HOME} 会被展开成用户目录
#    - 含 ".."           -> 警告 "Path traversal not allowed" 并跳过
#    - 含控制字符         -> 警告 "Invalid path format" 并跳过
#    - 不是绝对路径       -> 警告 "Must be absolute path" 并跳过
#    - 含连续斜杠 "//"   -> 警告 "Consecutive slashes" 并跳过
#    - 系统关键路径       -> 警告 "Protected system path" 并跳过
#    - 允许 glob 通配符（* ? []）
#
# 2) optimize 白名单  ~/.config/mole/whitelist_optimize
#    读取方: lib/manage/whitelist.sh 的 load_whitelist()
#    - 每行一个 pattern（整行原样使用, 只跳过空行与 # 注释行）
#    - pattern 精确匹配优化任务 ID（如 cache_refresh）-> 整个任务被跳过
#    - pattern 也可以是路径 / 路径 glob -> 该路径不被优化流程处理
#    注意: "名称|模式|分类" 三段式只是 Mole 内置清单（get_all_cache_items /
#    get_optimize_whitelist_items）的格式, 配置文件始终是单列 pattern。
#    写成三段式会让整行既匹配不上路径也匹配不上任务 ID, 完全失效。

FINDER_METADATA_SENTINEL = "FINDER_METADATA"

# Mole 内置清单里优化任务 ID 的命名风格（lib/optimize/catalog.sh）
OPTIMIZE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

# ---------------------------------------------------------------- Mole 内置清单（只读镜像）
#
# 下面两张表逐字抄自 ~/.config/mole 的只读源码, 用来让 WebUI 的校验跟 Mole 的真实
# 语义对齐。**Mole 升级后必须重新核对这两张表**（各表的注释里写了来源位置）。

# Mole 的「可选默认保护项」, 来自 lib/core/base.sh 的 DEFAULT_WHITELIST_PATTERNS
# （base.sh 中 declare -a DEFAULT_WHITELIST_PATTERNS=(...) 的全部条目）。
# 语义: Mole 的白名单文件一旦存在, load_mole_whitelist() 就只读这个文件,
# **不再回落到 DEFAULT_WHITELIST_PATTERNS**。所以 WebUI 首次创建 whitelist 时
# 必须把它们一并写进去, 否则用户第一次加条目就会失去 gradle / ollama /
# playwright 等缓存的保护。
# 写法保持 base.sh 的 $HOME 形式: Mole 菜单保存的也是这种写法, 而
# patterns_equivalent() 是字面比较（只展开 ~, 不展开 $HOME）, 写成 $HOME 才能
# 和 Mole 菜单里的行对上。Mole 读取时会自己把 $HOME 展开成绝对路径。
# 注意: DEFAULT_WHITELIST_PATTERNS 末尾的 $FINDER_METADATA_SENTINEL 没有收录 ——
# 它属于**硬安全项**（SAFETY_WHITELIST_PATTERNS / ensure_safety_whitelist_patterns）,
# 由 Mole 自己无条件合并, WebUI 再写一遍只会重复。
MOLE_DEFAULT_WHITELIST_PATTERNS = (
    "$HOME/Library/Caches/ms-playwright*",
    "$HOME/.gradle/caches/*",
    "$HOME/.gradle/daemon/*",
    "$HOME/.ollama/models/*",
    "$HOME/Library/Caches/com.nssurge.surge-mac/*",
    "$HOME/Library/Application Support/com.nssurge.surge-mac/*",
    "$HOME/Library/Caches/org.R-project.R/R/renv/*",
    "$HOME/Library/Caches/JetBrains*",
    "$HOME/Library/Caches/com.jetbrains.toolbox*",
    "$HOME/Library/Caches/tealdeer/tldr-pages",
    "$HOME/Library/Application Support/JetBrains*",
    "$HOME/Library/Caches/com.apple.finder",
    "$HOME/Library/Mobile Documents*",
)

# Mole 内置的优化任务 ID, 来自 lib/optimize/catalog.sh 的 MOLE_OPTIMIZE_ACTIONS
# （每个 _optimize_catalog_register 调用的第一个参数, 逐字照抄, 共 19 个）。
# 只有这张表里的 ID 才是「优化任务 ID」; 相对路径或者随便一个标识符写进
# whitelist_optimize, 既匹配不上路径也匹配不上任务, 条目完全惰性。
MOLE_OPTIMIZE_TASK_IDS = (
    "cache_refresh",
    "saved_state_cleanup",
    "fix_broken_configs",
    "network_optimization",
    "sqlite_vacuum",
    "prevent_network_dsstore",
    "legacy_overrides_audit",
    "network_stack_optimize",
    "disk_permissions_repair",
    "spotlight_index_optimize",
    "spotlight_orphan_rules_cleanup",
    "periodic_maintenance",
    "shared_file_list_repair",
    "disk_verify",
    "login_items_audit",
    "quarantine_cleanup",
    "launch_agents_cleanup",
    "notification_cleanup",
    "coreduet_cleanup",
)

# Mole 明确拒绝加入白名单的系统关键路径（lib/core/base.sh load_mole_whitelist）
PROTECTED_SYSTEM_PATHS = ("/", "/System", "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/etc", "/var/db")


def _wl_expand_home(pattern):
    """把 ~ / $HOME / ${HOME} 展开成绝对路径, 保留 glob 通配符。"""
    home = USER_HOME
    if pattern == "~":
        return home
    if pattern.startswith("~/"):
        pattern = home + pattern[1:]
    pattern = pattern.replace("${HOME}", home)
    pattern = pattern.replace("$HOME", home)
    return pattern


def _wl_equivalent(a, b):
    """
    两条白名单条目是否等价（展开 home 后逐字比较）。

    展开覆盖四种写法, 彼此视为同一条:
      $HOME/x   ~/x   ${HOME}/x   /Users/<you>/x
    注意 Mole 自己的 patterns_equivalent() 只展开 ~ 不展开 $HOME（字面比较）,
    这里比 Mole 更宽, 目的是让 WebUI 能认出「同一条的两种写法」。
    """
    return _wl_expand_home(a) == _wl_expand_home(b)


def _wl_contract_home(path):
    """
    把绝对路径里的用户家目录前缀还原成 $HOME（Mole 菜单里的写法）。

    Mole 菜单保存的 pattern 是 $HOME/... 形式, 且 patterns_equivalent() 只展开 ~,
    不展开 $HOME —— 所以文件里写绝对路径虽然保护照样生效（Mole 读文件时会展开）,
    但在菜单里对不上那一行, 勾选状态显示为未勾。写入前统一还原成 $HOME/...。

    只还原用户家目录前缀, 其余部分（含 glob 通配符）原样保留。
    """
    home = USER_HOME.rstrip("/")
    if not home or home == "/":
        return path
    if path == home:
        return "$HOME"
    if path.startswith(home + "/"):
        return "$HOME" + path[len(home):]
    return path


def _wl_has_control_char(text):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _wl_is_protected_system_path(path):
    for base in PROTECTED_SYSTEM_PATHS:
        if path == base or path.startswith(base + "/"):
            return True
    return False


def _validate_path_entry(path):
    """校验一条路径型白名单条目, 与 Mole 的跳过规则一一对应。"""
    if ".." in path:
        return "不允许路径穿越（..）, Mole 会跳过这一行"
    if _wl_has_control_char(path):
        return "路径含有无效的控制字符, Mole 会跳过这一行"
    if not path.startswith("/"):
        return "必须是绝对路径（以 / 开头）或 ~ 开头的路径, 相对路径会被 Mole 跳过"
    if "//" in path:
        return "路径不能包含连续的斜杠（//）, Mole 会跳过这一行"
    if _wl_is_protected_system_path(path):
        return "这是系统关键路径, Mole 会拒绝把它加入白名单"
    return None


def _validate_clean_entry(raw):
    """校验 clean 白名单条目, 返回 (规范化后的条目, 错误信息)。"""
    expanded = _wl_expand_home(raw)
    if expanded == FINDER_METADATA_SENTINEL:
        return expanded, None
    return expanded, _validate_path_entry(expanded)


def _validate_optimize_entry(raw):
    """
    校验 optimize 白名单条目, 返回 (规范化后的条目, 错误信息)。

    optimize 白名单按整行精确匹配, 一行有两种合法含义:
      - 路径 / 路径 glob -> 该路径不被优化流程处理
      - 优化任务 ID      -> 整个优化任务被跳过
    任务 ID 必须是 Mole 内置的那张表（MOLE_OPTIMIZE_TASK_IDS）里的一个。
    否则这行既不是路径也不是任务, 写进去完全惰性 —— 用户却会以为保护生效了。
    """
    expanded = _wl_expand_home(raw)
    if expanded.startswith("/"):
        return expanded, _validate_path_entry(expanded)
    if expanded in MOLE_OPTIMIZE_TASK_IDS:
        return expanded, None
    if OPTIMIZE_TASK_ID_RE.match(expanded):
        return expanded, (
            "「%s」不是 Mole 内置的优化任务 ID。可用 ID: %s。"
            "或者改填绝对路径（~ 开头也可以）。"
            % (expanded, ", ".join(MOLE_OPTIMIZE_TASK_IDS)))
    return expanded, (
        "optimize 白名单只接受绝对路径（~ 开头也可以）或 Mole 内置的优化任务 ID"
        "（如 cache_refresh）。相对路径既不是路径也不是任务 ID, 写进去不会生效。")


def write_whitelist(kind, action, pattern):
    """
    增删白名单条目。文件不存在时按 Mole 的 header 约定新建; 写前备份。
    """
    kind = "cache" if kind == "cache" else "project"
    path = _whitelist_path(kind)
    pattern = (pattern or "").strip()
    if not pattern:
        return {"ok": False, "error": "pattern 不能为空"}
    if "\n" in pattern:
        return {"ok": False, "error": "pattern 不能包含换行"}
    if action not in ("add", "remove"):
        return {"ok": False, "error": "action 必须是 add 或 remove"}
    # optimize 白名单按整行精确匹配, 三段式会让整行匹配不上任何东西, 直接拒绝。
    # 只拦 add: remove 只是用来定位文件里已有的那一行, 不该被格式校验挡住
    # （否则文件里残留的三段式条目永远删不掉）。
    if kind == "project" and action == "add" and pattern.count("|") == 2:
        return {"ok": False, "error": (
            "optimize 白名单按整行精确匹配，不支持「名称|模式|分类」三段式。"
            "请只填绝对路径（~ 开头也可以）或单个优化任务 ID（如 cache_refresh）。")}

    # 写入前按 Mole 侧的规则规范化并校验, 避免写出 Mole 读不进去的行。
    # remove 只用于定位已有条目, 不做有效性校验 —— 否则文件里遗留的无效行
    # （例如相对路径）会因为校验失败而永远删不掉。
    if kind == "cache":
        normalized, verr = _validate_clean_entry(pattern)
    else:
        normalized, verr = _validate_optimize_entry(pattern)
    if verr and action == "add":
        return {"ok": False, "error": verr}

    with WRITE_LOCK:
        entries = []
        file_existed = os.path.exists(path)
        if file_existed:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh.read().splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    entries.append(stripped)

        if action == "add":
            # 写入统一用 $HOME/... 形式，与 Mole 菜单里的 pattern 写法一致。
            # Mole 的 patterns_equivalent() 只展开 ~ 不展开 $HOME —— 写绝对路径
            # 虽然保护照样生效（读文件时会展开），但菜单里对不上那一行、显示为未勾。
            normalized = _wl_contract_home(normalized)
            if any(_wl_equivalent(e, normalized) for e in entries):
                # 已有等价条目：既不重复写，也不改动文件
                return {"ok": True, "kind": kind, "path": path, "changed": False,
                        "entries": entries, "message": "该条目已存在"}
            # 首次创建 clean 白名单时, 把 Mole 的「可选默认保护项」一并写进去。
            # Mole 的规则是「白名单文件一旦存在就只读这个文件, 不再回落到
            # DEFAULT_WHITELIST_PATTERNS」, 所以这里必须替用户把默认项带上,
            # 否则第一次加一条就会让 gradle / ollama / playwright 等缓存失去保护。
            # 文件已经存在时只追加用户这一条, 不动已有内容。
            if kind == "cache" and not file_existed:
                for default_pattern in MOLE_DEFAULT_WHITELIST_PATTERNS:
                    if any(_wl_equivalent(e, default_pattern) for e in entries):
                        continue
                    entries.append(default_pattern)
            entries.append(normalized)
            changed = True
        else:
            # 移除不存在的条目 = 无操作：不写文件、更不创建本不该存在的文件。
            # Mole 的规则是「白名单文件一旦存在就只读这个文件」，
            # 凭空造一个空文件会让它的默认可选保护项整体消失。
            if not file_existed:
                return {"ok": True, "kind": kind, "path": path, "changed": False,
                        "entries": [], "message": "白名单文件不存在，无需移除"}
            remaining = [e for e in entries if not _wl_equivalent(e, normalized)]
            if len(remaining) == len(entries):
                return {"ok": True, "kind": kind, "path": path, "changed": False,
                        "entries": entries, "message": "没有匹配的条目，文件未改动"}
            entries = remaining
            changed = True

        # 写前备份
        backup = None
        if os.path.exists(path):
            backup = path + ".bak"
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                old = fh.read()
            with open(backup, "w", encoding="utf-8") as fh:
                fh.write(old)
            os.chmod(backup, 0o600)

        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(WHITELIST_HEADERS[kind] + "\n")
            if entries:
                fh.write("\n")
                for entry in entries:
                    fh.write(entry + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    return {"ok": True, "kind": kind, "path": path, "backup": backup,
            "entries": entries, "changed": changed}


def read_purge_paths():
    """读取 purge_paths 配置"""
    if not os.path.exists(PURGE_PATHS_FILE):
        return {"exists": False, "path": PURGE_PATHS_FILE, "paths": [], "raw": ""}
    with open(PURGE_PATHS_FILE, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    paths = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        paths.append(stripped)
    return {"exists": True, "path": PURGE_PATHS_FILE, "paths": paths, "raw": raw}


def _dir_size_bytes(path, deadline):
    """统计目录占用; 超过 deadline 就停。返回 (字节数, 是否被截断)"""
    total = 0
    for root, dirs, files in os.walk(path):
        if time.time() > deadline:
            return total, True
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total, False


def scan_worktree_containers():
    """
    列出 Mole 1.71.0 起会自动纳入 purge 扫描范围的 AI agent worktree 容器。

    Mole 的默认扫描路径 (lib/clean/purge_shared.sh 的
    MOLE_PURGE_DEFAULT_SEARCH_PATHS) 含三个固定容器, 另有每个项目根下
    6 层深的 .claude/worktrees。默认路径与 purge_paths 的自定义项是并集
    (lib/clean/caches.sh 同时喂了两份), 所以这些容器不需要额外配置就会
    被扫到 —— 这里只是把它们显式列出来, 让人知道扫到哪、里面有多少东西。

    只读: 只 stat 与求和, 不删除、不修改任何文件。
    """
    deadline = time.time() + WORKTREE_SCAN_TIMEOUT
    containers = []
    seen = set()

    def add(path, owner, kind):
        expanded = os.path.expanduser(path)
        key = os.path.realpath(expanded)
        if key in seen:
            return
        seen.add(key)
        exists = os.path.isdir(expanded)
        entries = 0
        size = 0
        truncated = False
        if exists:
            try:
                entries = len([n for n in os.listdir(expanded) if not n.startswith(".")])
            except OSError:
                entries = 0
            size, truncated = _dir_size_bytes(expanded, deadline)
        containers.append({
            "path": path,
            "owner": owner,
            "kind": kind,
            "exists": exists,
            "entries": entries,
            "size_bytes": size,
            "truncated": truncated,
        })

    for path, owner in WORKTREE_FIXED_CONTAINERS:
        add(path, owner, "fixed")

    for name in WORKTREE_PROJECT_ROOTS:
        root_path = os.path.join(USER_HOME, name)
        if not os.path.isdir(root_path):
            continue
        base_depth = root_path.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, _files in os.walk(root_path):
            if time.time() > deadline:
                break
            if dirpath.count(os.sep) - base_depth >= 6:
                dirnames[:] = []
                continue
            if os.path.basename(dirpath) == ".claude" and "worktrees" in dirnames:
                add(os.path.join(dirpath, "worktrees"), "Claude Code", "project")
                dirnames[:] = [d for d in dirnames if d != "worktrees"]

    return {
        "containers": containers,
        "scanned_roots": WORKTREE_PROJECT_ROOTS,
        "timeout": WORKTREE_SCAN_TIMEOUT,
    }


def write_purge_paths(paths):
    """覆写 purge_paths, 写前先备份"""
    if not isinstance(paths, list):
        return {"ok": False, "error": "paths 必须是数组"}
    cleaned = []
    for item in paths:
        item = str(item).strip()
        if not item:
            continue
        if "\n" in item:
            return {"ok": False, "error": "路径不能包含换行"}
        cleaned.append(item)

    header = (
        "# Mole Purge Paths - Auto-discovered project directories\n"
        "# Edit this file to customize, or run: mo purge --paths\n"
        "# Add one path per line (supports ~ for home directory)\n"
        "\n"
    )
    with WRITE_LOCK:
        backup = None
        if os.path.exists(PURGE_PATHS_FILE):
            backup = PURGE_PATHS_FILE + ".bak"
            with open(PURGE_PATHS_FILE, "r", encoding="utf-8", errors="replace") as fh:
                old = fh.read()
            with open(backup, "w", encoding="utf-8") as fh:
                fh.write(old)
            os.chmod(backup, 0o600)

        tmp = PURGE_PATHS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(header)
            for item in cleaned:
                fh.write(item + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, PURGE_PATHS_FILE)
    return {"ok": True, "path": PURGE_PATHS_FILE, "backup": backup, "paths": cleaned}


# ---------------------------------------------------------------- 菜单与帮助文案

HELP_CACHE = {}
HELP_LOCK = threading.Lock()


def collect_help(command):
    """采集 mo <cmd> --help 的中文原文"""
    with HELP_LOCK:
        if command in HELP_CACHE:
            return HELP_CACHE[command]
    out, err, _code, _ms = run_sync([MOLE_BIN, command, "--help"], timeout=30)
    text = strip_ansi(out) if out.strip() else strip_ansi(err)
    text = translate_text(text).strip()
    with HELP_LOCK:
        HELP_CACHE[command] = text
    return text


def mole_version():
    out, _err, _code, _ms = run_sync([MOLE_BIN, "--version"], timeout=15)
    for line in out.splitlines():
        m = re.search(r"Mole version ([0-9.]+)", line)
        if m:
            return m.group(1)
    return "unknown"


def update_notice():
    """读取本地缓存的更新提示（只读, 不触发联网检查）"""
    try:
        if os.path.exists(UPDATE_MESSAGE_FILE):
            with open(UPDATE_MESSAGE_FILE, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
    except Exception:
        pass
    return ""


# 非交互环境下必须显式确认才能执行的命令: {命令: 必需标志}
# WebUI 的子进程 stdin 是 /dev/null, 没有终端可交互 —— 这些命令在 WebUI 里
# 唯一的执行方式就是带上那个标志。实际 argv 会随任务的 start 事件回传,
# 页面上显示的就是真实执行的命令, 不会与实际不符。
NONINTERACTIVE_CONFIRM_FLAGS = {
    # purge 没有 --yes 就直接拒绝:
    # "Purge requires confirmation. Run mo purge in a terminal, or use
    #  --dry-run to preview and --yes to confirm unattended cleanup."
    "purge": "--yes",
}


def _apply_noninteractive_flags(command, argv):
    """给必须显式确认的命令补上确认标志 (只改 argv, 不执行)"""
    flag = NONINTERACTIVE_CONFIRM_FLAGS.get(command)
    if flag and flag not in argv:
        argv = list(argv) + [flag]
    return argv


def build_menu():
    commands = []
    version = mole_version()
    for key, spec in COMMANDS.items():
        confirm = spec.get("confirm", "")
        if confirm:
            confirm = confirm.replace("{version}", version)
        commands.append({
            "key": key,
            "title": spec["title"],
            "summary": spec["summary"],
            "usage": "mo %s" % key,
            "help": collect_help(key),
            "flags": spec["bool_flags"] + spec["value_flags"],
            "positional": spec["positional"],
            "positional_allowed": spec.get("positional_allowed", []),
            "danger": spec["danger"],
            "sudo": spec["sudo"],
            "confirm": confirm,
        })
    return {
        "version": version,
        "primary": PRIMARY_MENU,
        "commands": commands,
        "update_notice": update_notice(),
    }


# ---------------------------------------------------------------- HTTP 处理

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MoleWebUI/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ------------------------------------------------ 基础工具

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data, content_type, code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        return json.loads(raw.decode("utf-8"))

    def _query(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    # ------------------------------------------------ GET

    def do_GET(self):
        path, query = self._query()
        try:
            if path == "/api/health":
                return self._api_health()
            if path == "/api/menu":
                return self._send_json(build_menu())
            if path == "/api/status":
                return self._api_status(query)
            if path == "/api/status/stream":
                return self._api_status_stream(query)
            if path == "/api/history":
                return self._api_history(query)
            if path == "/api/analyze":
                return self._api_analyze(query)
            if path == "/api/apps":
                return self._api_apps()
            if path == "/api/whitelist":
                return self._api_whitelist_get(query)
            if path == "/api/purge/paths":
                return self._send_json(read_purge_paths())
            if path == "/api/worktrees":
                return self._send_json(scan_worktree_containers())
            if path == "/api/stream":
                return self._api_stream(query)
            if path in ("/", "/index.html"):
                return self._api_index()
            return self._send_json({"error": "not found", "path": path}, 404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc), "type": type(exc).__name__}, 500)

    def _api_index(self):
        index = os.path.join(WEB_DIR, "index.html")
        if not os.path.exists(index):
            return self._send_json({"error": "index.html 不存在"}, 404)
        with open(index, "rb") as fh:
            data = fh.read()
        return self._send_bytes(data, "text/html; charset=utf-8")

    def _api_health(self):
        return self._send_json({
            "ok": True,
            "mole_version": mole_version(),
            "install_dir": INSTALL_DIR,
            "python_version": sys.version.split()[0],
            "mole_bin": MOLE_BIN,
            "update_notice": update_notice(),
            "sudo_cached": _sudo_cached(),
            "has_controlling_tty": _has_controlling_tty(),
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    def _api_status(self, _query):
        argv = [MOLE_BIN, "status", "--json"]
        out, err, code, ms = run_sync(argv, timeout=60)
        payload = {"command": " ".join(argv), "exit_code": code, "elapsed_ms": ms,
                   "stderr": strip_ansi(err)}
        try:
            payload["data"] = json.loads(out)
        except Exception:
            payload["data"] = None
            payload["stdout"] = strip_ansi(out)
        return self._send_json(payload)

    def _api_status_stream(self, query):
        """mo status --watch --interval 2s 的 NDJSON 实时流 (SSE 封装)"""
        interval = (query.get("interval") or ["2s"])[0]
        if not re.match(r"^[0-9]+(\.[0-9]+)?(ms|s|m|h)?$", interval):
            interval = "2s"
        argv = [MOLE_BIN, "status", "--watch", "--interval", interval]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=os.path.expanduser("~"), env=mole_env(),
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                process_group=0,
            )
        except OSError as exc:
            self.wfile.write(("event: error\ndata: %s\n\n" %
                              json.dumps({"error": str(exc)}, ensure_ascii=False)).encode("utf-8"))
            self.wfile.flush()
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except Exception:
                    continue
                self.wfile.write(("event: snapshot\ndata: %s\n\n" % line).encode("utf-8"))
                self.wfile.flush()
        except Exception:
            pass
        finally:
            _kill_group(proc)

    def _api_history(self, query):
        limit_raw = (query.get("limit") or ["20"])[0]
        try:
            limit = int(limit_raw)
        except ValueError:
            limit = 20
        limit = max(1, min(200, limit))
        argv = [MOLE_BIN, "history", "--json", "--limit", str(limit)]
        out, err, code, ms = run_sync(argv, timeout=60)
        payload = {"command": " ".join(argv), "exit_code": code, "elapsed_ms": ms,
                   "stderr": strip_ansi(err)}
        try:
            payload["data"] = json.loads(out)
        except Exception:
            payload["data"] = None
            payload["stdout"] = strip_ansi(out)
        return self._send_json(payload)

    def _api_analyze(self, query):
        target = (query.get("path") or [""])[0].strip()
        if target:
            if not target.startswith("/"):
                return self._send_json({"error": "path 必须是绝对路径"}, 400)
            if not os.path.exists(target):
                return self._send_json({"error": "路径不存在: %s" % target}, 400)
            argv = [ANALYZE_BIN, "--json", target]
        else:
            argv = [ANALYZE_BIN, "--json"]
        out, err, code, ms = run_sync(argv, timeout=300)
        payload = {"command": " ".join(argv), "exit_code": code, "elapsed_ms": ms,
                   "stderr": strip_ansi(err)}
        try:
            payload["data"] = json.loads(out)
        except Exception:
            payload["data"] = None
            payload["stdout"] = strip_ansi(out)
        return self._send_json(payload)

    def _api_apps(self):
        """mo uninstall --list —— 输出是 JSON 数组"""
        argv = [MOLE_BIN, "uninstall", "--list"]
        out, err, code, ms = run_sync(argv, timeout=120)
        payload = {"command": " ".join(argv), "exit_code": code, "elapsed_ms": ms,
                   "stderr": strip_ansi(err)}
        try:
            payload["apps"] = json.loads(out)
        except Exception:
            payload["apps"] = []
            payload["stdout"] = strip_ansi(out)
        return self._send_json(payload)

    def _api_whitelist_get(self, query):
        kind = (query.get("kind") or ["cache"])[0]
        if kind not in ("cache", "project"):
            return self._send_json({"error": "kind 必须是 cache 或 project"}, 400)
        payload = read_whitelist(kind)
        if kind == "project":
            # 把合法的优化任务 ID 一并给前端, 让用户照着填而不是瞎猜
            payload["optimize_task_ids"] = list(MOLE_OPTIMIZE_TASK_IDS)
        return self._send_json(payload)

    def _api_stream(self, query):
        task_id = (query.get("task") or [""])[0]
        task = TASKS.get(task_id)
        if task is None:
            return self._send_json({"error": "task 不存在"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                try:
                    payload = task.q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if payload is None:
                    break
                event = payload.pop("type", "message")
                data = json.dumps(payload, ensure_ascii=False)
                self.wfile.write(("event: %s\ndata: %s\n\n" % (event, data)).encode("utf-8"))
                self.wfile.flush()
                if event == "end":
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ------------------------------------------------ POST

    def do_POST(self):
        path, _query = self._query()
        try:
            if path == "/api/readonly":
                return self._api_readonly()
            if path == "/api/run":
                return self._api_run()
            if path == "/api/whitelist":
                return self._api_whitelist_post()
            if path == "/api/purge/paths":
                return self._api_purge_paths_post()
            if path == "/api/sudo/auth":
                return self._api_sudo_auth()
            if path.startswith("/api/task/") and path.endswith("/confirm"):
                task_id = path[len("/api/task/"):-len("/confirm")]
                body = self._read_json_body()
                action = str(body.get("action") or "")
                result, err = send_task_confirm(task_id, action)
                if err:
                    return self._send_json({"error": err}, 400)
                return self._send_json(result)
            if path.startswith("/api/task/") and path.endswith("/abort"):
                task_id = path[len("/api/task/"):-len("/abort")]
                task = TASKS.get(task_id)
                if task is None:
                    return self._send_json({"error": "task 不存在"}, 404)
                ok = task.abort()
                return self._send_json({"ok": ok, "task_id": task_id})
            return self._send_json({"error": "not found", "path": path}, 404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc), "type": type(exc).__name__}, 500)

    def _api_readonly(self):
        body = self._read_json_body()
        command = str(body.get("command") or "")
        args = body.get("args") or []
        if command not in COMMANDS:
            return self._send_json({"error": "不允许的命令: %s" % command}, 400)
        spec = COMMANDS[command]
        argv, err = validate_args(command, args)
        if err:
            return self._send_json({"error": err}, 400)
        # 只读查询端点: 只允许跑支持 --dry-run 的命令。
        # 不支持 --dry-run 的命令一旦执行就是真的动手 (例如 update 会覆盖安装),
        # 这里一律拒绝, 让调用方走「真正执行」的任务通道。
        if not spec["dry_run"] and spec["danger"] in ("high", "medium"):
            return self._send_json({
                "error": "%s 不支持只读执行；请直接执行或先在终端确认。" % command
            }, 400)
        # 强制加 --dry-run, 保证这个端点永远不会真的改动系统
        # argv 的形状固定是 [MOLE_BIN, command, ...参数]（见 validate_args）：
        # argv[0] 是可执行文件, argv[1] 是子命令。Mole 入口按第一个参数分派子命令,
        # --dry-run 必须排在子命令之后; 插到 argv[0] 之后会变成 "mo --dry-run <cmd>"
        # 而被入口判为未知命令。
        if spec["dry_run"] and "--dry-run" not in argv and "-n" not in argv:
            argv = argv[:2] + ["--dry-run"] + argv[2:]
        out, errout, code, ms = run_sync(argv, timeout=READONLY_TIMEOUT)
        return self._send_json({
            "command": " ".join(argv),
            "stdout": translate_text(strip_ansi(out)),
            "stderr": translate_text(strip_ansi(errout)),
            "exit_code": code,
            "elapsed_ms": ms,
            "parsed": parse_output(out),
        })

    def _api_run(self):
        body = self._read_json_body()
        command = str(body.get("command") or "")
        args = body.get("args") or []
        sudo = bool(body.get("sudo"))
        if command not in COMMANDS:
            return self._send_json({"error": "不允许的命令: %s" % command}, 400)
        task_id, err = start_task(command, args, sudo)
        if err:
            return self._send_json({"error": err}, 400)
        return self._send_json({"task_id": task_id})

    def _api_whitelist_post(self):
        body = self._read_json_body()
        kind = str(body.get("kind") or "cache")
        action = str(body.get("action") or "")
        pattern = str(body.get("pattern") or "")
        if kind not in ("cache", "project"):
            return self._send_json({"error": "kind 必须是 cache 或 project"}, 400)
        result = write_whitelist(kind, action, pattern)
        return self._send_json(result, 200 if result.get("ok") else 400)

    def _api_purge_paths_post(self):
        body = self._read_json_body()
        result = write_purge_paths(body.get("paths") or [])
        return self._send_json(result, 200 if result.get("ok") else 400)

    def _api_sudo_auth(self):
        ok, message = _request_touch_id()
        payload = {"ok": ok, "message": message, "sudo_cached": _sudo_cached()}
        if not ok:
            payload["error"] = message
        return self._send_json(payload, 200 if ok else 400)


def main():
    if not os.path.exists(MOLE_BIN):
        sys.stderr.write("[WebUI] 警告: 未找到 %s\n" % MOLE_BIN)
    atexit.register(_cleanup_running_tasks)
    if _has_controlling_tty():
        threading.Thread(target=_sudo_keepalive_loop, daemon=True).start()
        print("[WebUI] 已启用 sudo 票据续期（每 %d 秒刷新一次）" % SUDO_KEEPALIVE_INTERVAL)
    else:
        print("[WebUI] 警告: 本服务没有控制终端，Mole 提权会失败。")
        print("[WebUI] 请在终端前台运行 run.sh，不要用 nohup 或后台方式启动。")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print("=" * 58)
    print("  Mole WebUI 已启动")
    print("  访问地址: http://%s:%d" % (HOST, PORT))
    print("  Mole 版本: %s" % mole_version())
    print("  安装目录: %s" % INSTALL_DIR)
    print("  仅监听本机回环地址，按 Ctrl+C 停止")
    print("=" * 58)
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[WebUI] 已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
