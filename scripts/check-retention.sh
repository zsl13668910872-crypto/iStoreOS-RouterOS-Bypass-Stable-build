#!/bin/bash
# check-retention.sh SEED      （在源码树根目录运行，已 make defconfig 之后）
# 校验：种子里声明的 =y 软件包（且没被 overlay 取消），经过 defconfig 之后是否还在。
# 若 feeds 没装全 / 依赖不满足，defconfig 会悄悄把它们丢掉，镜像就会“缩水”。
# 环境变量：THRESHOLD(默认10)  MODE(strict|warn，默认strict)  ASSEMBLED(默认 .config.assembled)
set -euo pipefail
SEED="${1:?SEED}"
THRESHOLD="${THRESHOLD:-10}"; MODE="${MODE:-strict}"
ASSEMBLED="${ASSEMBLED:-.config.assembled}"
list() { grep -E '^CONFIG_PACKAGE_[^=]+=y$' "$1" | sed -E 's/^CONFIG_PACKAGE_([^=]+)=y$/\1/' | sort -u; }
ours="$(mktemp)"; comm -12 <(list "$SEED") <(list "$ASSEMBLED") > "$ours"
final="$(mktemp)"; list .config > "$final"
dropped="$(mktemp)"; if [ -f .resolver-dropped ]; then sort -u .resolver-dropped > "$dropped"; else : > "$dropped"; fi
missing="$(comm -23 "$ours" "$final" | comm -23 - "$dropped")"
n=0; [ -n "$missing" ] && n="$(printf '%s\n' "$missing" | wc -l)"
total="$(wc -l < "$ours")"
echo "[INFO] 种子声明的软件包 ${total} 个，defconfig 后缺失 ${n} 个（阈值 ${THRESHOLD}，模式 ${MODE}）"
[ "$n" -gt 0 ] && printf '%s\n' "$missing" | head -n 60 | sed 's/^/       - /'
if [ "$n" -gt "$THRESHOLD" ] && [ "$MODE" = strict ]; then
  echo "::error::有 ${n} 个官方声明的软件包被 defconfig 丢弃，超过阈值 ${THRESHOLD}。通常是 feeds 未装全或依赖缺失。"
  exit 1
fi
[ "$n" -gt 0 ] && echo "::warning::${n} 个种子软件包未进入最终配置（未超过阈值）"
exit 0
