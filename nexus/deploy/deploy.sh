#!/usr/bin/env bash
# 公网演示部署脚本（Ubuntu/Debian + systemd + Nginx，不用 Docker）
#
#   sudo bash deploy/deploy.sh
#
# 它做的是"把一台干净的机器变成能跑的演示环境"，并且在每一步都倾向于
# 拒绝而不是放行：证书申请不到就不启动，账号没建好也不启动。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/nexus}"
APP_USER="${APP_USER:-nexus}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
PUBLIC_HOST="${PUBLIC_HOST:-}"
# 对外 HTTPS 端口。刻意不用 443：443/8443/8080 是扫描器的常客。
# 证书绑的是 IP 不是端口，所以换端口不影响证书有效性。
HTTPS_PORT="${HTTPS_PORT:-59287}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[注意] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[中止] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash deploy/deploy.sh"
[[ -n "$PUBLIC_HOST" ]] || die "请指定对外地址：sudo PUBLIC_HOST=你的域名或IP bash deploy/deploy.sh"

say "1/8 创建专用系统账号（不用 root 跑应用）"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home-dir "$APP_DIR" "$APP_USER"

say "2/8 安装系统依赖"
apt-get update -qq
apt-get install -y -qq nginx python3-venv certbot curl rsync

say "3/8 同步代码到 $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
      --exclude '.venv' --exclude '.env' --exclude '*.db' --exclude '__pycache__' \
      --exclude '.runtime' --exclude 'node_modules' \
      "$REPO_ROOT/" "$APP_DIR/"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
mkdir -p "$APP_DIR/.runtime" && chown "$APP_USER:$APP_USER" "$APP_DIR/.runtime"
# 数据库里有交易流水、审计链和账号口令哈希。演示数据不是真实客户信息，
# 但文件默认是 644（同机其他用户可读），公网机器上收紧到 600。
if [[ -f "$APP_DIR/nexus-demo.db" ]]; then
    chown "$APP_USER:$APP_USER" "$APP_DIR/nexus-demo.db"
    chmod 600 "$APP_DIR/nexus-demo.db"
fi

say "4/8 建虚拟环境并装依赖"
sudo -u "$APP_USER" "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

say "5/8 写环境变量文件"
[[ -f "$APP_DIR/.env" ]] || {
    sed -e "s|^NEXUS_ALLOWED_HOSTS=.*|NEXUS_ALLOWED_HOSTS=${PUBLIC_HOST}|" \
        "$APP_DIR/deploy/.env.production.example" > "$APP_DIR/.env"
    warn "已生成 $APP_DIR/.env，请把 NEXUS_LLM_API_KEY 填成你的模型 Key"
    warn "然后重新运行本脚本"
    exit 1
}
grep -q '^NEXUS_LLM_API_KEY=replace\|在这里填你的模型' "$APP_DIR/.env" \
    && die "NEXUS_LLM_API_KEY 还是样例值。先填好再跑一次。"
chmod 600 "$APP_DIR/.env"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"

say "6/8 创建登录账号"
if [[ -z "${OPERATOR_USERNAME:-}" ]]; then
    warn "未指定 OPERATOR_USERNAME，跳过账号创建"
    warn "稍后手动执行： sudo -u $APP_USER bash -c 'cd $APP_DIR && .venv/bin/python -m scripts.create_operator --username <名字> --generate'"
else
    sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && printf '%s' '$OPERATOR_PASSWORD' | .venv/bin/python -m scripts.create_operator \
        --username '$OPERATOR_USERNAME' --display-name '${OPERATOR_DISPLAY_NAME:-$OPERATOR_USERNAME}' --force"
    warn "账号 '$OPERATOR_USERNAME' 就绪。口令是你传进来的那个，部署完记得改。"
fi

say "7/8 申请 TLS 证书并配置 Nginx"
install -m 644 "$APP_DIR/deploy/proxy_params_nexus.conf" /etc/nginx/proxy_params_nexus.conf
sed -e "s|59\.110\.23\.216|${PUBLIC_HOST}|g" \
    -e "s|listen 59287 ssl|listen ${HTTPS_PORT} ssl|g" \
    -e "s|listen \[::\]:59287 ssl|listen [::]:${HTTPS_PORT} ssl|g" \
    "$APP_DIR/deploy/nginx.conf" > /etc/nginx/sites-available/nexus
sed -i "s|/etc/letsencrypt/live/REPLACE_ME|/etc/letsencrypt/live/${PUBLIC_HOST}|g" /etc/nginx/sites-available/nexus
install -m 644 "$APP_DIR/deploy/nginx-limits.conf" /etc/nginx/conf.d/nexus-limits.conf
ln -sf /etc/nginx/sites-available/nexus /etc/nginx/sites-enabled/nexus
rm -f /etc/nginx/sites-enabled/default
mkdir -p /var/www/certbot

if [[ ! -d "/etc/letsencrypt/live/${PUBLIC_HOST}" ]]; then
    warn "申请证书（需要 80 端口可从公网访问，且未被占用）"
    systemctl stop nginx || true
    certbot certonly --standalone --non-interactive --agree-tos \
        --register-unsafely-without-email -d "$PUBLIC_HOST" || die "证书申请失败"
    systemctl start nginx || true
fi

# 换端口的前提是先放行，否则重载后连自己都连不上
ufw allow "${HTTPS_PORT}/tcp" >/dev/null 2>&1 || true

say "8/8 装 systemd 服务并启动"
install -m 644 "$APP_DIR/deploy/nexus.service" /etc/systemd/system/nexus.service
# 应用只监听 127.0.0.1，公网入口只有 Nginx
systemctl daemon-reload
systemctl enable --now nexus
nginx -t && systemctl reload nginx

sleep 3
systemctl is-active --quiet nexus || { journalctl -u nexus -n 40 --no-pager; die "服务没能起来，日志在上面"; }

say "部署完成"
cat <<EOF

  地址     https://${PUBLIC_HOST}:${HTTPS_PORT}
  账号     ${OPERATOR_USERNAME:-（未创建，见上方提示）}

  常用命令
    查看日志   journalctl -u nexus -f
    重启       systemctl restart nexus
    改配置     vim ${APP_DIR}/.env && systemctl restart nexus
    证书续期   certbot renew --dry-run

EOF
