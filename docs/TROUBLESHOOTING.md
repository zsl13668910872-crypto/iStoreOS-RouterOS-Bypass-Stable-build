# 编译失败排查

先下载失败运行的 **build-logs** 工件（含 `build.log`、`build-retry.log`、`.config`、`.config.assembled`、`status.env`）。

| 现象 / 日志关键字 | 含义 | 处理 |
|---|---|---|
| `官方 commit.buildinfo (...) 不在分支 ... 的历史中` | 官方目录里的 config.seed 属于别的分支（例如目录当前是 25.12，你选了 24.10） | 选对 `branch` 或 `official_base_url`；或关掉 `pin_official_commit`（种子与源码可能不严格匹配） |
| `未能取得有效的官方 config.seed` | 官方站点不可达或文件不是 x86_64 配置，已回退到 `fallback.seed` | 稍后重试；或把官方 config.seed 存为 `configs/base.seed` 后选 `config_source=repo` |
| `官方锁定的 feeds 拉取失败` | 锁定的提交已被上游清理，已自动回退到源码树自带 feeds | 属正常回退；若随后 `保留检查` 失败，说明包集合与种子不匹配，请改 `feeds_mode=branch-head` 复查 |
| `有 N 个官方声明的软件包被 defconfig 丢弃` | feeds 没装全或依赖缺失，镜像会缩水 | 看日志列出的包名；多半是 feeds 失败。可临时调高 `retention_fail_threshold`，但不建议 |
| `互斥包同时启用` / `均被硬依赖选中，无法自动解决` | 两个会写同一文件的包都被别的包硬依赖 | 看日志里 `<-` 列出的上游包；用 `# CONFIG_PACKAGE_上游包 is not set`（写进 `configs/overlay.config`）关掉它，或改选另一个 |
| `check_data_file_clashes` | 打包阶段发现文件重名（通常已被前置检查拦截） | 日志会自动抽取上下文；把该段发来分析 |
| `[WARN] 源码树中不存在，已跳过: xxx` | 你在 `custom_packages` 写的包名在该分支源码里没有 | 核对包名；这是提示，不影响构建 |
| `No space left on device` | runner 磁盘不足 | 去掉大型 `custom_packages`；确认 “Free disk space” 步骤成功 |
| 并行构建失败后出现 `单线程重试` | 为定位第一个真正的错误 | 在 `build-retry.log` 里搜 `Error` / `ERROR` 的第一处 |
| 超时（350 分钟） | 官方完整软件包集合体量大 | 再跑一次（`dl` 缓存会生效）；或精简 overlay / 自定义包 |
| `Mihomo core not preloaded` | 预置内核下载失败（不影响编译） | 开机后在 OpenClash 页面手动更新内核 |

## 想查“是谁选了某个包”

编译前的冲突处理步骤会读取 `tmp/.config-package.in` 并打印 `<- 上游包名`。本地有源码树时：

```sh
awk -v t="PACKAGE_libustream-openssl" '/^config /{cur=$2} $0 ~ ("select " t "($|[ \t])"){print cur}' tmp/.config-package.in
```
