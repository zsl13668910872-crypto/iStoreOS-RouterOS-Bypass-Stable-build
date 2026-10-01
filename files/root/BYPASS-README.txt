iStoreOS x86_64 旁路由固件（GitHub Actions 构建）

- IPv4-only 基线；本机 DHCP/RA/DHCPv6 关闭（由 RouterOS 主路由负责）。
- OpenClash 为主控：dnsmasq :53 -> 127.0.0.1:1053（仅在存在可用 YAML 后才改 DNS，且 cachesize=0）。
- Flow Offloading 关闭；BBR（构建时启用且内核支持）；TCP/UDP 缓冲 16MB；conntrack 超时收紧。
- OK影视直连规则：hidns.vip / cmliussss.com / 090227.xyz / tvbbox.pgjgr.eu.cc。
- 健康检查：连续 2 次本地 DNS 探测失败才重启 OpenClash，15 分钟冷却。

常用命令：
  bypass-apply apply     重新应用基线
  bypass-apply direct    回滚：恢复直连 DNS 并停止 OpenClash
  bypass-health          手动执行一次健康检查（日志 /var/log/bypass-health.log）
  sysctl net.ipv4.tcp_congestion_control
