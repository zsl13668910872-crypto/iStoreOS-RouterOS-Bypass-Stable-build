#!/bin/bash
# assemble-config.sh SEED OVERLAY      （在源码树根目录运行）
# 生成 .config：  官方/回退 种子  <  仓库 overlay.config  <  工作流输入（后者覆盖前者）
# 环境变量：ROOTFS_PARTSIZE(默认4096)  ROOTFS_TYPE(squashfs|ext4|both，默认squashfs)
#          ENABLE_BBR(true/false)  ENABLE_OPENCLASH(true/false)  CUSTOM_PACKAGES(空格分隔)
set -euo pipefail
SEED="${1:?SEED}"; OVERLAY="${2:?OVERLAY}"
[ -s "$SEED" ] || { echo "::error::种子配置为空: $SEED"; exit 1; }

ROOTFS_PARTSIZE="${ROOTFS_PARTSIZE:-4096}"
ROOTFS_TYPE="${ROOTFS_TYPE:-squashfs}"
ENABLE_BBR="${ENABLE_BBR:-true}"
ENABLE_OPENCLASH="${ENABLE_OPENCLASH:-true}"
CUSTOM_PACKAGES="${CUSTOM_PACKAGES:-}"

dyn="$(mktemp)"
add_if_present() {
  local pkg="$1"
  if find package feeds -maxdepth 4 -name "$pkg" -print -quit 2>/dev/null | grep -q .; then
    echo "CONFIG_PACKAGE_${pkg}=y" >> "$dyn"; echo "[OK]   ${pkg}"
  else
    echo "[WARN] 源码树中不存在，已跳过: ${pkg}"
  fi
}

{
  echo 'CONFIG_TARGET_x86=y'
  echo 'CONFIG_TARGET_x86_64=y'
  echo 'CONFIG_TARGET_x86_64_DEVICE_generic=y'
  echo 'CONFIG_GRUB_IMAGES=y'
  echo 'CONFIG_GRUB_EFI_IMAGES=y'
  echo "CONFIG_TARGET_ROOTFS_PARTSIZE=${ROOTFS_PARTSIZE}"
  case "$ROOTFS_TYPE" in
    squashfs) echo 'CONFIG_TARGET_ROOTFS_SQUASHFS=y'; echo '# CONFIG_TARGET_ROOTFS_EXT4FS is not set' ;;
    ext4)     echo 'CONFIG_TARGET_ROOTFS_EXT4FS=y';   echo '# CONFIG_TARGET_ROOTFS_SQUASHFS is not set' ;;
    both)     echo 'CONFIG_TARGET_ROOTFS_SQUASHFS=y'; echo 'CONFIG_TARGET_ROOTFS_EXT4FS=y' ;;
    *) echo "::error::未知 ROOTFS_TYPE: $ROOTFS_TYPE"; exit 1 ;;
  esac
} >> "$dyn"

[ "$ENABLE_BBR" = true ] && echo 'CONFIG_PACKAGE_kmod-tcp-bbr=y' >> "$dyn"
[ "$ENABLE_OPENCLASH" = true ] && add_if_present luci-app-openclash
for p in $CUSTOM_PACKAGES; do add_if_present "$p"; done

# 合并：后出现的同名项覆盖先出现的；纯注释/空行丢弃
cat "$SEED" <(echo) "$OVERLAY" <(echo) "$dyn" | tr -d '\r' | awk '
  {
    line=$0
    if (match(line,/^CONFIG_[A-Za-z0-9_+.-]+=/)) { key=substr(line,1,RLENGTH-1) }
    else if (line ~ /^# CONFIG_[A-Za-z0-9_+.-]+ is not set[[:space:]]*$/) {
      key=line; sub(/^# /,"",key); sub(/ is not set[[:space:]]*$/,"",key); line="# " key " is not set"
    } else { next }
    if (!(key in idx)) { idx[key]=++n; order[n]=key }
    val[key]=line
  }
  END { for (i=1;i<=n;i++) print val[order[i]] }
' > .config
rm -f "$dyn"
echo "[OK]   .config 已生成：$(wc -l < .config) 行，其中 =y 软件包 $(grep -c '^CONFIG_PACKAGE_.*=y$' .config) 个"
