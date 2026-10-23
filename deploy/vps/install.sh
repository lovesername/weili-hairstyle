#!/usr/bin/env bash
# 微粒·WEILI —— 自有云主机一键部署（Ubuntu / Debian / CentOS / Alma / Rocky / 阿里云 Linux 通用）
#
# 用法（在服务器上以 root 执行）：
#   bash install.sh aiwig.weilees.cn          # 装依赖 + 起服务 + 配 HTTPS
#   bash install.sh aiwig.weilees.cn --no-ssl # 只起 HTTP（证书稍后再说）
#   bash install.sh --update                  # 拉新代码后重启（数据不动）
#   bash install.sh --logs                    # 看日志
#
# 前提：/opt/weili 里已经有代码（本地打包上传或 git clone），并且 /opt/weili/.env 已填好。
set -euo pipefail

APP=/opt/weili
SVC=weili
PORT=8899

say(){ printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok(){  printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
die(){ printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 子命令
case "${1:-}" in
  --logs) exec journalctl -u "$SVC" -f --no-pager ;;
  --reset-data)
    [ -f "$APP/deploy/vps/dump/app.db" ] || die "没有 $APP/deploy/vps/dump/app.db"
    [ "$(id -u)" = "0" ] || die "请用 root 执行"
    systemctl stop "$SVC" 2>/dev/null || true
    cp "$APP/storage/app.db" "$APP/storage/app.db.bak.$(date +%s)" 2>/dev/null || true
    cp "$APP/deploy/vps/dump/app.db" "$APP/storage/app.db"
    "$APP/venv/bin/python" -c "import sqlite3,sys;c=sqlite3.connect('$APP/storage/app.db');c.execute(\"update photos set path=replace(path,'__ROOT__','$APP') where path like '__ROOT__%'\");c.commit()"
    systemctl start "$SVC" 2>/dev/null || true
    ok "数据已重灌（旧库备份为 app.db.bak.*）"
    exit 0 ;;
  --update)
    [ -d "$APP/backend" ] || die "$APP 不存在"
    say "重启 $SVC"
    systemctl restart "$SVC"
    sleep 3; systemctl --no-pager --lines=0 status "$SVC" | head -5 || true
    curl -fsS "http://127.0.0.1:$PORT/api/health" && printf '\n'; ok "已重启"
    exit 0 ;;
esac

DOMAIN="${1:-}"
NO_SSL=0
for a in "$@"; do [ "$a" = "--no-ssl" ] && NO_SSL=1; done
[ -n "$DOMAIN" ] || die "用法：bash install.sh 你的域名 [--no-ssl]"
[ "$(id -u)" = "0" ] || die "请用 root 执行（sudo -i 之后再跑）"
[ -d "$APP/backend" ] || die "$APP 里没有代码，先把 weili-src.tar.gz 解压到 $APP"

# ---------------------------------------------------------------- 包管理器
say "识别系统"
if command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v yum >/dev/null 2>&1; then PM=yum
else die "不认识的系统（没有 apt / dnf / yum）"
fi
ok "$PM / $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"

pk_install(){
  case "$PM" in
    apt) DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
    dnf) dnf install -y -q "$@" ;;
    yum) yum install -y -q "$@" ;;
  esac
}

say "装系统依赖"
case "$PM" in
  apt) apt-get update -qq; pk_install python3 python3-venv python3-pip nginx curl ca-certificates ;;
  *)   pk_install python3 python3-pip nginx curl ca-certificates ;;
esac
python3 -V

# ---------------------------------------------------------------- Python 环境
say "建虚拟环境 + 装依赖（走清华镜像，国内快）"
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
PIP="$APP/venv/bin/pip"
"$PIP" install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
"$PIP" install -q -r "$APP/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
ok "依赖装好：$("$APP/venv/bin/python" -c 'import fastapi,cv2,PIL;print("fastapi",fastapi.__version__,"| cv2",cv2.__version__)')"

# ---------------------------------------------------------------- 目录 & 权限
mkdir -p "$APP/storage/uploads" "$APP/storage/results" "$APP/storage/candidates" \
         "$APP/storage/debug" "$APP/storage/qr"

[ -f "$APP/.env" ] || die "缺少 $APP/.env（环境变量），先上传再跑"
chmod 600 "$APP/.env"
grep -q '^APIMART_API_KEY=.\+' "$APP/.env" || die "$APP/.env 里 APIMART_API_KEY 是空的"
if grep -q '^DATABASE_URL=.\+' "$APP/.env"; then
  printf '\033[1;33m! .env 里还有 DATABASE_URL，数据会继续写云端而不是本机。\033[0m\n'
  printf '\033[1;33m  要用本机 SQLite 就把它注释掉，再 systemctl restart weili\033[0m\n'
fi

# ---------------------------------------------------------------- 数据迁移（Supabase → 本机）
DATA="$APP/deploy/vps/dump"
if [ -d "$DATA" ]; then
  if [ -f "$APP/storage/app.db" ]; then
    ok "已存在 storage/app.db，保留现有数据，不覆盖（要重灌加 --reset-data）"
  elif [ -f "$DATA/app.db" ]; then
    say "导入线上数据（账号 / 订单 / 积分 / 生成记录）"
    cp "$DATA/app.db" "$APP/storage/app.db"
    # photos.path 里存的是 __ROOT__/storage/uploads/xxx.jpg，换成真实路径
    "$APP/venv/bin/python" - "$APP" <<'PYEOF'
import sqlite3, sys
app = sys.argv[1]
c = sqlite3.connect(app + "/storage/app.db")
c.execute("update photos set path = replace(path,'__ROOT__',?) where path like '__ROOT__%'", (app,))
c.commit()
print("  照片路径已改写：", c.execute("select count(*) from photos").fetchone()[0], "条")
print("  账号：", c.execute("select count(*) from accounts").fetchone()[0],
      "| 用户：", c.execute("select count(*) from users").fetchone()[0],
      "| 订单：", c.execute("select count(*) from orders").fetchone()[0],
      "| 生成记录：", c.execute("select count(*) from jobs").fetchone()[0])
PYEOF
    if [ -d "$DATA/results" ]; then
      cp -n "$DATA/results/"*.jpg "$APP/storage/results/" 2>/dev/null || true
      ok "成品图 $(ls "$APP/storage/results" | wc -l) 张已就位"
    fi
    ok "数据导入完成"
  fi
fi

# ---------------------------------------------------------------- systemd
say "写 systemd 服务"
cat > /etc/systemd/system/$SVC.service <<EOF
[Unit]
Description=WEILI AI Hairstyle API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP/backend
EnvironmentFile=$APP/.env
Environment=PYTHONUNBUFFERED=1
Environment=DEPLOY=1
ExecStart=$APP/venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port $PORT --workers 1 --proxy-headers
Restart=always
RestartSec=3
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable $SVC >/dev/null
systemctl restart $SVC

say "等服务起来"
for i in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then break; fi
  sleep 2
done

if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  ok "后端 OK：$(curl -fsS http://127.0.0.1:$PORT/api/health)"
else
  journalctl -u $SVC -n 40 --no-pager
  die "后端起不来，日志见上"
fi

# ---------------------------------------------------------------- nginx
say "配 nginx 反代"
NGINX_CONF=/etc/nginx/conf.d/weili.conf
[ "$PM" = apt ] && NGINX_CONF=/etc/nginx/sites-available/weili.conf

cat > "$NGINX_CONF" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    client_max_body_size 24m;
    client_body_timeout 120s;

    # 出图一趟 30~60 秒，别让 nginx 先掐断
    proxy_connect_timeout 30s;
    proxy_send_timeout    300s;
    proxy_read_timeout    300s;
    proxy_buffering off;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade           \$http_upgrade;
        proxy_set_header Connection        "upgrade";
    }
}
EOF

if [ "$PM" = apt ]; then
  ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/weili.conf
  rm -f /etc/nginx/sites-enabled/default
fi

nginx -t
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx
ok "nginx 已生效"

# ---------------------------------------------------------------- 防火墙
say "放行 80 / 443（本机防火墙）"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q active; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ok "ufw 已放行"
elif command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-service=http >/dev/null
  firewall-cmd --permanent --add-service=https >/dev/null
  firewall-cmd --reload >/dev/null; ok "firewalld 已放行"
else
  iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 80 -j ACCEPT
  iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 443 -j ACCEPT
  ok "iptables 已放行"
fi

# ---------------------------------------------------------------- HTTPS
if [ "$NO_SSL" = "0" ]; then
  say "申请 HTTPS 证书（Let's Encrypt，免费自动续）"
  case "$PM" in
    apt) pk_install certbot python3-certbot-nginx || true ;;
    *)   pk_install certbot python3-certbot-nginx || pk_install certbot || true ;;
  esac
  if command -v certbot >/dev/null 2>&1; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
      --register-unsafely-without-email --redirect || \
      printf '\033[1;33m! 证书没申请成功（多半是域名还没解析到这台机器，或 80 端口没通）\033[0m\n'
  else
    printf '\033[1;33m! 没装上 certbot，先跑 HTTP\033[0m\n'
  fi
fi

# ---------------------------------------------------------------- 收尾
PUBIP=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo "?")
cat <<EOF

────────────────────────────────────────────
  部署完成
  本机地址   http://127.0.0.1:$PORT/api/health
  公网 IP    $PUBIP
  域名       http://$DOMAIN
  后台       http://$DOMAIN/admin
  日志       bash install.sh --logs
  重启       systemctl restart $SVC
────────────────────────────────────────────
还没做的两件事：
  1) 域名 A 记录指向 $PUBIP（DNSPod 里把 aiwig 那条改掉）
  2) 云厂商控制台的「安全组 / 防火墙」放行 TCP 80、443
EOF
