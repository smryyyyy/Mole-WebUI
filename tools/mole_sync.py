#!/usr/bin/env python3
"""Mole WebUI 与 Mole CLI 的同步工具(纯标准库, 无副作用除非显式写入)。

子命令:
  check    核对 WebUI 的命令白名单与当前安装的 Mole 是否一致
  collect  采集 Mole 真实输出, 算出汉化缺口, 产出待翻译清单
  merge    把翻译结果合并回 i18n/mole_zh.json 与 i18n/mole_zh_norm.json
  report   统计当前汉化覆盖率

用法:
  python3 tools/mole_sync.py check
  python3 tools/mole_sync.py collect --out /tmp/mole_sync
  python3 tools/mole_sync.py merge --todo /tmp/mole_sync/todo.json --translated /tmp/mole_sync/zh.json
  python3 tools/mole_sync.py report
"""
import argparse
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
SERVER = BASE / "server.py"
MOLE_BIN = "/usr/local/bin/mole"
I18N_DIR = BASE / "i18n"
MAIN_TABLE = I18N_DIR / "mole_zh.json"
NORM_TABLE = I18N_DIR / "mole_zh_norm.json"

# 采集真实输出用的用例: (名称, 参数)
CASES = [
    ("clean", ["clean", "--dry-run"]),
    ("purge", ["purge", "--dry-run"]),
    ("optimize", ["optimize", "--dry-run"]),
    ("installer", ["installer", "--dry-run"]),
    ("uninstall", ["uninstall", "--dry-run", "LM Studio"]),
    ("slim", ["slim", "--dry-run"]),
    ("touchid", ["touchid", "status"]),
    ("help_analyze", ["analyze", "--help"]),
    ("help_clean", ["clean", "--help"]),
    ("help_history", ["history", "--help"]),
    ("help_installer", ["installer", "--help"]),
    ("help_optimize", ["optimize", "--help"]),
    ("help_purge", ["purge", "--help"]),
    ("help_slim", ["slim", "--help"]),
    ("help_status", ["status", "--help"]),
    ("help_touchid", ["touchid", "--help"]),
    ("help_uninstall", ["uninstall", "--help"]),
    ("help_update", ["update", "--help"]),
]
# 快速模式只跑秒级用例
QUICK = {"help_analyze", "help_clean", "help_history", "help_installer", "help_optimize",
         "help_purge", "help_slim", "help_status", "help_touchid", "help_uninstall",
         "help_update", "touchid", "purge", "installer"}

OPT_LINE = re.compile(
    r"^\s*(?:-{1,2}[A-Za-z][\w-]*(?:\s*,\s*-{1,2}[A-Za-z][\w-]*)*"
    r"|[a-z][\w-]*)(?:\s+[A-Z]{2,})?\s{2,}(.+)$")
CMD_LINE = re.compile(r"^\s*(mo\s+\S+(?:\s+\S+)*?)\s{2,}(.+)$")
NATURAL = re.compile(r"^[A-Za-z][A-Za-z0-9 ,.'\u2019\-\u2013\u2014:;!?%\u00b7\u2022|()/~+&#@$]*$")
HASWORD = re.compile(r"[A-Za-z]{3,}")
LOWER = re.compile(r"\b[a-z]{2,}\b")
SYMPREFIX = re.compile(
    r"^([\u25cb\u2713\u2299\u2699\u25ce\u27a4\u25cd\u25aa\u2022\u2691"
    "\u2605\u2714\u2716\u26a0\u25c9\u2023]+[ \t]*)(.+)$")
PATHTOK = re.compile(r"(?:^|[\s(])(~?/[^\s,;)]*)")
NUM_TOKEN = re.compile(r"\d+(?:\.\d+)?")
MARK = re.compile(r"#(\d+)")
MAX_INLINE = 80

# 已知不适合做行内片段的词条(命令名 / 应用名的一部分), 抽取出时剔除
BLOCK_INLINE = {
    "mo analyze --json", "Support", "Studio",
}


def load_server():
    spec = importlib.util.spec_from_file_location("mole_webui_server", str(SERVER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._load_zh_map()
    return mod


def norm_key(text):
    counter = [0]

    def rep(m):
        out = "#%d" % counter[0]
        counter[0] += 1
        return out

    return NUM_TOKEN.sub(rep, text)


def cmd_check(_args):
    """核对 WebUI 的命令白名单与当前安装的 Mole 是否一致"""
    mod = load_server()
    whitelist = mod.COMMANDS
    proc = subprocess.run([MOLE_BIN, "--help"], capture_output=True, text=True, timeout=30)
    text = proc.stdout + proc.stderr
    cmds = set(re.findall(r"^\s{2}mo\s+([a-z][a-z0-9-]+)\s{2,}", text, re.M))
    cmds.discard("help")
    missing = sorted(cmds - set(whitelist))
    extra = sorted(set(whitelist) - cmds)
    print("Mole 子命令 %d 个 / WebUI 白名单 %d 个" % (len(cmds), len(whitelist)))
    print("Mole 有但 WebUI 没有: %s" % (missing or "无"))
    print("WebUI 有但 Mole 没有: %s" % (extra or "无"))
    print()
    print("--- 各子命令选项核对 (--help 恒定不需白名单) ---")
    problems = 0
    for cmd in sorted(cmds):
        if cmd not in whitelist:
            continue
        res = subprocess.run([MOLE_BIN, cmd, "--help"], capture_output=True, text=True, timeout=30)
        opts = set(re.findall(r"(--[a-z][a-z0-9-]*)", res.stdout + res.stderr))
        spec_ = whitelist.get(cmd) or {}
        allowed = set()
        for key, val in spec_.items():
            if isinstance(val, (list, tuple)):
                allowed |= set(val)
        unknown = sorted(opts - allowed - {"--help"})
        if unknown:
            problems += 1
            print("  !! %-10s 新增未收录选项: %s" % (cmd, unknown))
    if not problems:
        print("  所有子命令的选项都已收录")
    print()
    if missing or extra or problems:
        print("结论: 需要更新 server.py 里的 COMMANDS 白名单")
        return 1
    print("结论: 白名单与当前 Mole 完全一致, 无需改动")
    return 0


def cmd_collect(args):
    """采集真实输出, 算汉化缺口, 产出待翻译清单"""
    mod = load_server()
    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, argv in CASES:
        if args.quick and name not in QUICK:
            continue
        out, err, code, ms = mod.run_sync([MOLE_BIN] + argv, timeout=240)
        (outdir / (name + ".out")).write_text(out or "", encoding="utf-8")
        for line in mod.strip_ansi(out or err or "").split("\n"):
            if line.strip():
                rows.append(line)
        print("  %-16s exit=%-4s %6dms  %d 行" % (name, code, ms, len(out.splitlines())))
    total = len(rows)
    miss = [r for r in rows if mod.translate_text(r).strip() == r.strip()]
    print()
    print("总行 %d, 未译 %d (%.1f%% 已译)" % (total, len(miss), (total - len(miss)) * 100.0 / total))

    norm_items, inline_items, full_items, skipped = {}, {}, {}, []
    for line in miss:
        s = line.rstrip()
        pre = ""
        mp = SYMPREFIX.match(s.strip())
        if mp:
            pre, s = mp.group(1), mp.group(2)
        body = re.sub(r"[*\u2022]\s*/\S+", "", s).strip()
        if not HASWORD.search(body):
            skipped.append(s)
            continue
        mm = OPT_LINE.match(s) or CMD_LINE.match(s)
        if mm:
            desc = mm.group(mm.lastindex).strip()
            if not desc or "  " in desc:
                full_items.setdefault(pre + s, []).append(line)
            elif PATHTOK.search(desc):
                pass
            elif re.search(r"\d", desc):
                norm_items.setdefault(norm_key(desc), []).append(line)
            else:
                inline_items.setdefault(desc, []).append(line)
            continue
        if PATHTOK.search(s):
            for part in re.split(r"(?:~?/[^\s,;)]*)", s):
                part = part.strip()
                if not part or not HASWORD.search(part) or re.search(r"\d", part):
                    continue
                if len(part) <= MAX_INLINE and NATURAL.match(part) and part not in BLOCK_INLINE:
                    inline_items.setdefault(part, []).append(line)
            continue
        if re.search(r"\d", s):
            if LOWER.search(s):
                norm_items.setdefault(norm_key(s), []).append(line)
            else:
                skipped.append(s)
            continue
        if len(s.strip()) <= MAX_INLINE and NATURAL.match(s.strip()):
            if s.strip() not in BLOCK_INLINE:
                inline_items.setdefault(s.strip(), []).append(line)
        else:
            full_items.setdefault(pre + s, []).append(line)

    todo = {"norm": norm_items, "inline": inline_items, "full": full_items}
    dest = outdir / "todo.json"
    dest.write_text(json.dumps(todo, ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print("待翻译清单: norm %d, inline %d, full %d (跳过 %d 行: 纯路径/纯数据)"
          % (len(norm_items), len(inline_items), len(full_items), len(skipped)))
    print("已写出 %s" % dest)
    print()
    print("下一步: 翻译 norm/inline/full 三组(键必须逐字保留, norm 组的 #N 占位符原样保留),")
    print("        译文写成 {\"norm\": {...}, \"inline\": {...}, \"full\": {...}} 后执行:")
    print("        python3 tools/mole_sync.py merge --todo %s --translated <你的译文.json>" % dest)
    return 0


def cmd_merge(args):
    """把翻译结果合并回映射表(带占位符校验, 校验不过的条目直接丢弃)"""
    todo = json.load(open(args.todo, encoding="utf-8"))
    tr = json.load(open(args.translated, encoding="utf-8"))
    main = json.load(open(MAIN_TABLE, encoding="utf-8")) if MAIN_TABLE.exists() else {}
    norm = json.load(open(NORM_TABLE, encoding="utf-8")) if NORM_TABLE.exists() else {}
    added = overwritten = dropped = protected = 0
    for group in ("norm", "inline", "full"):
        src = todo.get(group) or {}
        dst = tr.get(group) or {}
        missing = [k for k in src if k not in dst]
        for k in missing:
            print("  缺译文(跳过): %r" % k[:60])
        for key, val in dst.items():
            if not isinstance(val, str) or not val.strip():
                dropped += 1
                continue
            if group == "norm":
                want = MARK.findall(key)
                got = MARK.findall(val)
                if want != got:
                    dropped += 1
                    print("  占位符不一致(丢弃): %r 原文%s 译文%s" % (key[:50], want, got))
                    continue
                if key.strip() in norm and norm[key.strip()] != val.strip():
                    overwritten += 1
                norm[key.strip()] = val.strip()
            else:
                # 保护: 译文退回英文原样时, 不许覆盖已有的中文译文
                # (例如目录名 Application Support / Time Machine 故意保留英文)
                old = main.get(key.strip())
                if old is not None and val.strip() == key.strip() and old != key.strip():
                    protected += 1
                    continue
                if old is not None and old != val.strip():
                    overwritten += 1
                main[key.strip()] = val.strip()
            added += 1
    I18N_DIR.mkdir(parents=True, exist_ok=True)
    MAIN_TABLE.write_text(json.dumps(main, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    NORM_TABLE.write_text(json.dumps(norm, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print()
    print("新增/写入 %d 条, 覆盖旧译文 %d 条, 丢弃 %d 条, 保护未回退 %d 条"
          % (added, overwritten, dropped, protected))
    print("主表 %d 条, 归一化表 %d 条" % (len(main), len(norm)))
    return 0


def cmd_report(args):
    """统计当前覆盖率(对已采集的真实输出)"""
    mod = load_server()
    outdir = pathlib.Path(args.out)
    rows = []
    if outdir.exists():
        for f in sorted(outdir.glob("*.out")):
            for line in mod.strip_ansi(f.read_text(encoding="utf-8", errors="replace")).split("\n"):
                if line.strip():
                    rows.append(line)
    if not rows:
        print("没有采集数据, 先执行: python3 tools/mole_sync.py collect --out %s" % outdir)
        return 1
    miss = [r for r in rows if mod.translate_text(r).strip() == r.strip()
            and re.search(r"[A-Za-z]{3,}", r)]
    print("主表 %d 条, 归一化表 %d 条" % (len(mod._ZH_MAP), len(mod._NORM_MAP)))
    print("总行 %d, 含英文且未译 %d, 覆盖率 %.1f%%"
          % (len(rows), len(miss), (len(rows) - len(miss)) * 100.0 / len(rows)))
    if args.show:
        for line in miss:
            print("   未译: %r" % line[:100])
    return 0


def main():
    ap = argparse.ArgumentParser(description="Mole WebUI 与 Mole CLI 同步工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="核对命令白名单").set_defaults(func=cmd_check)
    p = sub.add_parser("collect", help="采集真实输出并产出待翻译清单")
    p.add_argument("--quick", action="store_true", help="只跑秒级用例")
    p.add_argument("--out", default="/tmp/mole_sync", help="采集输出目录")
    p.set_defaults(func=cmd_collect)
    p = sub.add_parser("merge", help="合并翻译结果")
    p.add_argument("--todo", required=True, help="collect 产出的 todo.json")
    p.add_argument("--translated", required=True, help="你的译文 json")
    p.set_defaults(func=cmd_merge)
    p = sub.add_parser("report", help="统计覆盖率")
    p.add_argument("--out", default="/tmp/mole_sync", help="采集输出目录")
    p.add_argument("--show", action="store_true", help="列出未译行")
    p.set_defaults(func=cmd_report)
    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
