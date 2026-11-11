#!/usr/bin/env python3
"""
Mole 输出汉化表刷新工具

Mole 官方版是英文的, WebUI 靠 i18n/mole_zh.json 把它的输出翻成中文。
Mole 每次更新会带进新的提示语, 那些新文案不在表里就会显示英文。
这个工具负责: 抽取当前 Mole 的全部用户可见文案 -> 找出表里没有的 ->
批量翻译新增部分 -> 合并回 i18n/mole_zh.json。

用法:
  python3 tools/refresh_i18n.py --report      只报告覆盖率, 不改任何文件
  python3 tools/refresh_i18n.py --extract     只抽取, 输出待翻译清单到 /tmp
  python3 tools/refresh_i18n.py --translate    抽取 + 翻译 + 合并 (默认动作)
  python3 tools/refresh_i18n.py --prune       顺带删掉表里已失效的旧条目

依赖:
  - 本机装的 Mole (默认 ~/.config/mole, 可用 --mole-dir 指定)
  - llm_call.py (LLM 翻译), 路径可用 --llm-script 指定
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

DEFAULT_MOLE_DIR = os.path.expanduser("~/.config/mole")
DEFAULT_LLM = os.path.expanduser(
    "~/.minimax/.builtin-skills/llm-call/scripts/llm_call.py")
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
I18N_FILE = BASE_DIR / "i18n" / "mole_zh.json"
MODEL = "minimax/MiniMax-M3.1-Flash"
BATCH = 60

ANSI_VAR = re.compile(r"\$\{[A-Z_0-9]+\}")
LITERAL = re.compile(r'"([^"\n]{2,300})"|\'([^\'\n]{2,300})\'')
OUTPUT_FN = re.compile(
    r"\b(echo|printf|log_info|log_error|log_warning|log_success|note|"
    r"print_|review_row|die)\b")
CODEISH = re.compile(
    r"(complete -f|function |\{gsub|^s/.*/$|^set |^export |^local |^if |^for |"
    r"^while |^case |^return |^declare |^readonly |^typeset )")
PLACEHOLDER = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z_0-9]*|"
                         r"%(?:\d+\$)?[sdfx]|%\([^)]+\)[sdfx]")
MARK = re.compile("[%s](\\d+)[%s]" % (chr(0xE000), chr(0xE001)))


def protect(text):
    """把变量占位符换成私用区标记, 避免翻译时被改坏"""
    marks = []

    def repl(m):
        marks.append(m.group(0))
        return "\uE000%d\uE001" % (len(marks) - 1)

    return PLACEHOLDER.sub(repl, text), marks


def restore(text, marks):
    """把私用区标记还原回原始占位符"""
    def repl(m):
        idx = int(m.group(1))
        return marks[idx] if 0 <= idx < len(marks) else m.group(0)

    return MARK.sub(repl, text)


def extract_strings(mole_dir):
    """抽取 Mole 脚本里面向用户的英文文案"""
    root = pathlib.Path(mole_dir)
    files = sorted(list(root.glob("lib/**/*.sh")) + list(root.glob("bin/*.sh")))
    result = {}
    for path in files:
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in src.splitlines():
            if not OUTPUT_FN.search(line) or CODEISH.search(line.strip()):
                continue
            for m in LITERAL.finditer(line):
                s = m.group(1) if m.group(1) is not None else m.group(2)
                if s is None:
                    continue
                s = ANSI_VAR.sub("", s).strip()
                if len(s) < 4 or len(s) > 200:
                    continue
                if not re.search(r"[A-Za-z]{3,}", s):
                    continue
                if re.fullmatch(r"[\$%{}()\[\]:;/\\\-_=+*?!&|.<>\s0-9]+", s):
                    continue
                if s.startswith("/") or s.startswith("~/"):
                    continue
                if re.fullmatch(r"[A-Za-z0-9_./\-]+", s) and " " not in s:
                    continue
                if "$(" in s or s.startswith("$("):
                    continue
                if s.endswith("\\\\") or "\\\\" in s:
                    continue
                protected, marks = protect(s)
                result.setdefault(s, {"protected": protected, "marks": marks})
    return result


def load_table():
    try:
        data = json.loads(I18N_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def call_llm(items, llm_script):
    """批量翻译, 返回 {英文: 中文}"""
    if not items:
        return {}
    prompt = (
        "把下面 JSON 数组里的每条英文终端文案翻译成简体中文。"
        "只输出一个 JSON 数组, 长度与输入一致, 每项是对应的中文译文, 不要任何解释。"
        "命令、选项名(以 -- 开头)、路径、环境变量、应用名保持原样不译。"
        "形如 U+E000 数字 U+E001 的占位符标记必须原样保留, 一个都不能丢或改。\n\n"
        + json.dumps(items, ensure_ascii=False)
    )
    cmd = [sys.executable, llm_script, "--model", MODEL, "--prompt", prompt]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as exc:
        print("  LLM 调用失败: %s" % exc)
        return {}
    raw = (proc.stdout or "").strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        print("  LLM 返回里找不到 JSON 数组")
        return {}
    try:
        arr = json.loads(raw[start:end + 1])
    except Exception as exc:
        print("  LLM 返回解析失败: %s" % exc)
        return {}
    out = {}
    for src, zh in zip(items, arr):
        if isinstance(zh, str) and zh.strip():
            out[src] = zh.strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mole-dir", default=DEFAULT_MOLE_DIR)
    ap.add_argument("--llm-script", default=DEFAULT_LLM)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--translate", action="store_true")
    ap.add_argument("--prune", action="store_true")
    args = ap.parse_args()

    if not (args.report or args.extract or args.translate):
        args.translate = True

    strings = extract_strings(args.mole_dir)
    table = load_table()
    covered = [k for k in strings if k in table]
    missing = [k for k in strings if k not in table]
    print("Mole 文案总数: %d | 已覆盖: %d | 未覆盖: %d (%.1f%%)"
          % (len(strings), len(covered), len(missing),
             len(covered) * 100 / len(strings) if strings else 0))

    if args.report:
        for k in missing[:20]:
            print("   未覆盖: %s" % k[:78])
        return 0

    if args.extract:
        todo = {k: strings[k] for k in missing}
        out = pathlib.Path("/tmp/mole_todo_refresh.json")
        out.write_text(json.dumps(todo, ensure_ascii=False, indent=1), encoding="utf-8")
        print("待翻译清单: %s (%d 条)" % (out, len(todo)))
        return 0

    if not missing:
        print("没有新增文案, 无需更新。")
        return 0

    print("开始翻译 %d 条新增文案..." % len(missing))
    translated = {}
    t0 = time.time()
    for i in range(0, len(missing), BATCH):
        chunk = missing[i:i + BATCH]
        protected = [strings[k]["protected"] for k in chunk]
        got = call_llm(protected, args.llm_script)
        ok = 0
        for orig, prot in zip(chunk, protected):
            zh = got.get(prot)
            if not zh:
                continue
            want = MARK.findall(prot)
            have = MARK.findall(zh)
            if want != have:
                print("  占位符不一致, 丢弃: %s" % orig[:60])
                continue
            translated[orig] = zh
            ok += 1
        print("  批次 %d/%d  完成 %d 条 (累计 %d, 用时 %.0fs)"
              % (i // BATCH + 1, (len(missing) + BATCH - 1) // BATCH, ok,
                 len(translated), time.time() - t0))
        time.sleep(0.3)

    if args.prune:
        valid = set(strings)
        removed = [k for k in table if k not in valid]
        for k in removed:
            table.pop(k, None)
        print("清理失效条目: %d" % len(removed))

    table.update(translated)
    if I18N_FILE.exists():
        shutil.copy2(str(I18N_FILE), str(I18N_FILE) + ".bak")
    I18N_FILE.parent.mkdir(parents=True, exist_ok=True)
    I18N_FILE.write_text(json.dumps(table, ensure_ascii=False, indent=1, sort_keys=True),
                         encoding="utf-8")
    print("已更新 %s : 共 %d 条 (本次新增 %d)" % (I18N_FILE, len(table), len(translated)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
