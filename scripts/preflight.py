#!/usr/bin/env python3
"""
离线预检（不需要外网、不需要 iStoreOS 源码）。用法：  python3 scripts/preflight.py

  1) 静态：工作流 YAML、inputs 声明/引用一致、每个 run 步骤 bash -n、scripts/ 与 files/ 的脚本语法
  2) fetch-official.sh：用本机 HTTP 服务器模拟“官方目录”的 6 种情形（正常/只有 buildinfo/错平台/404/坏提交/feeds 回退）
  3) assemble-config.sh：种子 < overlay < 输入 的覆盖语义、可选包存在性判断
  4) resolve-conflicts.sh + check-retention.sh：用 mock 的 `make defconfig`（select 覆盖 "is not set"）覆盖多种依赖场景
  5) 端到端：把工作流里的 Validate / Fetch / Assemble / Resolve / Inject / Finalize 步骤原样抽出来串联执行

这是“配置阶段”的模拟，不能替代真实编译；真实依赖关系只存在于 iStoreOS 源码树里。
"""
import functools
import http.server
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

try:
    import yaml
except ImportError:
    sys.exit("需要 PyYAML:  pip install pyyaml")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "iStoreOS-x86_64-build.yml")
FAILS = []


def check(ok, msg):
    print(("  [ OK ] " if ok else "  [FAIL] ") + msg)
    if not ok:
        FAILS.append(msg)


def section(t):
    print("\n== %s ==" % t)


MOCK_MAKE = r'''#!/usr/bin/env python3
# mock `make defconfig`：select 覆盖 "is not set"；DEFAULT 包仅在符号缺失时生效；DROPS 模拟依赖缺失被丢弃
import os, re, sys
if sys.argv[1:2] != ["defconfig"]:
    sys.exit(0)
on, off, other = set(), set(), []
for l in open(".config").read().splitlines():
    m = re.match(r"CONFIG_PACKAGE_(\S+)=y$", l)
    if m: on.add(m.group(1)); continue
    m = re.match(r"# CONFIG_PACKAGE_(\S+) is not set$", l)
    if m: off.add(m.group(1)); continue
    if l.strip(): other.append(l)
sel = [tuple(x.split(">")) for x in os.environ.get("SELECTS", "").split(",") if x]
for d in [x for x in os.environ.get("DEFAULTS", "").split(",") if x]:
    if d not in on and d not in off: on.add(d)
changed = True
while changed:
    changed = False
    for a, b in sel:
        if a in on and b not in on:
            on.add(b); off.discard(b); changed = True
for d in [x for x in os.environ.get("DROPS", "").split(",") if x]:
    on.discard(d)
out = other + ["CONFIG_PACKAGE_%s=y" % p for p in sorted(on)] \
    + ["# CONFIG_PACKAGE_%s is not set" % p for p in sorted(off - on)]
open(".config", "w").write("\n".join(out) + "\n")
os.makedirs("tmp", exist_ok=True)
with open("tmp/.config-package.in", "w") as f:
    for a, b in sel:
        f.write("config PACKAGE_%s\n\ttristate \"%s\"\n\tselect PACKAGE_%s\n\n" % (a, a, b))
'''


def sh(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def load_wf():
    return yaml.safe_load(open(WF, encoding="utf-8"))


def steps_by_name(wf):
    return {s.get("name", ""): s for s in wf["jobs"]["build"]["steps"]}


def input_defaults(wf):
    on = wf.get(True) or wf.get("on")
    d = {}
    for k, v in on["workflow_dispatch"]["inputs"].items():
        dv = v.get("default", "")
        d[k] = ("true" if dv else "false") if isinstance(dv, bool) else str(dv)
    return d


def subst(s, inputs):
    return re.sub(r"\$\{\{\s*inputs\.(\w+)\s*\}\}", lambda m: inputs[m.group(1)], str(s))


# ----------------------------------------------------------------------------- 1 静态
def static_checks(wf):
    section("1. 静态检查")
    text = open(WF, encoding="utf-8").read()
    on = wf.get(True) or wf.get("on")
    declared = set(on["workflow_dispatch"]["inputs"])
    used = set(re.findall(r"inputs\.(\w+)", text))
    check(not (used - declared), "所有引用的 inputs 都已声明 (未声明: %s)" % (used - declared or "无"))
    check(not (declared - used), "所有声明的 inputs 都被使用 (未使用: %s)" % (declared - used or "无"))
    check(len(declared) <= 25, "inputs 数量 %d <= 25（GitHub 上限）" % len(declared))
    for k, v in on["workflow_dispatch"]["inputs"].items():
        if v.get("type") == "choice":
            check(v.get("default") in v.get("options", []), "choice 默认值在选项内: %s" % k)
    steps = wf["jobs"]["build"]["steps"]
    for i, s in enumerate(steps):
        nm = s.get("name") or s.get("uses")
        check(("run" in s) != ("uses" in s), "step %02d 恰有 run 或 uses: %s" % (i, nm))
        if "run" in s:
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
                f.write(re.sub(r"\$\{\{.*?\}\}", "X", s["run"]))
            r = sh(["bash", "-n", f.name])
            os.unlink(f.name)
            check(r.returncode == 0, "bash -n  %s %s" % (nm, r.stderr.strip()[:120]))
    check(all("@" in s["uses"] for s in steps if "uses" in s), "所有 action 都带版本号")
    check(all(s.get("working-directory", "istoreos") == "istoreos" for s in steps), "working-directory 仅为 istoreos")
    for f in sorted(os.listdir(os.path.join(ROOT, "scripts"))):
        if f.endswith(".sh"):
            r = sh(["bash", "-n", os.path.join(ROOT, "scripts", f)])
            check(r.returncode == 0, "bash -n  scripts/%s %s" % (f, r.stderr.strip()[:100]))
    shbin = shutil.which("dash") or shutil.which("sh")
    for rel in ("usr/bin/bypass-apply", "usr/bin/bypass-health", "etc/uci-defaults/99-istoreos-bypass"):
        r = sh([shbin, "-n", os.path.join(ROOT, "files", rel)])
        check(r.returncode == 0, "sh -n  files/%s %s" % (rel, r.stderr.strip()[:100]))
    body = "".join(open(os.path.join(ROOT, "files/etc/init.d/bypass-tune")).readlines()[1:])
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(body)
    r = sh([shbin, "-n", f.name]); os.unlink(f.name)
    check(r.returncode == 0, "sh -n  files/etc/init.d/bypass-tune")
    rules = open(os.path.join(ROOT, "files/etc/openclash/custom/openclash_custom_rules.list")).read().splitlines()
    check(rules[0] == "rules:" and all(l.startswith("- DOMAIN") for l in rules[1:]), "custom rules 以 rules: 开头且格式正确")
    sysctl = [l for l in open(os.path.join(ROOT, "files/etc/sysctl.d/98-bypass-tune.conf")).read().splitlines()
              if l.strip() and not l.startswith("#")]
    check(all(re.match(r"^[a-z0-9_.]+ = .+$", l) for l in sysctl), "sysctl 文件每行均为 key = value")
    for seedf in ("configs/overlay.config", "configs/fallback.seed"):
        bad = [l for l in open(os.path.join(ROOT, seedf)).read().splitlines()
               if l.strip() and not l.startswith("#") and not re.match(r"^CONFIG_[A-Za-z0-9_+.-]+=.+$", l)]
        check(not bad, "%s 每个非注释行都是 CONFIG_x=y 形式 %s" % (seedf, bad[:2]))
        bad2 = [l for l in open(os.path.join(ROOT, seedf)).read().splitlines()
                if l.startswith("# CONFIG_") and not l.endswith(" is not set")]
        check(not bad2, "%s 里没有被写坏的 '# CONFIG_' 行" % seedf)


# ----------------------------------------------------------------------------- 2 fetch
class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory):
    handler = functools.partial(QuietHandler, directory=directory)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


GOOD_SEED = "CONFIG_TARGET_x86=y\nCONFIG_TARGET_x86_64=y\nCONFIG_TARGET_x86_64_DEVICE_generic=y\nCONFIG_PACKAGE_foo=y\n"
GOOD_FEEDS = "src-git-full packages https://example.invalid/packages.git^abcdef1\nsrc-git-full luci https://example.invalid/luci.git^1234567\n"


def fetch_case(name, files, expect):
    base = tempfile.mkdtemp(prefix="fetch-")
    www = os.path.join(base, "www"); out = os.path.join(base, "out"); os.makedirs(www)
    for fn, content in files.items():
        open(os.path.join(www, fn), "w").write(content)
    srv = serve(www)
    try:
        url = "http://127.0.0.1:%d/" % srv.server_address[1]
        r = sh(["bash", os.path.join(ROOT, "scripts/fetch-official.sh"), url, out])
        st = {}
        p = os.path.join(out, "status.env")
        if os.path.exists(p):
            st = dict(l.split("=", 1) for l in open(p).read().splitlines() if "=" in l)
        check(r.returncode == 0, "%s: 脚本退出码 0" % name)
        for k, v in expect.items():
            check(st.get(k) == v, "%s: %s = %r (实际 %r)" % (name, k, v, st.get(k)))
        return out
    finally:
        srv.shutdown()
        shutil.rmtree(base, ignore_errors=True)


def fetch_tests():
    section("2. fetch-official.sh（本机 HTTP 模拟官方目录）")
    fetch_case("A 正常", {"config.seed": GOOD_SEED, "feeds.buildinfo": GOOD_FEEDS, "commit.buildinfo": "d3adb33f\n"},
               {"OFFICIAL_SEED": "1", "OFFICIAL_SEED_NAME": "config.seed", "OFFICIAL_FEEDS": "1",
                "OFFICIAL_FEEDS_NAME": "feeds.buildinfo", "OFFICIAL_COMMIT": "d3adb33f"})
    fetch_case("B 只有 config.buildinfo", {"config.buildinfo": GOOD_SEED, "feeds.conf": GOOD_FEEDS},
               {"OFFICIAL_SEED": "1", "OFFICIAL_SEED_NAME": "config.buildinfo", "OFFICIAL_FEEDS_NAME": "feeds.conf",
                "OFFICIAL_COMMIT": ""})
    fetch_case("C 种子属于别的平台", {"config.seed": "CONFIG_TARGET_rockchip=y\nCONFIG_TARGET_x86=y\n"},
               {"OFFICIAL_SEED": "0"})
    fetch_case("D 全部 404", {}, {"OFFICIAL_SEED": "0", "OFFICIAL_FEEDS": "0", "OFFICIAL_COMMIT": ""})
    fetch_case("E 提交内容非法", {"config.seed": GOOD_SEED, "commit.buildinfo": "not a hash!\n"},
               {"OFFICIAL_SEED": "1", "OFFICIAL_COMMIT": ""})
    fetch_case("F feeds 内容非法", {"config.seed": GOOD_SEED, "feeds.buildinfo": "<html>oops</html>"},
               {"OFFICIAL_SEED": "1", "OFFICIAL_FEEDS": "0"})


# ----------------------------------------------------------------------------- tree helpers
def make_tree(base, extra_pkgs=()):
    tree = os.path.join(base, "istoreos")
    os.makedirs(os.path.join(tree, "bin_mock"), exist_ok=True)
    mk = os.path.join(tree, "bin_mock", "make")
    open(mk, "w").write(MOCK_MAKE); os.chmod(mk, 0o755)
    for d in ("package/openclash/luci-app-openclash", "feeds/packages/utils/qemu-ga", "feeds/luci/applications/luci-app-ttyd"):
        os.makedirs(os.path.join(tree, d), exist_ok=True)
    open(os.path.join(tree, "package/openclash/luci-app-openclash/Makefile"), "w").write("PKG_VERSION:=0.47.156\n")
    for p in extra_pkgs:
        os.makedirs(os.path.join(tree, "feeds/packages/utils", p), exist_ok=True)
    return tree


def tree_env(tree, **kw):
    env = dict(os.environ, PATH=os.path.join(tree, "bin_mock") + os.pathsep + os.environ["PATH"])
    env.update(kw)
    return env


# ----------------------------------------------------------------------------- 3 assemble
def assemble_tests():
    section("3. assemble-config.sh")
    base = tempfile.mkdtemp(prefix="asm-")
    try:
        tree = make_tree(base)
        seed = os.path.join(base, "seed")
        open(seed, "w").write("# comment\nCONFIG_TARGET_x86=y\nCONFIG_TARGET_ROOTFS_PARTSIZE=104\nCONFIG_PACKAGE_foo=y\n"
                              "# CONFIG_PACKAGE_bar is not set\nCONFIG_TARGET_ROOTFS_SQUASHFS=y\n\r\nCONFIG_PACKAGE_crlf=y\r\n")
        ovl = os.path.join(base, "ovl")
        open(ovl, "w").write("CONFIG_PACKAGE_bar=y\n# CONFIG_PACKAGE_foo is not set\n")
        env = tree_env(tree, ROOTFS_PARTSIZE="4096", ROOTFS_TYPE="ext4", ENABLE_BBR="true", ENABLE_OPENCLASH="true",
                       CUSTOM_PACKAGES="luci-app-ttyd not-exist-pkg")
        r = sh(["bash", os.path.join(ROOT, "scripts/assemble-config.sh"), seed, ovl], cwd=tree, env=env)
        check(r.returncode == 0, "assemble 退出码 0 %s" % r.stderr[-200:])
        cfg = open(os.path.join(tree, ".config")).read().splitlines()
        has = lambda l: l in cfg
        check(cfg.count("CONFIG_TARGET_ROOTFS_PARTSIZE=4096") == 1 and not any("=104" in l for l in cfg), "输入的分区大小覆盖种子且只出现一次")
        check(has("# CONFIG_PACKAGE_foo is not set") and not has("CONFIG_PACKAGE_foo=y"), "overlay 可以取消种子里的包")
        check(has("CONFIG_PACKAGE_bar=y") and not has("# CONFIG_PACKAGE_bar is not set"), "overlay 可以启用种子里关闭的包")
        check(has("CONFIG_TARGET_ROOTFS_EXT4FS=y") and has("# CONFIG_TARGET_ROOTFS_SQUASHFS is not set"), "rootfs_type=ext4 生效")
        check(has("CONFIG_PACKAGE_kmod-tcp-bbr=y"), "ENABLE_BBR 生效")
        check(has("CONFIG_PACKAGE_luci-app-openclash=y"), "OpenClash 源码存在时启用")
        check(has("CONFIG_PACKAGE_luci-app-ttyd=y") and not has("CONFIG_PACKAGE_not-exist-pkg=y"), "自定义包：存在才启用，不存在跳过")
        check(has("CONFIG_PACKAGE_crlf=y"), "CRLF 行被正确处理")
        keys = [re.sub(r"^# ", "", re.sub(r"( is not set|=.*)$", "", l)) for l in cfg]
        check(len(keys) == len(set(keys)), "同一配置项只出现一次")
        check(not any(l.startswith("#") and not l.endswith(" is not set") for l in cfg), "无多余注释行")
        # 关闭 OpenClash / 源码里没有 OpenClash
        env2 = tree_env(tree, ENABLE_OPENCLASH="false", ENABLE_BBR="false")
        r = sh(["bash", os.path.join(ROOT, "scripts/assemble-config.sh"), seed, ovl], cwd=tree, env=env2)
        cfg2 = open(os.path.join(tree, ".config")).read()
        check("luci-app-openclash" not in cfg2 and "tcp-bbr" not in cfg2, "关闭开关后不再写入 OpenClash/BBR")
        r = sh(["bash", os.path.join(ROOT, "scripts/assemble-config.sh"), "/nonexistent", ovl], cwd=tree, env=env)
        check(r.returncode != 0, "种子缺失时失败")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ----------------------------------------------------------------------------- 4 resolver
def resolver_case(name, seed_pkgs, overlay_pkgs, selects, defaults, want_rc, must, mustnot, drops=""):
    print("-- %s" % name)
    base = tempfile.mkdtemp(prefix="res-")
    try:
        tree = make_tree(base)
        seed = os.path.join(base, "seed"); ovl = os.path.join(base, "ovl")
        open(seed, "w").write("CONFIG_TARGET_x86=y\nCONFIG_TARGET_x86_64=y\n" + "".join("CONFIG_PACKAGE_%s=y\n" % p for p in seed_pkgs))
        open(ovl, "w").write("".join("CONFIG_PACKAGE_%s=y\n" % p for p in overlay_pkgs))
        env = tree_env(tree, SELECTS=selects, DEFAULTS=defaults, DROPS=drops, ROOTFS_PARTSIZE="4096",
                       ENABLE_BBR="false", ENABLE_OPENCLASH="false")
        r = sh(["bash", os.path.join(ROOT, "scripts/assemble-config.sh"), seed, ovl], cwd=tree, env=env)
        shutil.copy(os.path.join(tree, ".config"), os.path.join(tree, ".config.assembled"))
        sh(["make", "defconfig"], cwd=tree, env=env)
        r = sh(["bash", os.path.join(ROOT, "scripts/resolve-conflicts.sh")], cwd=tree, env=env)
        check(r.returncode == want_rc, "resolver 退出码 = %d (期望 %d)" % (r.returncode, want_rc))
        if want_rc == 0:
            got = {m.group(1) for l in open(os.path.join(tree, ".config")).read().splitlines()
                   for m in [re.match(r"^CONFIG_PACKAGE_(\S+)=y$", l)] if m}
            check(set(must) <= got, "保留 %s" % sorted(must))
            check(not (set(mustnot) & got), "剔除 %s (实际含 %s)" % (sorted(mustnot), sorted(set(mustnot) & got) or "无"))
            c = sh(["bash", os.path.join(ROOT, "scripts/resolve-conflicts.sh"), "--check-only"], cwd=tree, env=env)
            check(c.returncode == 0, "--check-only 通过")
        else:
            check("<-" in r.stdout, "失败时列出了是谁选中了冲突包")
        return tree, env
    finally:
        shutil.rmtree(base, ignore_errors=True)


def resolver_tests():
    section("4. resolve-conflicts.sh / check-retention.sh（mock make）")
    resolver_case("R0 无冲突", ["dnsmasq-full"], ["tcpdump"], "", "libustream-mbedtls", 0,
                  {"dnsmasq-full", "tcpdump", "libustream-mbedtls"}, {"tcpdump-mini"})
    resolver_case("R1 默认包叠加（你遇到的两个报错形态）", [], ["tcpdump"], "", "libustream-mbedtls,libustream-openssl,tcpdump-mini", 0,
                  {"libustream-mbedtls", "tcpdump"}, {"libustream-openssl", "tcpdump-mini"})
    resolver_case("R2 种子显式选 openssl，默认带 mbedtls", ["libustream-openssl"], [], "", "libustream-mbedtls", 0,
                  {"libustream-openssl"}, {"libustream-mbedtls"})
    resolver_case("R3 openssl 被硬依赖（种子选 mbedtls）", ["libustream-mbedtls", "luci-app-x"], [], "luci-app-x>libustream-openssl", "", 0,
                  {"libustream-openssl"}, {"libustream-mbedtls"})
    resolver_case("R4 tcpdump-mini 被硬依赖", [], ["tcpdump", "luci-app-y"], "luci-app-y>tcpdump-mini", "", 0,
                  {"tcpdump-mini"}, {"tcpdump"})
    resolver_case("R5 三个 ustream 库同时出现", [], [], "", "libustream-mbedtls,libustream-openssl,libustream-wolfssl", 0,
                  {"libustream-mbedtls"}, {"libustream-openssl", "libustream-wolfssl"})
    resolver_case("R6 dnsmasq 家族", ["dnsmasq-full"], [], "", "dnsmasq", 0, {"dnsmasq-full"}, {"dnsmasq"})
    resolver_case("R7 两个都被硬依赖（应在编译前失败）", ["a", "b"], [], "a>libustream-openssl,b>libustream-mbedtls", "", 1, [], [])

    print("-- 保留检查 (check-retention)")
    base = tempfile.mkdtemp(prefix="ret-")
    try:
        pk = ["p%02d" % i for i in range(30)]
        tree = make_tree(base)
        seed = os.path.join(base, "seed"); ovl = os.path.join(base, "ovl")
        open(seed, "w").write("CONFIG_TARGET_x86=y\nCONFIG_TARGET_x86_64=y\n" + "".join("CONFIG_PACKAGE_%s=y\n" % p for p in pk))
        open(ovl, "w").write("# CONFIG_PACKAGE_p29 is not set\n")
        drops = ",".join(pk[:12])
        env = tree_env(tree, DROPS=drops, ROOTFS_PARTSIZE="4096", ENABLE_BBR="false", ENABLE_OPENCLASH="false")
        sh(["bash", os.path.join(ROOT, "scripts/assemble-config.sh"), seed, ovl], cwd=tree, env=env)
        shutil.copy(os.path.join(tree, ".config"), os.path.join(tree, ".config.assembled"))
        sh(["make", "defconfig"], cwd=tree, env=env)
        for th, mode, want in (("10", "strict", 1), ("20", "strict", 0), ("0", "warn", 0)):
            r = sh(["bash", os.path.join(ROOT, "scripts/check-retention.sh"), seed], cwd=tree,
                   env=dict(env, THRESHOLD=th, MODE=mode))
            check(r.returncode == want, "丢弃 12 个包，阈值 %s 模式 %s -> 退出码 %d (期望 %d)" % (th, mode, r.returncode, want))
        r = sh(["bash", os.path.join(ROOT, "scripts/check-retention.sh"), seed], cwd=tree, env=dict(env, THRESHOLD="10"))
        check("缺失 12 个" in r.stdout, "被 overlay 取消的包(p29)不算“丢失”：报告 12 个而非 13 个")
        open(os.path.join(tree, ".resolver-dropped"), "w").write("\n".join(pk[:5]) + "\n")
        r = sh(["bash", os.path.join(ROOT, "scripts/check-retention.sh"), seed], cwd=tree, env=dict(env, THRESHOLD="10"))
        check("缺失 7 个" in r.stdout, "resolver 主动关闭的包不计入丢失")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ----------------------------------------------------------------------------- 5 e2e
def run_step(steps, name, cwd, env, inputs, ghenv):
    s = steps[name]
    e = dict(env)
    for k, v in (s.get("env") or {}).items():
        e[k] = subst(v, inputs)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(s["run"])
    r = sh(["bash", "--noprofile", "--norc", "-eo", "pipefail", f.name], cwd=cwd, env=e)
    os.unlink(f.name)
    if os.path.exists(ghenv):
        for l in open(ghenv).read().splitlines():
            if "=" in l:
                k, v = l.split("=", 1); env[k] = v
    return r


def e2e_tests(wf):
    section("5. 工作流步骤端到端（原样抽取 YAML 里的 run 脚本）")
    steps = steps_by_name(wf)
    base_inputs = input_defaults(wf)

    # 5a Validate inputs 的拒绝/通过
    env0 = dict(os.environ)
    ghenv0 = os.path.join(tempfile.mkdtemp(), "ghenv")
    cases = [({}, True, "默认值通过"),
             ({"rootfs_partsize": "abc"}, False, "非法分区大小被拒"),
             ({"rootfs_partsize": "100"}, False, "过小分区被拒"),
             ({"official_base_url": "http://x.example"}, False, "非 https 的官方 URL 被拒"),
             ({"lan_ipaddr": "1.2.3"}, False, "非法 IP 被拒"),
             ({"custom_packages": "a; rm -rf /"}, False, "含 shell 元字符的包名被拒"),
             ({"openclash_version": "0.47 156"}, False, "含空格的版本被拒"),
             ({"lan_ipaddr": "192.168.1.2", "lan_gateway": "192.168.1.1"}, True, "合法静态网络参数通过")]
    for ov, ok, msg in cases:
        inp = dict(base_inputs, **ov)
        r = run_step(steps, "Validate inputs", ROOT, dict(env0), inp, ghenv0)
        check((r.returncode == 0) == ok, "Validate inputs: " + msg)

    # 5b 全流程
    ws = tempfile.mkdtemp(prefix="ws-")
    try:
        for d in ("scripts", "files", "configs"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(ws, d))
        tree = make_tree(ws)
        shutil.rmtree(os.path.join(tree, "feeds/luci/applications/luci-app-ttyd"), ignore_errors=True)
        www = os.path.join(ws, "www"); os.makedirs(www)
        seed_pk = "".join("CONFIG_PACKAGE_s%02d=y\n" % i for i in range(20)) + "CONFIG_PACKAGE_libustream-openssl=y\n"
        open(os.path.join(www, "config.seed"), "w").write(GOOD_SEED + seed_pk)
        open(os.path.join(www, "feeds.buildinfo"), "w").write(GOOD_FEEDS)
        open(os.path.join(www, "commit.buildinfo"), "w").write("0123abcd\n")
        srv = serve(www)
        ghenv = os.path.join(ws, "ghenv"); open(ghenv, "w").close()
        env = tree_env(tree, GITHUB_WORKSPACE=ws, GITHUB_ENV=ghenv, SRC_DIR="istoreos", INPUTS_DIR="build-inputs",
                       SELECTS="", DEFAULTS="libustream-mbedtls,tcpdump-mini")
        inp = dict(base_inputs, official_base_url="http://127.0.0.1:%d" % srv.server_address[1],
                   custom_packages="qemu-ga nothing-here", lan_ipaddr="192.168.1.2", lan_gateway="192.168.1.1")
        try:
            r = run_step(steps, "Fetch official build inputs", ws, env, inp, ghenv)
            check(r.returncode == 0 and env.get("OFFICIAL_SEED") == "1" and env.get("OFFICIAL_COMMIT") == "0123abcd",
                  "Fetch：官方输入就绪并写入 GITHUB_ENV %s" % r.stderr[-150:])
            for nm in ("Assemble .config (seed + overlay + inputs)", "Resolve package file conflicts and check retention",
                       "Inject rootfs overlay", "Finalize config and verify"):
                r = run_step(steps, nm, tree, env, inp, ghenv)
                check(r.returncode == 0, "%s 退出码 0 %s" % (nm, (r.stdout + r.stderr)[-300:] if r.returncode else ""))
            cfg = open(os.path.join(tree, ".config")).read().splitlines()
            pk = {m.group(1) for l in cfg for m in [re.match(r"^CONFIG_PACKAGE_(\S+)=y$", l)] if m}
            check("libustream-openssl" in pk and "libustream-mbedtls" not in pk, "e2e：种子选择的 openssl 被保留，默认 mbedtls 被剔除")
            check("tcpdump" in pk and "tcpdump-mini" not in pk, "e2e：tcpdump 与 tcpdump-mini 不再同时存在")
            check({"luci-app-openclash", "kmod-tcp-bbr", "qemu-ga"} <= pk, "e2e：OpenClash / BBR / 自定义包进入配置")
            f = lambda *a: os.path.join(tree, "files", *a)
            check(os.path.isfile(f("etc/routeros-bypass/bbr")), "e2e：BBR 标记文件已生成")
            lan = open(f("etc/routeros-bypass/lan.conf")).read()
            check("LAN_IP='192.168.1.2'" in lan and "LAN_GW='192.168.1.1'" in lan, "e2e：LAN 参数写入 lan.conf")
            check(os.access(f("usr/bin/bypass-apply"), os.X_OK) and os.access(f("etc/uci-defaults/99-istoreos-bypass"), os.X_OK),
                  "e2e：overlay 脚本可执行")
            # 模拟“官方 feeds 缺失导致包被丢弃”应当让 Assemble 之后的检查失败
            env2 = dict(env, DROPS=",".join("s%02d" % i for i in range(15)))
            r = run_step(steps, "Assemble .config (seed + overlay + inputs)", tree, env2, inp, ghenv)
            r = run_step(steps, "Resolve package file conflicts and check retention", tree, env2, inp, ghenv)
            check(r.returncode != 0, "e2e：官方包被大量丢弃(15>10)时，保留检查使构建在编译前失败")
        finally:
            srv.shutdown()
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def main():
    if not os.path.isfile(WF):
        sys.exit("找不到工作流: %s" % WF)
    wf = load_wf()
    print("工作流: %s" % WF)
    static_checks(wf)
    if FAILS:
        print("\n静态检查失败，后续测试跳过"); sys.exit(1)
    fetch_tests()
    assemble_tests()
    resolver_tests()
    e2e_tests(wf)
    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项:\n  - %s" % (len(FAILS), "\n  - ".join(FAILS))))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
