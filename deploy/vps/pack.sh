#!/usr/bin/env bash
# 本地打包（Windows 的 Git Bash 也能跑）
#   bash deploy/vps/pack.sh
# 产出 ../weili-src.tar.gz，scp 到服务器后解压到 /opt/weili
set -euo pipefail
cd "$(dirname "$0")/../.."

OUT="${1:-../weili-src.tar.gz}"
tar -czf "$OUT" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
  --exclude='storage/uploads/*' --exclude='storage/results/*' --exclude='storage/candidates/*' \
  --exclude='storage/app.db' --exclude='.env' --exclude='deploy/vps/env.server' \
  backend frontend models data requirements.txt deploy/vps

printf '打包完成: %s (%s)\n' "$OUT" "$(du -h "$OUT" | cut -f1)"
echo "上传:  scp $OUT root@<服务器IP>:/opt/weili-src.tar.gz"
