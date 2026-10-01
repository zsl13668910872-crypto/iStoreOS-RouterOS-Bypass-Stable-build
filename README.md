# iStoreOS x86_64 旁路由固件 · GitHub Actions 编译

以 **iStoreOS 官方发布的构建输入**（`config.seed` / `feeds.buildinfo` / `commit.buildinfo`）为基线，叠加你的自定义项，编译 x86_64 EFI 固件。适合 PVE 虚拟机或物理小主机，作为 RouterOS 主路由后面的旁路由，并预装 OpenClash。

> 这套文件基于 ImmortalWrt 工作流的排错经验设计：把“互斥包同文件冲突”“包被 defconfig 悄悄丢掉”“OpenClash 自定义规则格式”等问题放到**编译前**检查，失败就在几分钟内暴露，而不是跑几小时后才在打包阶段报错。

## 仓库结构

```
.
├── .github/workflows/
│   ├── iStoreOS-x86_64-build.yml   ← 编译固件（手动触发）
│   ├── preflight.yml               ← 改动后自动离线预检
│   └── cleanup-runs.yml            ← 手动清理旧运行记录 / Release
├── configs/
│   ├── overlay.config              ← 你的自定义包/选项（叠加在官方种子之上）
│   └── fallback.seed               ← 官方种子下载失败时的最小保底种子
├── files/                          ← 原样并入固件 rootfs 的覆盖层（sysctl、init、uci-defaults、健康检查…）
├── scripts/
│   ├── fetch-official.sh           ← 下载并校验官方 config.seed / feeds / commit
│   ├── assemble-config.sh          ← 种子 < overlay < 输入 合并成 .config
│   ├── resolve-conflicts.sh        ← 处理同文件互斥包（libustream / tcpdump / dnsmasq …）
│   ├── check-retention.sh          ← 防止官方声明的包被 defconfig 丢掉导致镜像缩水
│   └── preflight.py                ← 离线预检（本地也能跑）
├── docs/TROUBLESHOOTING.md
└── README.md
```

## 一、第一次使用

1. 新建仓库（私有即可），把本目录原样提交，保持路径不变。
2. `Settings → Actions → General → Workflow permissions` 选 **Read and write permissions**。
3. `Actions → Preflight → Run workflow`，应显示通过。
4. `Actions → Build iStoreOS x86_64 → Run workflow`。**第一次建议只改 `lan_ipaddr` / `lan_gateway`，其余保持默认。**
5. 完成后在运行页底部 **Artifacts** 下载（保留 14 天）；勾选 `publish_release` 则发布到 Releases。

### 触发参数

| 参数 | 默认 | 说明 |
|---|---|---|
| branch | istoreos-25.12 | 源码分支（另有 istoreos-24.10） |
| config_source | official | `official`=下载官方种子；`repo`=用 `configs/base.seed`（没有则用 fallback.seed） |
| official_base_url | fw.koolcenter.com/iStoreOS/x86_64 | 官方构建输入所在目录 |
| pin_official_commit | 开 | 源码锁定到官方构建提交，与种子严格匹配；不一致直接报错 |
| feeds_mode | official-pinned | 用官方锁定的 feeds；失败自动回退到源码树自带 feeds |
| enable_openclash / openclash_version | 开 / 0.47.156 | 固定版本源码编译 OpenClash |
| preload_openclash_core | 开 | 预置 Mihomo 内核与 Geo 数据库（失败不影响编译） |
| enable_bbr | 开 | 开机检测内核，支持才启用 |
| rootfs_type / rootfs_partsize | squashfs / 4096 | 官方默认 squashfs，可恢复出厂；ext4 更易扩容 |
| custom_packages | 空 | 额外包，空格分隔；源码树里没有的会被跳过并提示 |
| lan_ipaddr / lan_netmask / lan_gateway | 空 | **建议填写**，否则沿用镜像默认 IP，可能与 RouterOS 网段冲突 |
| retention_fail_threshold | 10 | 官方种子里的包被丢弃超过该数就判失败 |

## 二、完全复刻官方功能集合的两种办法

- **默认**：`config_source=official`，工作流自己下载官方 `config.seed`。
- **手动固定**：从官方目录下载 `config.seed`，保存为仓库的 `configs/base.seed`，把 `config_source` 选为 `repo`。适合想长期固定某一版配置、不依赖官方站点是否可用的情况。

## 三、导入 PVE（示例，VM ID 101）

```sh
gunzip -k /root/istoreos-*-x86-64-squashfs-combined-efi.img.gz

qm create 101 --name istoreos --memory 2048 --cores 2 --cpu host \
  --bios ovmf --machine q35 --ostype l26 \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-single --agent enabled=1
qm importdisk 101 /root/istoreos-*-x86-64-squashfs-combined-efi.img local-lvm
qm config 101 | grep unused                 # 记下磁盘名，例如 vm-101-disk-0
qm set 101 --scsi0 local-lvm:vm-101-disk-0 --boot order=scsi0
qm set 101 --efidisk0 local-lvm:1,efitype=4m,pre-enrolled-keys=0
qm start 101
```

存储名和磁盘名以你环境为准。镜像文件名以实际产物为准。

## 四、首次开机后

1. 若构建时填了 `lan_ipaddr`，用它访问；否则用 iStoreOS 默认地址（官方通常是 192.168.100.1，以你实际刷入的版本为准）。
2. LuCI → 服务 → OpenClash：导入 YAML。预置内核成功则无需再下载。
3. 导入 YAML 后固件会自动把 dnsmasq 的 53 端口转到 OpenClash 的 1053；**没有可用 YAML 时不会改 DNS**，避免半装状态断网。
4. 出问题时：`bypass-apply direct` 一键回到直连 DNS 并停止 OpenClash。

常用命令：`bypass-apply apply|direct`、`bypass-health`、`sysctl net.ipv4.tcp_congestion_control`。

## 五、本地预检

```sh
pip install pyyaml
python3 scripts/preflight.py
```

不需要外网和源码。它检查 YAML 与脚本语法，用本机 HTTP 服务器模拟官方目录的 6 种情形，用模拟的 `make defconfig`（硬依赖会覆盖 "is not set"）覆盖 8 种互斥包场景，并把工作流里的 Validate / Fetch / Assemble / Resolve / Inject / Finalize 步骤原样抽出来串联执行。**它不能替代真实编译。**

## 六、维护

- 升级：改 `openclash_version`，或换 `branch` / `official_base_url`，推送后先看 Preflight。
- 想改默认包：编辑 `configs/overlay.config`（写 `# CONFIG_PACKAGE_xxx is not set` 可关闭官方带的包）。
- 想改固件内置行为：编辑 `files/` 下对应脚本。
- 清理空间：`Actions → Cleanup old runs and releases`。
