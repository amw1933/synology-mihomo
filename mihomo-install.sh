#!/bin/bash
# ============================================================
#  群晖 mihomo 一键部署（含网页管理台）
#  仓库：https://github.com/amw1933/synology-mihomo
#
#  用法（SSH 里 root 执行）：
#     sudo -i
#     curl -fsSL https://raw.githubusercontent.com/amw1933/synology-mihomo/main/mihomo-install.sh | bash
#
#  可选参数（不填就用默认值）：
#     SUB_URL=订阅链接     自动导入机场订阅（Clash 格式即可）
#     ADMIN_PASS=密码      管理台密码（不填会随机生成并打印）
#     MH_IP=192.168.1.100  绑定 IP（默认自动识别本机 IP）
#     PROXY_PORT=7890      代理端口（App 手填代理时才用）
#     PANEL_PORT=9091      内核控制接口端口（只绑 127.0.0.1，不用管）
#     ADMIN_PORT=9090      唯一对外端口：管理台 + /ui/ 面板 + /clash/ 内核反代
#     TUN=1                1=旁路由（默认，开 TUN）；0=只提供代理端口
#     BASE_DIR=/volume1/docker/mihomo    安装目录
#
#  注意：本脚本装的是「旁路由 / TUN」版本 —— 在路由器里把设备的网关指向这台群晖，
#        设备就全局走代理（不用填端口）。内核用 host 网络 + NET_ADMIN + /dev/net/tun，
#        所以本机自己的流量也会走代理。
#        回退：把 settings.json 的 tun_enable 改成 false，重新生成配置即可。
# ============================================================
set -u

RAW="${RAW:-https://raw.githubusercontent.com/amw1933/synology-mihomo/main}"
BASE="${BASE_DIR:-/volume1/docker/mihomo}"
PROXY_PORT="${PROXY_PORT:-7890}"
PANEL_PORT="${PANEL_PORT:-9091}"
TUN="${TUN:-1}"
ADMIN_PORT="${ADMIN_PORT:-9090}"
ADMIN_PASS="${ADMIN_PASS:-}"
SUB_URL="${SUB_URL:-}"
MH_IP="${MH_IP:-}"
# 想在一台机器上装第二套时，用 CT_PREFIX=t- 给容器名加前缀
CT_PREFIX="${CT_PREFIX:-}"
CT_MH="${CT_PREFIX}mihomo"
PROJECT="${PROJECT:-mihomo}"
CT_ADMIN="${CT_PREFIX}mihomo-admin"

msg() { echo; echo "==> $*"; }
die() { echo; echo "错误：$*" >&2; exit 1; }

# ---------- 1. 环境检查 ----------
msg "检查环境"
[ "$(id -u)" = "0" ] || die "请用 root 运行：先执行 sudo -i"

DOCKER=""
for c in /usr/local/bin/docker /usr/bin/docker docker; do
    if command -v "$c" >/dev/null 2>&1; then DOCKER="$c"; break; fi
done
[ -n "$DOCKER" ] || die "没找到 docker，请先在「套件中心」安装 Container Manager"
"$DOCKER" compose version >/dev/null 2>&1 || die "docker compose 不可用，请确认 Container Manager 已安装并启动"
COMPOSE="$DOCKER compose"
echo "    docker : $DOCKER"
echo "    目录   : $BASE"

# ---------- 2. 识别 IP ----------
if [ -z "$MH_IP" ]; then
    MH_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1);exit}}')
fi
if [ -z "$MH_IP" ]; then
    MH_IP=$(ip -4 addr show scope global 2>/dev/null | awk '/inet /{split($2,a,"/");print a[1];exit}')
fi
[ -n "$MH_IP" ] || die "无法自动识别本机 IP，请用 MH_IP=你的IP 重新运行"
echo "    绑定 IP: $MH_IP"

# ---------- 3. 管理台密码 ----------
if [ -z "$ADMIN_PASS" ]; then
    ADMIN_PASS=$(head -c 64 /dev/urandom | tr -dc 'a-zA-Z0-9' | head -c 10)
    GENPASS=1
else
    GENPASS=0
fi

# ---------- 4. 下载程序文件 ----------
msg "下载程序文件"
mkdir -p "$BASE" "$BASE/etc" "$BASE/etc/ui" "$BASE/admin" "$BASE/tmp" || die "创建目录失败"
dl() {
    # SRC_DIR 已存在则本地复制（离线安装用），否则从仓库下载
    if [ -n "${SRC_DIR:-}" ] && [ -f "$SRC_DIR/$1" ]; then
        cp -f "$SRC_DIR/$1" "$2" || die "复制 $1 失败"
        return 0
    fi
    curl -fsSL --connect-timeout 20 --retry 2 "$RAW/$1?t=$(date +%s)" -o "$2" || die "下载 $1 失败（检查网络或仓库地址）"
}
dl settings-apply.sh "$BASE/settings-apply.sh"
dl dsm-fix.sh        "$BASE/dsm-fix.sh"
dl admin/app.py      "$BASE/admin/app.py"
dl admin/sub_convert.py "$BASE/admin/sub_convert.py"
dl admin/Dockerfile  "$BASE/admin/Dockerfile"
dl compose.yaml      "$BASE/compose.yaml"
chmod 755 "$BASE"/*.sh
echo "    已下载到 $BASE"

# ---------- 5. 生成配置 ----------
msg "生成配置文件"
# 换到没带 TUN 的群晖上时自动降级，避免容器起不来
if [ "$TUN" = "1" ] && [ ! -c /dev/net/tun ]; then
    echo "    !! 这台群晖没有 /dev/net/tun（内核不带 TUN），自动改用「端口模式」"
    TUN=0
fi
sed -i \
    -e "s|__IP__|$MH_IP|g" \
    -e "s|__BASE__|$BASE|g" \
    -e "s|__PASS__|$ADMIN_PASS|g" \
    -e "s|__PROXY_PORT__|$PROXY_PORT|g" \
    -e "s|__PANEL_PORT__|$PANEL_PORT|g" \
    -e "s|__ADMIN_PORT__|$ADMIN_PORT|g" \
    -e "s|__CT_MH__|$CT_MH|g" \
    -e "s|__CT_ADMIN__|$CT_ADMIN|g" \
    -e "s|__TUN_ENABLE__|$TUN|g" \
    "$BASE/compose.yaml" || die "生成 compose.yaml 失败"

# 端口模式：去掉 TUN 相关的权限与设备映射（没有 tun 设备的机器否则会启动失败）
if [ "$TUN" != "1" ]; then
    sed -i -e '/cap_add:/d' -e '/- NET_ADMIN/d' -e '/devices:/d' -e '/\/dev\/net\/tun/d' "$BASE/compose.yaml"
    echo "    已去掉 compose 里的 TUN 权限/设备（端口模式，不需要改路由）"
fi

cat > "$BASE/settings.json" <<EOF
{
  "schedule_enabled": true,
  "schedule_time": "04:00",
  "last_run": "",
  "admin_bind": "",
  "proxy_ip": "",
  "proxy_port": "$PROXY_PORT",
  "panel_port": "$PANEL_PORT",
  "admin_port": "$ADMIN_PORT",
  "secret": "$ADMIN_PASS",
  "tun_enable": $([ "$TUN" = "1" ] && echo true || echo false),
  "direct_domains": ""
}
EOF

printf '%s\n' "metacubex/mihomo:latest" > "$BASE/image.name"
[ -n "$SUB_URL" ] && printf '%s\n' "$SUB_URL" > "$BASE/sub.url"

# ---------- 6. 构建并启动 ----------
msg "拉取 mihomo 内核镜像"
if $DOCKER pull metacubex/mihomo:latest >/dev/null 2>&1; then
    echo "    已拉取 metacubex/mihomo:latest（Docker Hub 官方发布）"
elif $DOCKER pull ghcr.io/metacubex/mihomo:latest >/dev/null 2>&1; then
    # ghcr.io 能拉通时，打个 Docker Hub 的标签（compose 里写的是 metacubex/mihomo）
    $DOCKER tag ghcr.io/metacubex/mihomo:latest metacubex/mihomo:latest
    echo "    Docker Hub 不通，已改用 ghcr.io 版本并打标签"
else
    die "mihomo 内核镜像拉取失败（Docker Hub 与 ghcr.io 都不通），请检查网络或配置镜像加速"
fi

msg "构建管理台镜像（第一次约 1-2 分钟）"
cd "$BASE" || die "进入 $BASE 失败"
$COMPOSE -p "$PROJECT" -f compose.yaml build || die "构建失败"

msg "启动容器"
$COMPOSE -p "$PROJECT" -f compose.yaml up -d || die "启动失败"

# ---------- 7. 绑定 IP（如果还不是本机地址） ----------
if ! ip addr show 2>/dev/null | grep -q " $MH_IP/"; then
    msg "添加 IP $MH_IP"
    ip addr add "$MH_IP/24" dev "$(ip route show default | awk '{print $5;exit}')" 2>/dev/null || true
    cat > /usr/local/etc/rc.d/mhip.sh <<EOF
#!/bin/sh
S=$BASE/settings.json
get() { sed -n "s/.*\"\$1\": *\"\\([^\"]*\\)\".*/\\1/p" "\$S" 2>/dev/null | head -n1; }
add() {
  [ -n "\$1" ] && [ "\$1" != "0.0.0.0" ] || return 0
  /sbin/ip addr show 2>/dev/null | grep -q " \$1/" || /sbin/ip addr add "\$1/24" dev "\$(/sbin/ip route show default | awk '{print \$5;exit}')"
}
add "\$(get proxy_ip)"
add "\$(get admin_bind)"
EOF
    chmod 755 /usr/local/etc/rc.d/mhip.sh
    echo "    已写入开机自动添加 IP：/usr/local/etc/rc.d/mhip.sh"
fi

# ---------- 8. 定期清理 DSM 记录 ----------
# 容器被重建后 DSM 会多出带十六进制前缀的记录，导致容器管理里点不动，这里定期清掉
cat > /etc/cron.d/mh-dsmfix <<EOF
*/10 * * * * root sh $BASE/dsm-fix.sh >> $BASE/dsm-fix.log 2>&1
EOF
chmod 644 /etc/cron.d/mh-dsmfix
echo
echo "    已加入定期清理：/etc/cron.d/mh-dsmfix（每 10 分钟）"

# ---------- 9. 生成节点配置 ----------
msg "生成 mihomo 配置"
for i in $(seq 1 30); do
    curl -s -o /dev/null -m 3 "http://$MH_IP:$ADMIN_PORT/" && break
    sleep 2
done
curl -s -c /tmp/mhck -m 20 -d "pass=$ADMIN_PASS" "http://$MH_IP:$ADMIN_PORT/login" >/dev/null 2>&1
R=$(curl -s -b /tmp/mhck -m 240 -X POST "http://$MH_IP:$ADMIN_PORT/api/gen" 2>/dev/null)
rm -f /tmp/mhck
case "$R" in
    *'"ok"'*true*|*'"ok": true'*) echo "    完成" ;;
    *) echo "    稍后可在网页管理台点「立即更新配置」重试" ;;
esac

# ---------- 10. 安装节点面板界面 ----------
msg "安装节点面板界面"
sleep 3
curl -s -c /tmp/mhck -m 20 -d "pass=$ADMIN_PASS" "http://$MH_IP:$ADMIN_PORT/login" >/dev/null 2>&1
R=$(curl -s -b /tmp/mhck -m 180 -X POST "http://$MH_IP:$ADMIN_PORT/api/panel-ui" 2>/dev/null)
rm -f /tmp/mhck
case "$R" in
    *'"ok"'*true*|*'"ok": true'*) echo "    完成" ;;
    *) echo "    跳过（可稍后在管理台点「一键安装面板界面」）" ;;
esac

# ---------- 11. 结果 ----------
IPURL="$MH_IP"
echo
echo "============================================================"
echo "  部署完成"
echo "============================================================"
echo "  管理台       http://$IPURL:$ADMIN_PORT          ← 唯一入口（面板也在里面）"
echo "  节点面板     http://$IPURL:$ADMIN_PORT/ui/       （或管理台里点「节点面板（内嵌）」）"
echo "               面板密钥 = 管理台密码"
echo "  代理地址     http://$IPURL:$PROXY_PORT           （HTTP / SOCKS5，可选）"
echo "  管理台密码   $ADMIN_PASS"
if [ "$TUN" = "1" ]; then
    echo
    echo "  【旁路由已开启】在路由器里把设备的「网关」填成下面这个 IP，设备就全局走代理："
    echo "      网关 = $IPURL     （DNS 可以不动，会被内核接管）"
fi
if [ "$GENPASS" = "1" ]; then
    echo "               （随机生成，请记下来，也可以改："
    echo "                 sed -i 's/^      - ADMIN_PASS=.*/      - ADMIN_PASS=新密码/' $BASE/compose.yaml"
    echo "                 cd $BASE && $COMPOSE -p $PROJECT up -d  )"
fi
echo
echo "  下一步："
if [ ! -s "$BASE/sub.url" ]; then
    echo "    1) 打开管理台 → 「机场」→ 粘贴订阅链接 → 添加"
    echo "    2) 回「状态」→ 点「立即更新配置」"
    echo "    3) 代理地址填到手机/电脑上即可"
else
    echo "    代理地址填到手机/电脑上即可；以后在管理台里维护"
fi
echo "============================================================"
echo
