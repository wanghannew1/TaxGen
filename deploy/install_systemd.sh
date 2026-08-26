#!/usr/bin/env bash
# TaxGen systemd 服务安装脚本（需要 root 权限执行：sudo bash deploy/install_systemd.sh）
set -euo pipefail

SERVICE_NAME="taxgen"
UNIT_SRC="$(cd "$(dirname "$0")" && pwd)/taxgen.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
APP_DIR="/home/ubuntu/github/TaxGen"

if [[ $EUID -ne 0 ]]; then
    echo "错误：请使用 root 权限运行：sudo bash deploy/install_systemd.sh" >&2
    exit 1
fi

echo "==> 校验项目路径与虚拟环境"
[[ -d "$APP_DIR" ]] || { echo "错误：项目目录不存在: $APP_DIR" >&2; exit 1; }
[[ -x "$APP_DIR/.venv/bin/python" ]] || { echo "错误：虚拟环境不存在，请先按 README 用 uv 安装依赖" >&2; exit 1; }
[[ -f "$APP_DIR/.env" ]] || { echo "错误：缺少 .env，请先 cp .env.example .env 并填入数据库密码" >&2; exit 1; }

echo "==> 安装服务单元文件: $UNIT_DST"
install -m 644 "$UNIT_SRC" "$UNIT_DST"

echo "==> 重新加载 systemd 配置"
systemctl daemon-reload

echo "==> 设置开机自启"
systemctl enable "$SERVICE_NAME"

echo "==> 启动服务"
systemctl restart "$SERVICE_NAME"

echo "==> 等待服务就绪（最多 30 秒）"
for i in $(seq 1 30); do
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        break
    fi
    sleep 1
done

systemctl status "$SERVICE_NAME" --no-pager || true

echo ""
echo "部署完成。常用命令："
echo "  sudo systemctl status taxgen      # 查看状态"
echo "  sudo systemctl restart taxgen     # 重启"
echo "  sudo systemctl stop taxgen        # 停止"
echo "  sudo journalctl -u taxgen -f      # 查看实时日志"
echo "  浏览器访问 http://<服务器IP>:5000"