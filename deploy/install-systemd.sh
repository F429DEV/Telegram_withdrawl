#!/usr/bin/env bash
#
# 把机器人装成 systemd 服务：开机自启、崩了自动拉起、掉线自动检测。
#
#   sudo ./deploy/install-systemd.sh
#
# 装两套 unit：
#   tg-lottery.service        机器人本体，Restart=always
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

if [ ! -x "$PY" ]; then
    echo "没找到 $PY，正在创建虚拟环境 ..."
    python3 -m venv "$PROJECT_DIR/.venv"
    "$PROJECT_DIR/.venv/bin/pip" install --upgrade pip -q
    "$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
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

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PY $PROJECT_DIR/bot.py
Restart=always
RestartSec=5
# 反复起不来就别无限刷：10 分钟内超过 10 次则停下，等人来看
StartLimitIntervalSec=600
StartLimitBurst=10
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
