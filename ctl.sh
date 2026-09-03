#!/usr/bin/env bash
#
# 抽奖机器人的启停脚本（后台运行，日志写文件）
#
#   ./ctl.sh start      后台启动
#   ./ctl.sh stop       停止
#   ./ctl.sh restart    重启
#   ./ctl.sh status     看状态
#   ./ctl.sh log        实时跟日志（Ctrl+C 退出，不影响机器人）
#   ./ctl.sh tail 200   看最近 200 行日志
#
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"

APP="抽奖机器人"
VENV=".venv"
PY="$VENV/bin/python"
PID_FILE="run/bot.pid"
# 程序自己写的滚动日志（logs/bot.log，可在 .env 里改 LOG_FILE）
LOG_FILE="logs/bot.log"
# 启动阶段的输出兜底：Python 日志系统起来之前的崩溃信息落在这里
BOOT_LOG="logs/boot.log"

mkdir -p run logs

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- 工具

running_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null)
    [ -n "$pid" ] || return 1
    # 确认这个 pid 真的是我们的 bot，而不是 pid 被系统回收后给了别的进程
    if kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o args= 2>/dev/null | grep -q "bot.py"; then
        echo "$pid"
        return 0
    fi
    rm -f "$PID_FILE"
    return 1
}

ensure_env() {
    if [ ! -f .env ]; then
        red "没找到 .env。先执行：cp .env.example .env，然后填入 BOT_TOKEN"
        exit 1
    fi
    if [ ! -x "$PY" ]; then
        ylw "首次运行，正在创建虚拟环境 $VENV ..."
        python3 -m venv "$VENV" || {
            red "创建虚拟环境失败。Debian/Ubuntu 上先装：sudo apt install -y python3-venv python3-full"
            exit 1
        }
        "$VENV/bin/pip" install --upgrade pip -q
        "$VENV/bin/pip" install -r requirements.txt || { red "依赖安装失败"; exit 1; }
        grn "虚拟环境就绪"
    fi
}

# ---------------------------------------------------------------- 命令

start() {
    local pid
    if pid=$(running_pid); then
        ylw "$APP 已经在跑了（PID $pid），不用重复启动。"
        return 0
    fi
    ensure_env

    # 同一个 token 只能有一个 polling 进程，否则 Telegram 会报 Conflict
    if pgrep -f "$PWD/bot.py" >/dev/null 2>&1 || pgrep -f "$PY bot.py" >/dev/null 2>&1; then
        red "检测到已有 bot.py 进程在跑（但没有 PID 文件）。先 ./ctl.sh stop 或手动 kill 掉再启动。"
        pgrep -af "bot.py" | sed 's/^/    /'
        return 1
    fi

    echo "正在启动 $APP ..."
    PYTHONUNBUFFERED=1 nohup "$PY" bot.py >> "$BOOT_LOG" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"

    # 等几秒确认它真的活下来了（token 错、依赖缺、端口冲突都会在这几秒内暴露）
    sleep 3
    if kill -0 "$new_pid" 2>/dev/null; then
        grn "$APP 已启动，PID $new_pid"
        echo "  日志：$LOG_FILE      （./ctl.sh log 实时查看）"
    else
        rm -f "$PID_FILE"
        red "$APP 启动后立刻退出了。最后几行输出："
        tail -n 20 "$BOOT_LOG" | sed 's/^/    /'
        return 1
    fi
}

stop() {
    local pid
    if ! pid=$(running_pid); then
        ylw "$APP 没在运行。"
        return 0
    fi
    echo "正在停止 $APP（PID $pid）..."
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 15); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        ylw "15 秒没退出，强制结束。"
        kill -9 "$pid" 2>/dev/null
        sleep 1
    fi
    rm -f "$PID_FILE"
    grn "$APP 已停止。"
}

status() {
    local pid
    if pid=$(running_pid); then
        grn "$APP 运行中"
        echo "  PID      : $pid"
        echo "  已运行   : $(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')"
        echo "  内存     : $(ps -p "$pid" -o rss= 2>/dev/null | awk '{printf "%.1f MB", $1/1024}')"
    else
        red "$APP 未运行"
    fi
    if [ -f "$LOG_FILE" ]; then
        echo "  日志文件 : $LOG_FILE（$(du -h "$LOG_FILE" | cut -f1)）"
        echo ""
        echo "  最近 5 行："
        tail -n 5 "$LOG_FILE" | sed 's/^/    /'
    fi
}

follow_log() {
    [ -f "$LOG_FILE" ] || { ylw "还没有日志文件 $LOG_FILE"; exit 0; }
    echo "跟随 $LOG_FILE（Ctrl+C 退出，不会影响机器人）"
    tail -n 30 -F "$LOG_FILE"
}

tail_log() {
    local n="${1:-100}"
    [ -f "$LOG_FILE" ] || { ylw "还没有日志文件 $LOG_FILE"; exit 0; }
    tail -n "$n" "$LOG_FILE"
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    log|logs|follow) follow_log ;;
    tail)    tail_log "${2:-100}" ;;
    *)
        echo "用法: $0 {start|stop|restart|status|log|tail [行数]}"
        exit 1
        ;;
esac
