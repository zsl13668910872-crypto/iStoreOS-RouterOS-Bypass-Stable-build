#!/bin/bash
# fetch-official.sh BASE_URL OUT_DIR
# 从 iStoreOS 官方固件目录下载构建输入（种子配置 / 锁定版本的 feeds / 源码提交），并做合法性校验。
#   官方目录示例： https://fw.koolcenter.com/iStoreOS/x86_64/
#   其中的 config.seed / config.buildinfo、feeds.buildinfo / feeds.conf、commit.buildinfo 由官方构建产出。
# 输出（OUT_DIR）： base.seed  feeds.pinned  commit.txt  status.env
# 任何一步失败都不会让脚本退出非 0：由调用方根据 status.env 决定回退策略。
set -u
BASE="${1:?用法: fetch-official.sh BASE_URL OUT_DIR}"
OUT="${2:?用法: fetch-official.sh BASE_URL OUT_DIR}"
BASE="${BASE%/}"
mkdir -p "$OUT"
rm -f "$OUT"/base.seed "$OUT"/feeds.pinned "$OUT"/commit.txt "$OUT"/status.env

fetch() { # 远端文件名 本地路径
  curl -fsSL --retry 3 --retry-delay 3 --connect-timeout 15 --max-time 90 \
       -o "$2" "$BASE/$1" 2>/dev/null
}

valid_seed() { # 必须是 x86_64 目标，且不能是别的平台
  local f="$1"
  [ -s "$f" ] || return 1
  grep -q '^CONFIG_TARGET_x86=y' "$f" || return 1
  grep -Eq '^CONFIG_TARGET_x86_64(=y|_DEVICE_)' "$f" || return 1
  if grep -Eq '^CONFIG_TARGET_(rockchip|armsr|mediatek|ramips|mvebu|sunxi)=y' "$f"; then return 1; fi
  return 0
}

seed_ok=0; seed_name=""
for n in config.seed config.buildinfo; do
  tmp="$OUT/.dl.$$"
  if fetch "$n" "$tmp"; then
    if valid_seed "$tmp"; then
      mv "$tmp" "$OUT/base.seed"; seed_ok=1; seed_name="$n"
      echo "[OK]   种子配置 <- $n ($(wc -l < "$OUT/base.seed") 行)"
      break
    else
      echo "[WARN] $n 已下载但不是 x86_64 配置，忽略"
    fi
  else
    echo "[WARN] 无法下载 $n"
  fi
  rm -f "$tmp"
done

feeds_ok=0; feeds_name=""
for n in feeds.buildinfo feeds.conf; do
  tmp="$OUT/.dl.$$"
  if fetch "$n" "$tmp" && grep -Eq '^src-(git|git-full|link|svn)' "$tmp"; then
    mv "$tmp" "$OUT/feeds.pinned"; feeds_ok=1; feeds_name="$n"
    echo "[OK]   feeds 列表 <- $n ($(grep -c '^src-' "$OUT/feeds.pinned") 个源)"
    break
  fi
  rm -f "$tmp"
done
[ "$feeds_ok" -eq 1 ] || echo "[WARN] 未取得官方 feeds 列表，将使用源码树自带的 feeds.conf.default"

commit=""
tmp="$OUT/.dl.$$"
if fetch commit.buildinfo "$tmp"; then
  c="$(tr -d '[:space:]' < "$tmp")"
  if printf '%s' "$c" | grep -Eq '^[0-9a-f]{7,40}$'; then
    commit="$c"; printf '%s\n' "$commit" > "$OUT/commit.txt"
    echo "[OK]   官方构建提交 <- commit.buildinfo ($commit)"
  else
    echo "[WARN] commit.buildinfo 内容不是提交哈希，忽略"
  fi
else
  echo "[WARN] 无法下载 commit.buildinfo"
fi
rm -f "$tmp"

{
  echo "OFFICIAL_SEED=$seed_ok"
  echo "OFFICIAL_SEED_NAME=$seed_name"
  echo "OFFICIAL_FEEDS=$feeds_ok"
  echo "OFFICIAL_FEEDS_NAME=$feeds_name"
  echo "OFFICIAL_COMMIT=$commit"
} > "$OUT/status.env"
exit 0
