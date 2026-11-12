#!/bin/sh
# 应用「IP + 端口」设置（mihomo 旁路由版）
# 以特权容器运行，挂载时保持宿主机原路径，所以这里看到的路径就是真实路径
#
# 注意：旁路由版是 host 网络，端口不再是「映射」（7890:7890 那种），
#       而是写进管理台的 PROXY_PORT / PANEL_PORT 环境变量、由配置决定监听端口。
set -u
B=$(cd "$(dirname "$0")" && pwd)
S="$B/settings.json"
C="$B/compose.yaml"

get() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" "$S" 2>/dev/null | head -n1; }

# 没填就自动取本机第一个非回环、非隧道 IPv4（不写死任何地址）
first_ip() {
    ip -4 addr show 2>/dev/null | sed -n 's/.*inet \([0-9.]*\)\/.*/\1/p' \
        | grep -vE '^(127\.|169\.254\.|198\.1[89]\.)' | head -n1
}
PROXY=$(get proxy_ip);     [ -n "${PROXY:-}" ] || PROXY=$(first_ip)
ADMIN=$(get admin_bind);   [ -n "${ADMIN:-}" ] || ADMIN=0.0.0.0
PROXYP=$(get proxy_port);  [ -n "${PROXYP:-}" ] || PROXYP=7890
PANELP=$(get panel_port);  [ -n "${PANELP:-}" ] || PANELP=9090
ADMINP=$(get admin_port);  [ -n "${ADMINP:-}" ] || ADMINP=9090

# 网卡也自动找（群晖一般是 ovs_eth0），广播地址交给内核算
IFACE=$(ip route 2>/dev/null | sed -n 's/^default .* dev \([^ ]*\).*/\1/p' | head -n1)
[ -n "${IFACE:-}" ] || IFACE=ovs_eth0
add_alias() {
    [ -n "$1" ] && [ "$1" != "0.0.0.0" ] || return 0
    ip addr show "$IFACE" 2>/dev/null | grep -q " $1/" || ip addr add "$1/24" dev "$IFACE"
}
add_alias "$PROXY"
add_alias "$ADMIN"

# host 网络：更新管理台 env 里的端口/绑定（不存在的行会原地替换，保持格式统一）
sed -i -E \
  -e "s|(- ADMIN_PORT=).*|\1${ADMINP}|" \
  -e "s|(- ADMIN_BIND=).*|\1${ADMIN}|" \
  -e "s|(- PROXY_PORT=).*|\1${PROXYP}|" \
  -e "s|(- PANEL_PORT=).*|\1${PANELP}|" \
  "$C"

cd "$B" || exit 1

# 项目名跟着现有容器走（Container Manager 建的项目名可能不一样），找不到就用 mihomo
DOCKER=""
for c in docker /usr/local/bin/docker /usr/bin/docker; do
    if command -v "$c" >/dev/null 2>&1; then DOCKER="$c"; break; fi
done
PROJ=""
if [ -n "$DOCKER" ]; then
    PROJ=$("$DOCKER" ps -a --format '{{.Label "com.docker.compose.project"}}' \
           --filter "label=com.docker.compose.project.working_dir=$B" 2>/dev/null | head -n1)
fi
[ -n "$PROJ" ] || PROJ="${PROJECT:-mihomo}"

"$DOCKER" compose -p "$PROJ" -f "$C" up -d

# compose 重建会让 DSM 的记录变脏，清一下让 DSM 自己重建
[ -x "$B/dsm-fix.sh" ] && sh "$B/dsm-fix.sh"

exit 0
