#!/bin/bash
# resolve-conflicts.sh [--check-only]     （在源码树根目录运行，需先有 .config 且已 make defconfig）
# 处理“会往镜像里写同一个文件”的互斥软件包，避免打包阶段 check_data_file_clashes 报错：
#   libustream-*  /lib/libustream-ssl.so         tcpdump / tcpdump-mini  /usr/bin/tcpdump
#   dnsmasq*      /usr/sbin/dnsmasq              ip-full / ip-tiny / ip-bridge
#   wget-ssl / wget-nossl                         wpad*  /usr/sbin/hostapd
#   odhcpd / odhcpd-ipv6only
# 策略（每个家族）：保留“用户/官方种子显式选择”的那个（读取 $ASSEMBLED，默认 .config.assembled），
#   其余先关闭；若被依赖包 select 回来（说明是硬依赖），则反过来放弃原先保留的；
#   两边都被硬依赖时在编译前失败，并打印是谁选了它。
# 被关闭的包写入 .resolver-dropped，供 check-retention.sh 排除。
set -euo pipefail
CHECK_ONLY=0; [ "${1:-}" = "--check-only" ] && CHECK_ONLY=1
ASSEMBLED="${ASSEMBLED:-.config.assembled}"

FAMILIES=(
  "libustream-mbedtls libustream-openssl libustream-wolfssl"
  "tcpdump tcpdump-mini"
  "dnsmasq-full dnsmasq dnsmasq-dhcpv6"
  "ip-full ip-tiny ip-bridge"
  "wget-ssl wget-nossl"
  "wpad-openssl wpad-mbedtls wpad-wolfssl wpad-basic-mbedtls wpad-basic-openssl wpad-basic-wolfssl wpad-mini"
  "odhcpd odhcpd-ipv6only"
)

pkg_on()  { grep -q "^CONFIG_PACKAGE_${1}=y" .config; }
in_assembled() { [ -f "$ASSEMBLED" ] && grep -q "^CONFIG_PACKAGE_${1}=y" "$ASSEMBLED"; }
pkg_off() {
  sed -i "/^CONFIG_PACKAGE_${1}=/d;/^# CONFIG_PACKAGE_${1} is not set/d" .config
  echo "# CONFIG_PACKAGE_${1} is not set" >> .config
  echo "$1" >> .resolver-dropped.tmp
}
pkg_set() {
  sed -i "/^CONFIG_PACKAGE_${1}=/d;/^# CONFIG_PACKAGE_${1} is not set/d" .config
  echo "CONFIG_PACKAGE_${1}=y" >> .config
  sed -i "/^${1}\$/d" .resolver-dropped.tmp
}
who_selects() {
  echo "----- 谁依赖/选择了 ${1} -----"
  [ -f tmp/.config-package.in ] || { echo '  (tmp/.config-package.in 不存在)'; return 0; }
  awk -v t="PACKAGE_${1}" '
    /^config /{cur=$2}
    $0 ~ ("select " t "($|[ \t])") {print "  <- " cur}
  ' tmp/.config-package.in | sort -u | head -n 20 || true
}
count_on() { local c=0 p; for p in "$@"; do pkg_on "$p" && c=$((c+1)); done; echo "$c"; }

if [ "$CHECK_ONLY" -eq 1 ]; then
  bad=0
  for fam in "${FAMILIES[@]}"; do
    # shellcheck disable=SC2086
    if [ "$(count_on $fam)" -gt 1 ]; then
      echo "::error::互斥包同时启用: $(for p in $fam; do pkg_on "$p" && printf '%s ' "$p"; done)"; bad=1
    fi
  done
  [ "$bad" -eq 0 ] && echo '[OK]   无同文件互斥包同时启用'
  exit "$bad"
fi

: > .resolver-dropped.tmp
resolve_family() {
  local fam=($1) on=() order=() p keep
  for p in "${fam[@]}"; do pkg_on "$p" && on+=("$p"); done
  [ "${#on[@]}" -le 1 ] && return 0
  echo "[CONFLICT] ${on[*]} 同时启用"
  for p in "${on[@]}"; do in_assembled "$p" && order+=("$p"); done      # 显式选择优先
  for p in "${on[@]}"; do in_assembled "$p" || order+=("$p"); done      # 其余按家族顺序
  keep="${order[0]}"
  for p in "${order[@]:1}"; do
    pkg_off "$p"; make defconfig >/dev/null 2>&1
    if pkg_on "$p"; then
      echo "[INFO] ${p} 被硬依赖重新选中，改为放弃 ${keep}"
      who_selects "$p"
      pkg_off "$keep"; pkg_set "$p"; make defconfig >/dev/null 2>&1
      if pkg_on "$keep"; then
        echo "::error::${keep} 与 ${p} 均被硬依赖选中，无法自动解决。"
        who_selects "$keep"; exit 1
      fi
      keep="$p"
    fi
  done
  echo "[FIXED] 保留 ${keep}"
}

for round in 1 2; do
  for fam in "${FAMILIES[@]}"; do resolve_family "$fam"; done
done

bash "$0" --check-only
sort -u .resolver-dropped.tmp > .resolver-dropped; rm -f .resolver-dropped.tmp
echo "[OK]   冲突处理完成；已关闭: $(tr '\n' ' ' < .resolver-dropped)"
