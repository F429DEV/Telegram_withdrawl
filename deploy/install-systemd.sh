#!/usr/bin/env bash
#
# 把机器人装成 systemd 服务：开机自启、崩了自动拉起、掉线自动检测。
#
#   sudo ./deploy/install-systemd.sh
#
# 装两套 unit：
#   tg-lottery.service        机器人本体，Restart=always，永不永久放弃
#   tg-lottery-health.timer   每 2 分钟跑一次健康检查，不健康就重启本体
#
# 为什么要第二套：Restart=always 只管进程死掉。进程活着但 API 已经调不通
# （网络被掐、token 被撤）systemd 看不出来，得靠心跳文件判断。
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
SERVICE="tg-lottery"
PY="$PROJECT_DIR/.venv/bin/python"
UNIT_DIR="/etc/systemd/system"

[ "$(id -u)" -eq 0 ] || { echo "要用 root 跑：sudo $0"; exit 1; }
[ -f "$PROJECT_DIR/.env" ] || { echo "没找到 $PROJECT_DIR/.env，先配好再装"; exit 1; }

# 光看 .venv/bin/python 在不在是不够的：
# 解释器在、但 import telegram 失败的情况真实发生过（依赖没装成功，
# 或者系统 Python 升了小版本，venv 里的 lib/pythonX.Y 对不上了）。
# 那种状态下机器人会在第一行 import 就崩，systemd 一直拉一直崩。
if ! "$PY" -c "import telegram" >/dev/null 2>&1; then
    if [ -x "$PY" ]; then
        echo "虚拟环境里缺依赖（import telegram 失败），重建 .venv ..."
        rm -rf "$PROJECT_DIR/.venv"
    else
        echo "没找到 $PY，正在创建虚拟环境 ..."
    fi
    python3 -m venv "$PROJECT_DIR/.venv"
    "$PROJECT_DIR/.venv/bin/pip" install --upgrade pip -q
    "$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt" \
        || { echo "依赖安装失败，先解决这个再装服务"; exit 1; }
    "$PY" -c "import telegram" >/dev/null 2>&1 \
        || { echo "装完还是 import 不到 telegram，别装服务了，先手查"; exit 1; }
    echo "依赖就绪"
fi

RUN_USER="${SUDO_USER:-root}"
echo "项目目录 : $PROJECT_DIR"
echo "运行用户 : $RUN_USER"
echo ""

cat > "$UNIT_DIR/${SERVICE}.service" <<UNIT
[Unit]
Description=Telegram 抽奖机器人
After=network-online.target
Wants=network-online.target
# 永不永久放弃。
#
# StartLimitIntervalSec 是 [Unit] 的键 —— 放在 [Service] 里 systemd 会直接
# 忽略并打一行 "Unknown key"，设置形同没写。
#
# 之所以要关掉这个限制：原来「10 分钟内失败 10 次就进 failed」的后果更糟，
# 开机时网络没就绪、机房抖一阵，连续失败十来次之后 unit 就再也不会自己回来，
# 表面上 start 说在跑，实际躺了好几天没人发现。
# 宁可一直重试刷日志，也不要静默长期掉线。
StartLimitIntervalSec=0

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PY $PROJECT_DIR/bot.py
Restart=always
RestartSec=10
# 配置错（token 无效）由 bot.py 用退出码 78 明确表示，这种情况不要再拉起来
RestartPreventExitStatus=78
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT

cat > "$UNIT_DIR/${SERVICE}-health.service" <<UNIT
[Unit]
Description=抽奖机器人健康检查（心跳停了就重启本体）
After=${SERVICE}.service

[Service]
Type=oneshot
WorkingDirectory=$PROJECT_DIR
ExecStart=/bin/bash -c '$PROJECT_DIR/ctl.sh health --quiet || { logger -t ${SERVICE} "health check failed, restarting"; systemctl restart ${SERVICE}.service; }'
UNIT

cat > "$UNIT_DIR/${SERVICE}-health.timer" <<UNIT
[Unit]
Description=每 2 分钟检查一次抽奖机器人

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
AccuracySec=15s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now "${SERVICE}.service"
systemctl enable --now "${SERVICE}-health.timer"

echo ""
echo "装好了。"
echo ""
systemctl --no-pager --lines=0 status "${SERVICE}.service" | head -5
echo ""
echo "常用命令："
echo "  ./ctl.sh status            看状态和心跳"
echo "  ./ctl.sh log               跟日志"
echo "  ./ctl.sh restart           重启（已转发给 systemctl）"
echo "  systemctl list-timers ${SERVICE}-health.timer"
echo "  journalctl -u ${SERVICE} -f"
