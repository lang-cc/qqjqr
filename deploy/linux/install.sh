#!/usr/bin/env bash
# AstrBot 云服务器一键准备脚本
# 用法: sudo ./install.sh
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/astrbot}
ASTRBOT_VERSION=${ASTRBOT_VERSION:-4.29.3}
RUN_USER=${RUN_USER:-astrbot}

echo "==> 安装系统依赖"
apt update
apt install -y curl wget git unzip vim htop ffmpeg python3 python3-venv python3-pip

echo "==> 时区"
timedatectl set-timezone Asia/Shanghai || true

echo "==> 创建用户 $RUN_USER"
id "$RUN_USER" &>/dev/null || useradd -m -s /bin/bash "$RUN_USER"

echo "==> 创建目录 $APP_DIR"
mkdir -p "$APP_DIR"/{tools,logs}
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

echo "==> 安装 AstrBot $ASTRBOT_VERSION"
su - "$RUN_USER" -c "cd $APP_DIR && \
  python3 -m venv .venv && \
  .venv/bin/pip install -U pip && \
  .venv/bin/pip install astrbot==$ASTRBOT_VERSION"

chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

cat <<EOT

✅ AstrBot 已装到 $APP_DIR

接下来手动做三件事：
 1) 把 tools/gw_proxy.py 放进 $APP_DIR/tools/（如果用排队代理）
 2) 装 NapCat（docker compose，见 deploy/linux/napcat-docker-compose.yml）
 3) 装 systemd 服务：
      cp deploy/linux/astrbot.service /etc/systemd/system/
      cp deploy/linux/gwproxy.service /etc/systemd/system/
      systemctl daemon-reload && systemctl enable --now gwproxy astrbot
EOT
