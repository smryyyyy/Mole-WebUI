#!/bin/bash
# =============================================================================
#  Mole WebUI 服务管理器
# =============================================================================
#  双击 = 直接启动（前台运行，Ctrl+C 停止）
#  命令行: ./Mole-WebUI.command [start|stop|restart|status|menu]
# =============================================================================

# 项目目录: 跟着脚本自己走, 整个文件夹搬到哪儿都能用
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=7788
URL="http://127.0.0.1:7788"

# 打开浏览器的命令, 可用环境变量覆盖(便于沙箱自测, 不真弹浏览器)
OPEN_CMD="${OPEN_CMD:-open}"

# ---- 颜色定义 ----------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ---- 工具函数 ----------------------------------------------------------------
print_line() {
    local char="${1:-─}"
    local color="${2:-$CYAN}"
    local width
    width=$(tput cols 2>/dev/null || echo 80)
    printf "$color"
    printf -- "$char%.0s" $(seq 1 "$width")
    printf "$NC\n"
}

# 端口上有没有人在监听
is_running() {
    nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1
}

# 占用端口的进程 PID
service_pid() {
    lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1
}

get_status() {
    if is_running; then
        printf "${GREEN}● 运行中${NC}"
    else
        printf "${RED}○ 已停止${NC}"
    fi
}

show_status() {
    print_line "═" "$BLUE"
    printf "  ${BOLD}状态:  Mole WebUI ${BOLD}$(get_status)${NC}\n"
    if is_running; then
        printf "  ${BOLD}地址:  ${NC}${URL}\n"
    fi
    print_line "═" "$BLUE"
}

# ---- 启动 --------------------------------------------------------------------
# ⚠️ 必须**前台**运行: Mole 提权时要在控制终端上弹 Touch ID / 密码框,
#    后台化就丢了这个终端, 提权会失败。所以这里用 exec 占住当前窗口。
start_webui() {
    if is_running; then
        printf "  ${YELLOW}⚠  Mole WebUI 已经在运行${NC}\n"
        printf "  地址: ${URL}\n"
        return 0
    fi
    if [ ! -f "$DIR/run.sh" ]; then
        printf "  ${RED}✗ 找不到启动脚本: %s/run.sh${NC}\n" "$DIR"
        return 1
    fi
    printf "  ${YELLOW}▶ 正在启动 Mole WebUI ...${NC}\n"
    printf "  ${BOLD}按 Ctrl+C 停止服务${NC}\n"
    printf "\n"
    # 用默认浏览器打开地址。
    # 下面 exec 会把当前进程整个换成服务进程, 之后的代码都不再执行,
    # 所以 open 必须提前安排到后台去做。
    # 等端口真的起来了再开, 免得浏览器先打开一个连不上的页面。
    (
        i=0
        while [ "$i" -lt 25 ]; do
            if nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; then
                "$OPEN_CMD" "$URL"
                break
            fi
            sleep 1
            i=$((i+1))
        done
    ) &
    cd "$DIR" || exit 1
    exec bash "$DIR/run.sh"
}

# ---- 停止 --------------------------------------------------------------------
stop_webui() {
    local pid
    pid=$(service_pid)
    if [ -z "$pid" ]; then
        printf "  ${YELLOW}⚠  Mole WebUI 本来就没在运行${NC}\n"
        return 0
    fi
    printf "  ${YELLOW}▶ 正在停止 Mole WebUI (PID %s) ...${NC}\n" "$pid"
    kill "$pid" 2>/dev/null
    local i=0
    while [ "$i" -lt 8 ] && is_running; do
        sleep 1
        i=$((i+1))
    done
    if is_running; then
        kill -9 "$pid" 2>/dev/null
        sleep 1
    fi
    if is_running; then
        printf "  ${RED}✗ 停止失败${NC}\n"
        return 1
    fi
    printf "  ${GREEN}✓ 已停止${NC}\n"
    return 0
}

open_webui() {
    if ! is_running; then
        printf "  ${YELLOW}⚠  服务没在运行，先启动它${NC}\n"
        return 1
    fi
    "$OPEN_CMD" "$URL"
    printf "  ${GREEN}✓ 已在浏览器打开${NC}\n"
    return 0
}

# ---- 主菜单 ------------------------------------------------------------------
show_menu() {
    clear
    printf "${CYAN}"
    cat << "EOF"

    ╔══════════════════════════════════════════════════════╗
    ║                                                      ║
    ║              Mole WebUI 服 务 管 理 器               ║
    ║                                                      ║
    ╚══════════════════════════════════════════════════════╝

EOF
    show_status
    echo ""
    printf "  ${BOLD}${YELLOW}请选择操作:${NC}\n"
    echo ""
    printf "    ${GREEN}${BOLD} 1.${NC}  启动 Mole WebUI（前台，Ctrl+C 停止）\n"
    printf "    ${BLUE}${BOLD} 2.${NC}  打开 Web UI\n"
    printf "    ${RED}${BOLD} 3.${NC}  停止 Mole WebUI\n"
    printf "    ${YELLOW}${BOLD} 4.${NC}  重启 Mole WebUI\n"
    printf "    ${BOLD}${BOLD} 5.${NC}  退出\n"
    echo ""
    print_line "─" "$BLUE"
    echo ""
}

menu_loop() {
    while true; do
        show_menu
        printf "  ${BOLD}请输入 [1-5]:${NC} "
        read -r choice
        echo ""
        case "$choice" in
            1)
                printf "${GREEN}━━━ 启动 Mole WebUI ━━━${NC}\n"
                start_webui
                ;;
            2)
                printf "${BLUE}━━━ 打开 Web UI ━━━${NC}\n"
                open_webui
                printf "\n"
                read -r -p "  按 Enter 键返回菜单..." _
                ;;
            3)
                printf "${RED}━━━ 停止 Mole WebUI ━━━${NC}\n"
                stop_webui
                printf "\n"
                read -r -p "  按 Enter 键返回菜单..." _
                ;;
            4)
                printf "${YELLOW}━━━ 重启 Mole WebUI ━━━${NC}\n"
                stop_webui
                printf "\n"
                start_webui
                ;;
            5)
                printf "\n${CYAN}  再见! 👋${NC}\n\n"
                exit 0
                ;;
            *)
                printf "${RED}  无效选项, 请输入 1-5${NC}\n"
                sleep 1
                ;;
        esac
    done
}

# ---- 程序入口 ----------------------------------------------------------------
case "${1:-doubleclick}" in
    start)
        start_webui
        ;;
    stop)
        stop_webui
        ;;
    restart)
        stop_webui
        printf "\n"
        start_webui
        ;;
    status)
        show_status
        ;;
    menu)
        menu_loop
        ;;
    doubleclick)
        # 双击: 没跑就直接前台启动; 已经跑着就进菜单
        if is_running; then
            menu_loop
        else
            start_webui
        fi
        ;;
    *)
        printf "用法: %s [start|stop|restart|status|menu]\n" "$0"
        ;;
esac
