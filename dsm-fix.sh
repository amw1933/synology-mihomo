#!/bin/sh
# 清理 DSM Container Manager 的脏记录（mihomo 版）
#
# 现象：compose 重建容器后，DSM 会多出一条名字带 12 位十六进制前缀的记录，
#       例如 86adba00a4b0_mihomo-admin.config，那个十六进制是「被替换掉的容器 ID 前 12 位」
#       （记录在容器的 com.docker.compose.replace 标签里）。
#       一旦正常的 <名字>.config 被它顶掉，Container Manager 里点容器就会报
#       「No such container」，停止、详情、日志都用不了。
# 做法：把这类带前缀的记录挪到 /tmp，DSM 发现记录缺失，会自动按真实容器名重建 <名字>.config。
set -u

B=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
E="${DSM_CM_ETC:-/var/packages/ContainerManager/etc}"
[ -d "$E" ] || exit 0

# cron 的 PATH 很干净，必须显式找 docker
DOCKER=""
for c in docker /usr/local/bin/docker /usr/bin/docker; do
    if command -v "$c" >/dev/null 2>&1; then DOCKER="$c"; break; fi
done

is_hex12() {
    case "$1" in
        [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) return 0 ;;
        *) return 1 ;;
    esac
}

clean_file() {
    f="$1"
    b=$(basename "$f")
    stem=${b%.config}
    pre=${stem%_*}
    [ "$pre" = "$stem" ] && return 0
    if is_hex12 "$pre"; then
        mv -f "$f" "/tmp/dsm-stale-$b" 2>/dev/null && echo "[$(date '+%F %T')] 已清理脏记录 $b"
    fi
}

# 一、按本项目实际容器名清（兼容改名/加前缀）
names=""
if [ -n "$DOCKER" ]; then
    names=$("$DOCKER" ps -a --format '{{.Names}}|{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null \
            | grep -F "$B" | cut -d'|' -f1)
fi
for n in $names; do
    for f in "$E"/*"$n".config; do
        [ -e "$f" ] || continue
        clean_file "$f"
    done
done

# 二、兜底：不管 docker 能不能用，直接按常见名字清一遍
for n in mihomo mihomo-admin; do
    for f in "$E"/*_"$n".config; do
        [ -e "$f" ] || continue
        clean_file "$f"
    done
done

exit 0
