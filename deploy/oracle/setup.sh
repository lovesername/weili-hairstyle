#!/usr/bin/env bash
# Oracle Cloud Always Free ARM 一键部署脚本（Ubuntu 22.04/24.04 aarch64）
#
#   bash setup.sh            # 首次部署
#   bash setup.sh update     # 拉代码 + 重新构建 + 重启（数据不动）
#   bash setup.sh logs       # 看日志
#
set -euo pipefail

APP=/opt/weili
REPO="${REPO:-}"          # 你的 GitHub 仓库地址，例：https://github.com/xxx/weili.git
DOMAIN="${DOMAIN:-}"

say(){ printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
die(){ printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

cd_to_app(){ [ -d "$APP" ] || die "$APP 不存在，先跑一次 setup.sh"; cd "$APP/deploy/oracle"; }

case "${1:-setup}" in
logs) cd_to_app; docker compose logs -f --tail=200; exit 0 ;;
update)
    cd_to_app
    say "拉取代码"; git -C "$APP" pull --ff-only
    say "重新构建并重启"; docker compose up -d --build
    say "等待健康检查"; sleep 20; docker compose ps
    curl -fsS "https://${DOMAIN}/api/health" && printf '\n✓ 已更新\n'
    exit 0 ;;
esac

# ---------------- 首次部署 ----------------
[ -n "$REPO" ] || die "请先设置仓库地址：REPO=https://github.com/<用户名>/<仓库名>.git bash setup.sh"
[ -n "$DOMAIN" ] || die "请先设置域名：DOMAIN=api.你的域名.com REPO=... bash setup.sh"

say "安装 Docker"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y ca-certificates curl gnupg
  curl -fsSL https://get.docker.com | sh
  usermod -aG docker "${SUDO_USER:-$USER}" || true
fi
docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件"

say "放开 80 / 443"
iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT 2>/dev/null || true
iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT 2>/dev/null || true
netfilter-persistent save 2>/dev/null || true

say "准备代码 $APP"
if [ -d "$APP/.git" ]; then git -C "$APP" pull --ff-only; else git clone "$REPO" "$APP"; fi
mkdir -p "$APP/deploy/oracle/runtime"

say "生成 .env"
ENVF="$APP/deploy/oracle/.env"
if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<EOF
# ---- 必填 ----
DOMAIN=${DOMAIN}
DEPLOY=1
PROVIDER=apimart
APIMART_API_KEY=
APIMART_IMAGE_MODEL=gpt-image-2
ADMIN_USER=admin
ADMIN_PASS=
ADMIN_TOKEN=

# ---- 计费 ----
GEN_CREDITS=8
FREE_GENS=3
USER_MAX_N=2
USD_CNY=7.15
RENDER_SIZE=768
EOF
  say "已生成 $ENVF —— 请把 APIMART_API_KEY / ADMIN_PASS / ADMIN_TOKEN 填进去，再重跑本脚本"
  exit 0
fi

grep -q '^APIMART_API_KEY=.\+' "$ENVF" || die "$ENVF 里 APIMART_API_KEY 还是空的"
grep -q '^ADMIN_PASS=.\+'     "$ENVF" || die "$ENVF 里 ADMIN_PASS 还是空的"

say "构建并启动"
docker compose up -d --build

say "等待就绪"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8899/api/health" >/dev/null 2>&1; then break; fi
  sleep 2
done

docker compose ps
cat <<EOF

────────────────────────────────────────
  后端地址（填进 GitHub 仓库变量 API_BASE）
  https://${DOMAIN}

  后台登录  https://${DOMAIN}/admin

  ⚠ 去 Oracle Cloud 控制台 → 子网 → 安全列表，
    加一条入站规则：0.0.0.0/0 + TCP 80,443（实例自带的 iptables 上面已放开）
  ⚠ DNS 里把 ${DOMAIN} 的 A 记录指向这台机器的公网 IP
────────────────────────────────────────
EOF
