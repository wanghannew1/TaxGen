#!/usr/bin/env bash
# TaxGen systemd 服务安装脚本（root 权限执行：sudo bash deploy/install_systemd.sh）
# 路径自动检测：以脚本所在 deploy/ 的上一级目录作为项目根，无需手填。
set -euo pipefail

SERVICE_NAME="taxgen"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_SRC="$SCRIPT_DIR/taxgen.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
VENV_PYTHON="$APP_DIR/.venv/bin/python"
# Oracle Instant Client 路径，默认标准位置，可用环境变量覆盖
ORACLE_CLIENT_LIB_DIR="${ORACLE_CLIENT_LIB_DIR:-/opt/oracle/instantclient_23_4}"
# 运行用户：优先用调用 sudo 的用户，其次取项目目录属主
RUN_USER="${SUDO_USER:-$(stat -c '%U' "$APP_DIR")}"

if [[ $EUID -ne 0 ]]; then
    echo "错误：请使用 root 权限运行：sudo bash deploy/install_systemd.sh" >&2
    exit 1
fi

echo "==> 校验项目路径与虚拟环境"
[[ -d "$APP_DIR" ]] || { echo "错误：项目目录不存在: $APP_DIR" >&2; exit 1; }
[[ -x "$VENV_PYTHON" ]] || { echo "错误：虚拟环境不存在或缺少 python: $VENV_PYTHON" >&2
                             echo "       请先按 README 用 uv 安装依赖" >&2; exit 1; }
[[ -f "$APP_DIR/.env" ]] || { echo "错误：缺少 .env，请先 cp .env.example .env 并填入数据库密码" >&2; exit 1; }
if [[ ! -d "$ORACLE_CLIENT_LIB_DIR" ]]; then
    echo "警告：Oracle Instant Client 目录不存在: $ORACLE_CLIENT_LIB_DIR"
    echo "       若装在别处，用 ORACLE_CLIENT_LIB_DIR=/your/path sudo -E bash deploy/install_systemd.sh 覆盖"
fi

echo "==> 检测到的配置"
echo "    项目目录      : $APP_DIR"
echo "    venv python    : $VENV_PYTHON"
echo "    运行用户       : $RUN_USER"
echo "    Oracle Client  : $ORACLE_CLIENT_LIB_DIR"

echo "==> 生成服务单元文件: $UNIT_DST"
sed -e "s|@APP_DIR@|$APP_DIR|g" \
    -e "s|@VENV_PYTHON@|$VENV_PYTHON|g" \
    -e "s|@RUN_USER@|$RUN_USER|g" \
    -e "s|@ORACLE_CLIENT_LIB_DIR@|$ORACLE_CLIENT_LIB_DIR|g" \
    "$UNIT_SRC" > "$UNIT_DST"
chmod 644 "$UNIT_DST"

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
