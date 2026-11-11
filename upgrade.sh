#!/bin/bash
# ============================================================================
#  群晖 mihomo 一键升级
#    只做两件事：拉最新管理台镜像 → 重建容器
#    配置、订阅、机场、各业务组线路都存在 settings.json / etc 里，升级不会丢
#
#  用法（SSH）：
#     sudo bash upgrade.sh
#     # 或指定目录： sudo BASE=/volume1/docker/mihomo bash upgrade.sh
# ============================================================================
set -u
D=$(command -v docker 2>/dev/null || echo /usr/local/bin/docker)
BASE="${BASE:-/volume1/docker/mihomo}"
ADMIN_PORT="${ADMIN_PORT:-9090}"

[ -d "$BASE" ] || { echo "找不到项目目录 $BASE（用 BASE=你的目录 指定）"; exit 1; }
cd "$BASE" || exit 1

echo "== 1/3 拉最新管理台镜像 =="
$D compose pull mihomo-admin || echo "  （拉取失败：可能网络不通，或本来就用的本地构建镜像）"

echo "== 2/3 重建管理台容器（不会动内核容器和配置）=="
$D compose up -d --force-recreate mihomo-admin

echo "== 3/3 状态 =="
sleep 8
$D ps --format '  {{.Names}} | {{.Status}}' | grep mihomo || echo "  没看到 mihomo 容器"
echo -n "  管理台 http://127.0.0.1:$ADMIN_PORT/ -> "
curl -s -m 8 -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:$ADMIN_PORT/" || true
echo
echo "升级完成。设置/机场/节点都在 $BASE/settings.json，没有丢。"
echo "如果界面还是旧的：浏览器按 Ctrl+F5 强刷一次。"
