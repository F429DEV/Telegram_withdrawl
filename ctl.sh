#!/usr/bin/env bash
#
# 抽奖机器人的启停脚本
#
#   ./ctl.sh start      启动
#   ./ctl.sh stop       停止
#   ./ctl.sh restart    重启
#   ./ctl.sh status     看状态
#   ./ctl.sh health     健康检查（给 cron / systemd timer 用，healthy 退出 0）
#   ./ctl.sh log        实时跟日志（Ctrl+C 退出，不影响机器人）
#   ./ctl.sh tail 200   看最近 200 行日志
#
# 装了 systemd unit（deploy/install-systemd.sh）之后，start/stop/restart/status
# 会自动转发给 systemctl，用法不变。没装就用自带的 nohup 方式跑。
#
set -uo pipefail
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")"

APP="抽奖机器人"
SERVICE="tg-lottery"
VENV=".venv"
PY="$VENV/bin/python"
PID_FILE="run/bot.pid"
LOG_FILE="logs/bot.log"
BOOT_LOG="logs/boot.log"

mkdir -p run logs

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- 配置读取

# 从 .env 里读一个键，没有就用默认值（不 source .env，避免执行里面的内容）
env_get() {
    local key="$1" default="$2" value=""
    if [ -f .env ]; then
        value=$(grep -E "^[[:space:]]*${key}[[:space:]]*=" .env 2>/dev/null |
                tail -1 | cut -d= -f2- | tr -d '"'"'"' \r' | xargs 2>/dev/null)
    fi
    echo "${value:-$default}"
}

heartbeat_path()     { env_get HEARTBEAT_FILE "run/heartbeat"; }
heartbeat_interval() { env_get HEARTBEAT_SECONDS "60"; }

# ---------------------------------------------------------------- systemd

use_systemd() {
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl cat "${SERVICE}.service" >/dev/null 2>&1
}

# systemd 的实际状态字符串：active / activating / inactive / failed ...
unit_state() {
    systemctl is-active "${SERVICE}.service" 2>/dev/null || true
}

# 服务「正在起或已经起来」都算在跑。
# is-active --quiet 只认 active，activating（含 Restart 间隙的 auto-restart）会返回非零，
# 拿它当健康判断会在刚重启那几秒误报，健康检查 timer 还会顺手把正在启动的服务重启掉。
unit_up() {
    case "$(unit_state)" in
        active|activating|reloading) return 0 ;;
        *) return 1 ;;
    esac
}

# 服务已经起来多少秒了。拿不到返回 -1。
service_age() {
    if use_systemd; then
        local mono up
        mono=$(systemctl show -p ActiveEnterTimestampMonotonic --value "${SERVICE}.service" 2>/dev/null)
        case "$mono" in
            ''|*[!0-9]*) echo -1; return ;;
        esac
        [ "$mono" -gt 0 ] || { echo -1; return; }
        up=$(awk '{print int($1)}' /proc/uptime 2>/dev/null)
        [ -n "$up" ] || { echo -1; return; }
        echo $(( up - mono / 1000000 ))
        return
    fi
    local pid
    if pid=$(running_pid); then
        ps -p "$pid" -o etimes= 2>/dev/null | tr -d ' ' || echo -1
    else
        echo -1
    fi
}

# 心跳过期阈值
heartbeat_limit() {
    local limit
    limit=$(( $(heartbeat_interval) * 3 ))
    [ "$limit" -lt 180 ] && limit=180
    echo "$limit"
}

# ---------------------------------------------------------------- 自带方式

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

# 解释器在 ≠ 依赖在。
# 线上真实踩过：.venv/bin/python 好好的，但 import telegram 失败
# （依赖没装成功，或者系统 Python 升了小版本让 venv 里的 lib/pythonX.Y 对不上），
# 机器人在第一行 import 就崩，systemd 一直拉一直崩。
venv_ok() {
    [ -x "$PY" ] || return 1
    "$PY" -c "import telegram" >/dev/null 2>&1
}

ensure_env() {
    if [ ! -f .env ]; then
        red "没找到 .env。先执行：cp .env.example .env，然后填入 BOT_TOKEN"
        exit 1
    fi
    venv_ok && return 0

    if [ -x "$PY" ]; then
        ylw "虚拟环境里 import 不到 telegram，重建 $VENV ..."
        rm -rf "$VENV"
    else
        ylw "首次运行，正在创建虚拟环境 $VENV ..."
    fi
    python3 -m venv "$VENV" || {
        red "创建虚拟环境失败。Debian/Ubuntu 上先装：sudo apt install -y python3-venv python3-full"
        exit 1
    }
    "$VENV/bin/pip" install --upgrade pip -q
    "$VENV/bin/pip" install -r requirements.txt || { red "依赖安装失败"; exit 1; }
    venv_ok || { red "装完还是 import 不到 telegram，先手查这个"; exit 1; }
    grn "虚拟环境就绪"
}

# ---------------------------------------------------------------- 命令

# systemctl start/restart 返回只代表「命令收到了」。Type=simple 下进程
# 起来就算 active，几秒后崩掉照样是 active 过。所以等一会儿再看一眼。
verify_started() {
    sleep 4
    if unit_up && [ "$(unit_state)" = "active" ]; then
        grn "$APP 运行中"
        systemctl --no-pager --lines=0 status "${SERVICE}.service" | sed -n '2,4p'
        echo "  日志：$LOG_FILE      （./ctl.sh log 实时查看）"
        return 0
    fi
    red "$APP 没起来 —— 当前状态：$(unit_state)"
    echo ""
    echo "最近的日志："
    journalctl -u "${SERVICE}.service" -n 15 --no-pager 2>/dev/null | sed 's/^/    /'
    return 1
}

start() {
    if use_systemd; then
        # 交给 systemd 之前先自己确认环境没问题 ——
        # 否则 systemd 只会一遍遍拉起一个必崩的进程
        ensure_env
        rm -f "$(heartbeat_path)"
        systemctl start "${SERVICE}.service" || { red "systemctl start 失败"; return 1; }
        verify_started
        return
    fi

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
    rm -f "$(heartbeat_path)"
    PYTHONUNBUFFERED=1 nohup "$PY" bot.py >> "$BOOT_LOG" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"

    # 等几秒确认它真的活下来了（token 错、依赖缺、端口冲突都会在这几秒内暴露）
    sleep 3
    if kill -0 "$new_pid" 2>/dev/null; then
        grn "$APP 已启动，PID $new_pid"
        echo "  日志：$LOG_FILE      （./ctl.sh log 实时查看）"
        ylw "  提醒：这种方式起的进程，服务器重启后不会自己回来。"
        ylw "  正式跑建议装 systemd：sudo ./deploy/install-systemd.sh"
    else
        rm -f "$PID_FILE"
        red "$APP 启动后立刻退出了。最后几行输出："
        tail -n 20 "$BOOT_LOG" | sed 's/^/    /'
        return 1
    fi
}

stop() {
    if use_systemd; then
        systemctl stop "${SERVICE}.service" && grn "$APP 已停止（systemd）"
        # 心跳留着会让下次启动的前 3 分钟「看起来健康」，即使新进程根本没起来
        rm -f "$(heartbeat_path)"
        return
    fi

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

heartbeat_report() {
    local hb interval age limit
    hb=$(heartbeat_path)
    interval=$(heartbeat_interval)
    limit=$(( interval * 3 ))
    [ "$limit" -lt 180 ] && limit=180

    if [ ! -f "$hb" ]; then
        echo "  心跳     : 还没有（刚启动的话等 $interval 秒左右）"
        return 1
    fi
    age=$(( $(date +%s) - $(cat "$hb" 2>/dev/null || echo 0) ))
    if [ "$age" -le "$limit" ]; then
        echo "  心跳     : ${age} 秒前（阈值 ${limit} 秒）"
        return 0
    fi
    echo "  心跳     : ${age} 秒前 —— 超过 ${limit} 秒，API 可能已经调不通"
    return 1
}

status() {
    if use_systemd; then
        systemctl --no-pager --lines=0 status "${SERVICE}.service" | head -6
    else
        local pid
        if pid=$(running_pid); then
            grn "$APP 运行中"
            echo "  PID      : $pid"
            echo "  已运行   : $(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')"
            echo "  内存     : $(ps -p "$pid" -o rss= 2>/dev/null | awk '{printf "%.1f MB", $1/1024}')"
        else
            red "$APP 未运行"
        fi
    fi
    heartbeat_report || true
    if venv_ok; then
        echo "  依赖     : 正常"
    else
        red "  依赖     : import telegram 失败 —— 跑 ./ctl.sh start 会自动重建 venv"
    fi
    if [ -f "$LOG_FILE" ]; then
        echo "  日志文件 : $LOG_FILE（$(du -h "$LOG_FILE" | cut -f1)）"
        echo ""
        echo "  最近 5 行："
        tail -n 5 "$LOG_FILE" | sed 's/^/    /'
    fi
}

# 健康 = 服务在跑 且 心跳新鲜。退出 0 健康，1 不健康。
# 给 systemd timer / cron 用：./ctl.sh health --quiet || systemctl restart tg-lottery
#
# 刚启动的那段时间心跳还没写出来，不能算不健康 —— 否则健康检查会把正在启动的
# 服务一直重启，自己造出一个循环。所以心跳不新鲜时还要看服务起来多久了。
health() {
    local quiet="${1:-}" ok=0 reason=""

    if use_systemd; then
        unit_up || { ok=1; reason="服务状态是 $(unit_state)"; }
    else
        running_pid >/dev/null || { ok=1; reason="进程不在运行"; }
    fi

    if [ "$ok" -eq 0 ]; then
        local hb limit age stale=0 svc_age
        hb=$(heartbeat_path)
        limit=$(heartbeat_limit)
        if [ -z "$hb" ]; then
            stale=0          # 没开心跳，只能看服务状态
        elif [ ! -f "$hb" ]; then
            stale=1; reason="没有心跳文件"
        else
            age=$(( $(date +%s) - $(cat "$hb" 2>/dev/null || echo 0) ))
            if [ "$age" -gt "$limit" ]; then
                stale=1; reason="心跳停在 ${age} 秒前（阈值 ${limit} 秒）"
            fi
        fi

        if [ "$stale" -eq 1 ]; then
            svc_age=$(service_age)
            if [ "$svc_age" -ge 0 ] && [ "$svc_age" -lt "$limit" ]; then
                reason=""     # 还在启动宽限期内，先别判它死
            else
                ok=1
            fi
        fi
    fi

    if [ "$ok" -eq 0 ]; then
        [ "$quiet" = "--quiet" ] || grn "healthy"
    else
        [ "$quiet" = "--quiet" ] || red "unhealthy: $reason"
        logger -t "$SERVICE" "unhealthy: $reason" 2>/dev/null || true
    fi
    return "$ok"
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
    restart)
        if use_systemd; then
            ensure_env
            rm -f "$(heartbeat_path)"
            systemctl restart "${SERVICE}.service" || { red "systemctl restart 失败"; exit 1; }
            verify_started
        else
            stop; sleep 1; start
        fi
        ;;
    status)  status ;;
    health)  health "${2:-}" ;;
    log|logs|follow) follow_log ;;
    tail)    tail_log "${2:-100}" ;;
    *)
        echo "用法: $0 {start|stop|restart|status|health|log|tail [行数]}"
        exit 1
        ;;
esac
