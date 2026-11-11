#!/bin/bash
# Mole WebUI 启动脚本
# 只监听本机回环地址 127.0.0.1:7788，按 Ctrl+C 停止。

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if ! command -v python3 > /dev/null 2>&1; then
  printf '%s\n' '错误：未找到 python3，无法启动 Mole WebUI。' >&2
  exit 1
fi

if [[ ! -f "$DIR/server.py" ]]; then
  printf '%s\n' '错误：缺少 server.py，请在 Mole-webui 目录下运行本脚本。' >&2
  exit 1
fi

# 启动横幅由 server.py 统一打印（含 Mole 版本与安装目录），这里不再重复输出
exec python3 "$DIR/server.py"
