#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冬瓜HAOS IP 寻踪转发器

监听 NAS 固定端口（默认 36123），为应用中心/桌面**双图标**提供固定入口：
  · GET /...         → 302 到本机 36124 管理后台反代（去「浏览器版本过低」横幅）
  · GET /ha...       → 302 到 Home Assistant http://<VM_IP>:8123/...
  · GET /power/start → 302 回本站 "/"（开机接口只认 POST，见 do_GET 注释）
  · POST /power/start→ 关机页的一键开机，回页显式刷新回 "/"
  · GET /api/status  → JSON 状态
  · GET /net         → 网络修复页（经 libvirt 串口，无需虚拟机有 IP）

跳转前提：解析到的地址必须探活通过（:8124 或 :8123 任一 TCP 可连），否则留在
寻踪页继续轮询，避免 ARP 残留/DHCP 旧租约把浏览器甩到一个不通的地址。

虚拟机 IP 多级发现链（由后台线程单飞维护，HTTP 请求只读缓存）：
  1. libvirt XML 的 MAC → /proc/net/arp 反查（最准，且要求表项是 complete）
  2. VNC 横幅 OCR 直读：HAOS 控制台横幅上就印着 `Meta: http://IP:8124`，
     零网络依赖，虚拟机一起来就能读到（TCP 验证后才采信，横幅可能是陈旧值）
  3. mDNS：avahi-resolve homeassistant.local，TCP 验证 + MAC 守门
  4. virsh domifaddr --source arp/lease/agent
  5. 对本机 LAN 子网并行 ping 扫描逼出 ARP，再按 MAC 反查
  6. 端口补齐：ARP 活跃主机里探 TCP :8124（冬瓜管理后台特征端口），排除 NAS
     自身，并按 MAC 守门排除局域网里其它 HA/ESPHome 设备

发现链全空（现实中真实存在：链路通，但 LAN 上没有任何 DHCPv4 服务器应答）时，
入口页不只让用户干等，提供三件事，全部经 libvirt 串口在虚拟机里执行：
  · POST /net/dhcp       → 让 NetworkManager 重新走一遍 DHCP
  · POST /net/restart    → 重启虚拟机网络
  · POST /net/static     → 给网卡设静态 IPv4（含恢复自动获取）
  · POST /net/manual     → 只记一个手动跳转地址（虚拟机在别的网段时用）
"""
import http.client
import http.server
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

VM_NAME = os.environ.get("HAOS_VM_NAME", "haos")
PORT = int(os.environ.get("HAOS_WEB_PORT", "36123"))
ADMIN_PORT = int(os.environ.get("HAOS_ADMIN_PORT", "8124"))       # 冬瓜管理后台
HA_PORT = int(os.environ.get("HAOS_HA_PORT", "8123"))             # Home Assistant
ADMIN_PROXY_PORT = int(os.environ.get("HAOS_ADMIN_PROXY_PORT", "36124"))
MDNS_NAMES = os.environ.get("HAOS_MDNS_NAMES", "homeassistant.local").split()
CACHE_TTL = 20
SWEEP_COOLDOWN = 45
# 刚点过开机的这段时间里，端口没通多半是系统还在起，不判成死地址。
# 冬瓜比 iStoreOS 慢得多（HA 首启要从上游拉镜像），宽限期给足。
BOOT_GRACE = int(os.environ.get("HAOS_BOOT_GRACE", "180"))
BOOT_HOLD = int(os.environ.get("HAOS_BOOT_HOLD", "90"))
# 与 cmd/main 共用的两个状态文件：开机请求登记 / 用户主动停用标记
START_REQ = "/tmp/haos-vm-start.req"
STOP_MARK = "/tmp/dongguaha-user-stopped"
# 应用中心里本应用的身份：入口页叫醒虚拟机后要拿它把平台状态一起带起来，
# 否则平台一直挂着「已停用」，界面上的停用会变成空操作（见 platform_sync_start）
APP_ID = os.environ.get("HAOS_APP_ID", "com.dongguaha.vm")
APPCENTER_CLI = "/usr/local/bin/appcenter-cli"
PLAT_SYNC_COOLDOWN = 60     # 秒：冷却期内不重复敲平台
_plat_lock = threading.Lock()
_plat_sync = {"ts": 0.0}
# 入口页手动记下的跳转地址（放在应用共享目录，重装不丢）：
# 发现链全空时的兜底，比如虚拟机被挪到别的网段。
MANUAL_IP_FILE = "/vol1/@appshare/dongguaha/manual-ip"

# 冬瓜管理后台前端的“浏览器版本过低”UA 检测（minified），反代时改写为恒通过。
UA_CHECK_RE = re.compile(
    rb"return!\(t&&parseInt\(t\[1\],10\)<\d+\|\|n&&parseInt\(n\[1\],10\)<\d+\|"
    rb"\|r&&parseInt\(r\[1\]\|\|r\[2\],10\)<\d+\|\|o&&parseInt\(o\[1\],10\)<\d+\)"
)

# 冬瓜面板的「HA Login Page / TTYD」等按钮把地址拼成 http://<当前主机名>:<兄弟端口>
# （HA 端口来自其后端 /v1/os/state 的 port 字段，实测 80；ttyd 固定 7681）。经本反代
# 访问时主机名是 NAS，于是 HA 按钮落到 NAS:80（＝飞牛 web 登录页）、ttyd 落到
# NAS:7681（无人监听）。对策：往 HTML 里注入一段 shim，把「与本页同主机、但端口不是
# 本反代端口」的链接改写到虚拟机真实 IP；面板自身（同端口）继续走反代。
LINKFIX_PATH = "/_dongguaha/linkfix.js"
LINKFIX_NOOP = "/* 冬瓜HAOS：虚拟机地址尚未确定，链接改写暂不启用 */\n"

LINKFIX_JS = r"""
(function () {
  "use strict";
  var VM = "__VM_IP__";
  var SELF = "__SELF_PORT__";
  if (!VM) { return; }
  function eport(u) {
    if (u.port) { return u.port; }
    return u.protocol === "https:" ? "443" : "80";
  }
  function fix(u) {
    try {
      var x = new URL(String(u), location.href);
      if (x.hostname !== location.hostname) { return u; }
      if (eport(x) === SELF) { return u; }
      return x.protocol + "//" + VM + ":" + eport(x) + x.pathname + x.search + x.hash;
    } catch (e) { return u; }
  }
  var _open = window.open;
  if (_open) {
    window.open = function (u, n, f) {
      if (typeof u === "string" && u) { u = fix(u); }
      return _open.call(window, u, n, f);
    };
  }
  document.addEventListener("click", function (e) {
    var t = e.target;
    var a = t && t.closest ? t.closest("a[href]") : null;
    if (!a) { return; }
    try {
      var to = fix(a.href);
      if (to !== a.href) { a.href = to; }
    } catch (err) { }
  }, true);
})();
"""


def inject_linkfix(data):
    """把 linkfix 脚本插进 HTML 的 <head> 开头。

    面板主脚本是 type=module（默认延迟执行），此处的经典脚本会先跑完，
    保证 window.open 在用户点按钮之前已被接管。"""
    tag = ('<script src="%s"></script>' % LINKFIX_PATH).encode("utf-8")
    if tag in data:
        return data
    low = data.lower()
    i = low.find(b"<head")
    if i >= 0:
        j = low.find(b">", i)
        if j >= 0:
            return data[:j + 1] + tag + data[j + 1:]
    return tag + data


# 必须是**可重入**锁：_resolve_locked() 内部自己也要拿它，而调用方
# （discovery_loop 与 resolve_ip 的冷启动路径）是先持锁再调它。用普通 Lock 时
# 第一次发现就自我死锁——入口端口只听不答，整个入口页从此卡死
# （iStoreOS 1.1.7 引入、1.1.8 修复；离线用例「发现锁必须可重入」守着）。
_lock = threading.RLock()
_cache = {"ip": None, "mac": None, "ts": 0.0, "sweep_ts": 0.0, "source": None,
          "vnc_fail_ts": 0.0, "last_ip": None, "last_ts": 0.0}

# 冬瓜HAOS 控制台 8x16 字体字形库（从真实 VNC 帧缓冲自学习提取：
# 横幅固定前缀 + "0123456789" 回显交叉验证，36 字形覆盖数字/点/冒号）。
# 仅用于横幅 IP 的 OCR 反查，glyph→char 唯一，误识只会得到 '?'。
GLYPHS = {
  "00000000000000000000000000060c0c": "'", "0000000000007f0000000000": "-",
  "00000000000018180000000000000000": ".", "0000000000000103060c183060400000": "/",
  "0000000000001c3663636b6b6363361c": "0", "0000000000007e1818181818181e1c18": "1",
  "0000000000007f6303060c183060633e": "2", "0000000000003e636060603c6060633e": "3",
  "000000000000783030307f33363c3830": "4", "0000000000003e636060603f0303037f": "5",
  "0000000000003e636363633f0303061c": "6", "0000000000000c0c0c0c18306060637f": "7",
  "0000000000003e636363633e6363633e": "8", "0000000000001e306060607e6363633e": "9",
  "00000000000000181800000018180000": ":", "000000000000636363637f6363361c08": "A",
  "0000000000005c6663637b030343663c": "G", "00000000000063636363637f63636363": "H",
  "00000000000036777f6b6b6b63636363": "M", "00000000000063636363636b7f7f7763": "M",
  "0000000000003e63636363636363633e": "O", "0000000000003e636360301c0663633e": "S",
  "0000000000006e3333333e301e000000": "a", "0000000000006e33333333363c303038": "d",
  "0000000000003e6303037f633e000000": "e", "00000000000067666666666e36060607": "h",
  "0000000000003c18181818181c001818": "i", "000000000000636b6b6b6b7f37000000": "m",
  "0000000000006666666666663b000000": "n", "0000000000003e63636363633e000000": "o",
  "0000000f06063e66666666663b000000": "p", "0000000000000f060606666e3b000000": "r",
  "0000000000003e63301c06633e000000": "s", "000000000000386c0c0c0c0c3f0c0c08": "t",
  "0000000000006e333333333333000000": "u", "00000000000063361c1c1c3663000000": "x",
}


def _sh(cmd, timeout=10):
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        ).stdout
    except Exception:
        return ""


def tcp_open(ip, port, timeout=0.5):
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def http_alive(ip, port, timeout=1.2):
    """这一扇门上到底有没有真的在说 HTTP。

    只看 TCP 会跳早：HA 核心在开机后还要重启一轮，那几十秒里 socket 在听、
    连接能建立，但一发请求就断，浏览器落到 chrome-error 空白页
    （2026-09-14 真机录屏 +108.7s 复现）。要拿到 HTTP 响应才算这扇门开了。
    """
    if not ip or not valid_v4(ip):
        return False
    conn = None
    try:
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read(64)
        return True
    except Exception:
        return False
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def door_ready(ip, port):
    """指定那一扇门是否真能应答 HTTP——跳转必须按「要去的那扇门」判。

    18.2.3 只做到「任一门开了就算 ok」，结果管理后台 :8124 先起、Home
    Assistant :8123 还在起，/ha 照样把人 302 到 8123，浏览器吃一口
    ERR_CONNECTION_REFUSED（chrome-error 空白页，2026-09-14 两次录屏复现）。
    """
    return bool(ip) and tcp_open(ip, port, 0.6) and http_alive(ip, port)


def target_alive(ip, timeout=0.6):
    """目标是否真的可用：管理后台 :8124 或 Home Assistant :8123 任一门真能应答。

    冬瓜有两扇门，任一门开都说明系统已经起来、跳过去有东西可看；两扇都没开
    才需要区分「Web 还在起」还是「那地址根本是残留」。先用 TCP 快速筛（端口
    没开就直接否，省掉两秒超时），再要求 HTTP 真响应。
    """
    if not ip:
        return False
    for port in (HA_PORT, ADMIN_PORT):
        if tcp_open(ip, port, timeout) and http_alive(ip, port):
            return True
    return False


def valid_v4(s):
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def host_alive(ip, timeout=1):
    """这个地址上到底还有没有机器在应答。

    端口没通有两种完全不同的情况：系统已经起来、Home Assistant 还在拉镜像起
    服务（该说「还在起来」，首启真的能要 15~40 分钟），和 ARP/DHCP 租约里残留
    的地址压根没人在用（该说「还在获取 IP」）。只看端口分不出来。
    冬瓜这里比 ping 更可靠的信号是 ARP 表项：能解析到虚拟机 MAC 且状态 complete
    就是「机器在」。两样取其一即可（HAOS 有时不响应 ICMP）。
    """
    if not ip or not valid_v4(ip):
        return False
    if arp_complete_for(ip) and mac_ok_for(ip):
        return True
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout)), ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout + 2).returncode == 0
    except Exception:
        return False


def just_powered_on(window=BOOT_GRACE):
    """刚点过「启动虚拟机」或自动补开机不久的宽限期。

    这段时间里端口不应答是正常的（系统还在起），不能马上判成死地址，
    否则每次开机都会先闪一句「还没拿到 IP」。判据是开机请求文件的新鲜度——
    入口按钮、自动补开机、cmd/main 开机都会写它。
    """
    try:
        return time.time() - os.path.getmtime(START_REQ) < window
    except OSError:
        return False


def vm_running():
    return "running" in _sh(f"virsh -c qemu:///system domstate {VM_NAME}", 8)


# 开关机态的 1 秒记忆：virsh 一问要 0.1 秒上下，入口页连打时别每次都敲
_power = {"v": None, "ts": 0.0}
POWER_MEMO = 1.0


def vm_running_now(ttl=POWER_MEMO):
    """带短期记忆的「虚拟机现在到底开没开」。

    发现结论是后台线程维护的（没人看时 20 秒才一轮），拿它当开关机态就会
    出现：虚拟机都关掉了，入口还挂着「正在启动」最长 20 秒，「启动虚拟机」
    按钮迟迟不出来（iStoreOS 1.1.10 修）。开关机态本身很便宜，值得实时确认。
    """
    now = time.time()
    v = _power["v"]
    if v is not None and now - _power["ts"] < ttl:
        return v
    v = vm_running()
    _power.update(v=v, ts=now)
    return v


def platform_sync_start():
    """把应用中心的状态同步成「已启动」。

    入口页的一键开机/自动补开是直接 virsh start，平台完全不知情：应用中心还挂着
    「已停用」。而它一旦以为自己已经停用，界面上的「停用」就不会再回调 cmd/main
    ——实测虚拟机明明在跑，`appcenter-cli stop` 却成了空操作，必须先「启用」再
    「停用」才关得掉，用户只会被莫名卡住。这里补一次 `appcenter-cli start` 把
    记账扳回来（实测约 2 秒，且不会重启本服务，pid 不变）。
    """
    now = time.time()
    with _plat_lock:
        if now - _plat_sync["ts"] < PLAT_SYNC_COOLDOWN:
            return
        _plat_sync["ts"] = now
    if os.path.exists(STOP_MARK):
        return          # 用户刚刚点过停用，别跟他抢方向盘
    cli = shutil.which("appcenter-cli") or APPCENTER_CLI
    try:
        subprocess.run([cli, "start", APP_ID], capture_output=True, timeout=10)
    except Exception:
        pass            # 同步失败不影响入口本身，下次开机再试


def platform_sync_start_async():
    """平台记账的活不该拖住 HTTP 响应，丢后台干。"""
    threading.Thread(target=platform_sync_start, daemon=True).start()


def vm_defined():
    return bool(_sh(f"virsh -c qemu:///system dominfo {VM_NAME}", 8).strip())


def vm_power_on():
    """一键/自动开机：shut off→start，paused→resume，running→幂等。返回状态描述。"""
    # 登记开机请求：cmd/main 的关机守望进程据此放弃强制断电（并在关机落定后补开）；
    # 同时撤销「用户停用」标记，否则应用中心会把这个窗口误判成未运行/异常。
    try:
        with open(START_REQ, "w"):
            pass
    except OSError:
        pass
    try:
        os.remove(STOP_MARK)
    except OSError:
        pass
    st = _sh(f"virsh -c qemu:///system domstate {VM_NAME}", 8)
    if "running" in st:
        # 虚拟机本来就在跑也要扳一次记账：可能是上一版留下的背离，也可能是别人
        # 在「虚拟机」应用里开的——平台不知道，停用就会变成空操作。
        platform_sync_start_async()
        return "already-running"
    if not vm_defined():
        return "undefined"
    if "paused" in st:
        _sh(f"virsh -c qemu:///system resume {VM_NAME}", 15)
        platform_sync_start_async()
        return "resumed"
    _sh(f"virsh -c qemu:///system start {VM_NAME}", 20)
    platform_sync_start_async()
    return "starting"


def vm_mac():
    xml = _sh(f"virsh -c qemu:///system dumpxml {VM_NAME}", 8)
    m = re.search(r"<mac address='([0-9a-f:]{17})'", xml)
    if m:
        return m.group(1)
    m = re.search(r"52:54:(?:[0-9a-f]{2}:){3}[0-9a-f]{2}", xml)
    return m.group(0) if m else None


def arp_line_state_ok(flags_field):
    """ARP 表第 3 列 Flags：0x2 才是「已完成」的表项。

    没解析成功的残留项（Flags 0x0）也留在表里，MAC 还写着虚拟机的——虚拟机换了
    地址或根本没开机时，照单全收就会拿着一个死地址告诉用户「已找到
    192.168.1.x」，一等就是几十分钟。
    """
    try:
        return bool(int(flags_field, 16) & 0x2)
    except (ValueError, TypeError):
        return False


def arp_lookup(mac):
    if not mac:
        return None
    try:
        with open("/proc/net/arp") as f:
            for line in f:
                parts = line.split()
                if (len(parts) >= 4 and parts[3].lower() == mac.lower()
                        and arp_line_state_ok(parts[2])):
                    return parts[0]
    except OSError:
        pass
    return None


def arp_complete_for(ip):
    """ARP 表里该 IP 是否有 complete 表项（机器在不在的旁证）。"""
    try:
        with open("/proc/net/arp") as f:
            for line in f:
                parts = line.split()
                if (len(parts) >= 4 and parts[0] == ip
                        and arp_line_state_ok(parts[2])):
                    return True
    except OSError:
        pass
    return False


def arp_entries():
    ips = []
    try:
        with open("/proc/net/arp") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0] != "IP":
                    ips.append(parts[0])
    except OSError:
        pass
    return ips


def local_ips():
    ips = set()
    for line in _sh("ip -4 -o addr show", 8).splitlines():
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
        if m:
            ips.add(m.group(1))
    return ips


def domifaddr():
    for src in ("arp", "lease", "agent"):
        out = _sh(f"virsh -c qemu:///system domifaddr {VM_NAME} --source {src}", 8)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", out)
        if m:
            return m.group(1)
    return None


def arp_mac_of(ip):
    """ARP 表中该 IP 对应的 MAC（无记录返回 None）。"""
    try:
        with open("/proc/net/arp") as f:
            for line in f:
                parts = line.split()
                if (len(parts) >= 4 and parts[0] == ip
                        and arp_line_state_ok(parts[2])):
                    return parts[3].lower()
    except OSError:
        pass
    return None


def mac_ok_for(ip):
    """ARP 守门：若 ARP 表已明确记录该 IP 属于别的 MAC，则否决该候选。

    局域网里还有别的 Home Assistant / ESPHome 设备（它们同样广播
    homeassistant.local、同样开 8123），没有这道闸门就会跳到邻居的设备上。
    """
    vm = vm_mac()
    if not vm:
        return True
    m = arp_mac_of(ip)
    return m is None or m == vm.lower()


def mdns_probe():
    if not shutil.which("avahi-resolve"):
        return None
    for name in MDNS_NAMES:
        out = _sh(f"avahi-resolve -4 -n {name}", 8)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
        if not m:
            continue
        ip = m.group(1)
        if ip in local_ips() or not mac_ok_for(ip):
            continue
        if target_alive(ip):
            return ip
    return None


def ping_sweep():
    """对物理/LAN 接口的子网做快速 ping 扫描，逼出 ARP 表。
    跳过 docker/virbr 等虚拟网桥；注意 net.hosts() 是生成器，切片要先 list()。"""
    hosts = []
    out = _sh("ip -4 -o addr show scope global", 8)
    for line in out.splitlines():
        if re.search(r"\b(docker|br-|virbr|veth|vnet|tun|tap|lo)\S*", line):
            continue
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", line)
        if not m:
            continue
        try:
            net = ipaddress.ip_interface(m.group(1)).network
        except ValueError:
            continue
        if net.prefixlen <= 24:
            hosts.extend(str(h) for h in list(net.hosts())[:1024])
    if not hosts:
        return []
    def p(ip):
        try:
            subprocess.run(["ping", "-c", "1", "-W", "1", "-q", ip],
                           capture_output=True, timeout=3)
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=64) as ex:
        list(ex.map(p, hosts))
    return hosts


def portscan_fallback():
    """ARP 表活跃主机里探测 TCP :8124（冬瓜管理后台特征端口）补齐 IP。"""
    skip = local_ips()
    cands = [ip for ip in arp_entries() if ip not in skip]
    def probe(ip):
        if not mac_ok_for(ip):
            return None
        return ip if tcp_open(ip, ADMIN_PORT, 0.4) else None
    hits = []
    with ThreadPoolExecutor(max_workers=48) as ex:
        for r in ex.map(probe, cands):
            if r:
                hits.append(r)
    return hits[0] if hits else None


def vnc_socket_path():
    xml = _sh(f"virsh -c qemu:///system dumpxml {VM_NAME}", 8)
    m = re.search(r"<graphics[^>]*socket='([^']+)'", xml)
    if m:
        return m.group(1)
    m = re.search(r"socket='(/var/run/vms/[^']+)'", xml)
    return m.group(1) if m else None


def _vnc_grab(path):
    """RFB 最小客户端：抓一帧全屏 raw 帧缓冲，返回 (w,h,bpp,px) 或 None。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(4)
    try:
        s.connect(path)
        f = s.makefile('rwb')

        def rd(n):
            b = b''
            while len(b) < n:
                c = f.read(n - len(b))
                if not c:
                    raise EOFError
                b += c
            return b

        rd(12); f.write(b'RFB 003.008\n'); f.flush()
        n = rd(1)[0]
        types = rd(n)
        if 1 not in types:
            return None
        f.write(b'\x01'); f.flush()
        if struct.unpack('>I', rd(4))[0] != 0:
            return None
        f.write(b'\x01'); f.flush()
        hdr = rd(24)
        w, h = struct.unpack('>HH', hdr[:4])
        bpp = hdr[4]
        nl = struct.unpack('>I', hdr[20:24])[0]
        rd(nl)
        f.write(struct.pack('>BBHHHH', 3, 0, 0, 0, w, h)); f.flush()
        while True:
            mt = rd(1)[0]
            if mt != 0:
                return None
            rd(1)
            nr, = struct.unpack('>H', rd(2))
            best = None
            for _ in range(nr):
                rx, ry, rw, rh, enc = struct.unpack('>HHHHi', rd(12))
                if enc == 0:
                    pxd = rd(rw * rh * (bpp // 8))
                    if best is None or rw * rh > best[0] * best[1]:
                        best = (rw, rh, pxd)
                elif enc == -239:
                    cw, ch = struct.unpack('>HH', rd(4))
                    rd((bpp // 8) * cw * ch)
                    rd(((cw + 7) // 8) * ch)
            if best:
                return best[0], best[1], bpp, best[2]
    except Exception:
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def vnc_banner_ip():
    """直读虚拟机 VNC 横幅上的 IP（最快最直接），TCP 验证防横幅陈旧值。"""
    now = time.time()
    if now - _cache.get("vnc_fail_ts", 0) < 15:
        return None
    path = vnc_socket_path()
    ip = None
    if path and os.path.exists(path):
        grabbed = _vnc_grab(path)
        if grabbed:
            rw, rh, bpp, px = grabbed
            if bpp == 32:
                def bright(x, y):
                    i = (y * rw + x) * 4
                    b, g, r = px[i], px[i + 1], px[i + 2]
                    return (r * 299 + g * 587 + b * 114) // 1000 > 128
                y0 = None
                for y in range(min(40, rh)):
                    if sum(1 for x in range(min(rw, 1280, 1000)) if bright(x, y)) > 3:
                        y0 = y
                        break
                if y0 is not None and y0 + 16 <= rh:
                    text = []
                    for cidx in range(min(rw // 8, 200)):
                        m = 0
                        for r in range(16):
                            base = r * 8
                            for cc in range(8):
                                if bright(cidx * 8 + cc, y0 + r):
                                    m |= 1 << (base + cc)
                        text.append(' ' if m == 0 else GLYPHS.get(f'{m:032x}', '?'))
                    m2 = re.search(r"http://(\d+\.\d+\.\d+\.\d+):\d+", ''.join(text))
                    if m2:
                        ip = m2.group(1)
    if not ip or ip in local_ips():
        _cache["vnc_fail_ts"] = now
        return None
    if target_alive(ip):
        return ip
    _cache["vnc_fail_ts"] = now
    return None


def manual_ip():
    """入口页手动记下的跳转地址；无效或未设置返回 None。"""
    try:
        with open(MANUAL_IP_FILE) as f:
            v = f.readline().strip()
    except OSError:
        return None
    return v if valid_v4(v) else None


def save_manual_ip(v):
    """空串表示清除，返回生效后的值（None 或地址）。"""
    try:
        os.makedirs(os.path.dirname(MANUAL_IP_FILE), exist_ok=True)
        if v:
            with open(MANUAL_IP_FILE, "w") as f:
                f.write(v + "\n")
        elif os.path.exists(MANUAL_IP_FILE):
            os.remove(MANUAL_IP_FILE)
    except OSError:
        return None
    return v or None


# ---- 经 libvirt 串口在虚拟机里修网络（没有 IP 时这是唯一入口）----
#
# 冬瓜HAOS 是 Home Assistant OS（buildroot + systemd + NetworkManager），
# 不是 OpenWrt：没有 uci/netifd/ubus，网络归 NetworkManager 管，
# 串口登录后是 root shell（免密）。所以这里的命令全部走 nmcli/systemd。
# 外部传进来的东西必须先过 valid_v4 才拼得进来。

# HAOS 里除了物理口还有 docker0 / hassio 两个桥，按字母序「取第一个网卡」会
# 选中 docker0（真机实测）；只认 NetworkManager 报告 connected 的那个口。
# 连接名实测是 "Supervisor enp1s0"（NAME 里不含冒号，cut -d: 取第一段安全）。
_GUEST_PROBE = ('DEV=$(nmcli -t -f DEVICE,STATE device 2>/dev/null | '
                'awk -F: \'$2=="connected" && $1!="lo" && $1!~/^docker/ && $1!~/^veth/ \'{print $1; exit}\'); '
                '[ -n "$DEV" ] || DEV=$(ls /sys/class/net | grep -v "^lo$" | grep -v "^docker" | '
                'grep -v "^veth" | head -n1); '
                'CON=$(nmcli -t -f NAME,DEVICE con show --active 2>/dev/null | '
                'grep ":$DEV$" | cut -d: -f1 | head -n1); '
                'echo "DEV=$DEV CON=$CON"')
_GUEST_STATE = ('ip -4 a show dev "$DEV"; ip route; '
                'nmcli -t -f DEVICE,STATE device 2>/dev/null')


def guest_run(cmds, per=4, tail=900):
    """通过 libvirt 串口在虚拟机里执行命令。

    用 script(1) 造一个 pty 挂住 virsh console，逐条喂命令，把屏幕内容读回来。
    所有命令喂进的是同一个交互 shell，所以前面赋值的环境变量后面还能用。
    HAOS 串口先要登录（root 免密）；已经登录时那行只是报个 command not found。
    """
    lines = ["sleep 2", "printf '\\n'", "sleep 1", "printf 'root\\n'", "sleep 2"]
    for c in cmds:
        lines.append("printf '%s\\n' " + shlex.quote(c))
        lines.append("sleep %d" % per)
    lines.append("sleep 3")
    total = 5 + per * len(cmds) + 20
    pipeline = ("{ " + "; ".join(lines) + "; } | timeout " + str(total) +
                " script -qec 'virsh -c qemu:///system console " + VM_NAME +
                "' /dev/null")
    try:
        p = subprocess.run(["bash", "-c", pipeline], capture_output=True,
                           text=True, timeout=total + 25)
    except Exception as exc:
        return "串口执行失败：" + str(exc)
    return ((p.stdout or "") + (p.stderr or ""))[-tail:]


def guest_dhcp_retry():
    """让网卡重新走一遍 DHCP。

    只让 NetworkManager 自己重来（reconnect，老版本回落到 disconnect+connect），
    绝不在 NM 托管的口上手跑 dhclient/udhcpc——那类客户端退出时会把地址和路由
    一起撤掉，实测在 iStoreOS 上把静态口搞成过整机失联。
    """
    return guest_run([_GUEST_PROBE,
                      'nmcli device reconnect "$DEV" 2>/dev/null || '
                      '{ nmcli device disconnect "$DEV"; sleep 2; '
                      'nmcli device connect "$DEV"; }',
                      _GUEST_STATE], per=8)


def guest_net_restart():
    """整机网络重启：治 NetworkManager 状态错乱（地址在、路由没了这类）。"""
    return guest_run([_GUEST_PROBE,
                      "systemctl restart NetworkManager",
                      "sleep 6",
                      'nmcli device connect "$DEV" 2>/dev/null || true',
                      _GUEST_STATE], per=9)


def guest_net_mode(mode, ip="", prefix="24", gw="", dns=""):
    """把网卡设为静态地址或恢复自动获取；参数非法返回 None。"""
    if mode == "static":
        try:
            net = ipaddress.IPv4Network(ip + "/" + prefix, strict=False)
        except ValueError:
            return None
        prefix = str(net.prefixlen)
        parts = ['ipv4.method static', 'ipv4.addresses "$IP/{}/"']
        if gw:
            parts.append('ipv4.gateway "' + gw + '"')
        if dns:
            parts.append('ipv4.nameservers "' + dns + '"')
        cmds = [_GUEST_PROBE, 'IP=' + ip,
                'nmcli con mod "$CON" ' + " ".join(parts).format(prefix),
                'nmcli con up "$CON"']
    else:
        cmds = [_GUEST_PROBE,
                'nmcli con mod "$CON" ipv4.method auto ipv4.addresses "" '
                'ipv4.gateway "" ipv4.nameservers ""',
                'nmcli con up "$CON"']
    cmds.append(_GUEST_STATE)
    return guest_run(cmds, per=5)


# 串口操作要几十秒，HTTP 请求不能干等：放后台线程跑，入口页 3 秒一轮看进度
_net_lock = threading.Lock()
_net_job = {"running": False, "note": "", "out": "", "ts": 0.0}


def start_net_job(kind, args=(), note=""):
    # 注意锁顺序：调用方都是先 resolve_ip()（内部要 _lock）再拿 _net_lock，
    # 反过来没有，不存在嵌套死锁。
    with _net_lock:
        if _net_job["running"]:
            return False
        if not vm_running():
            _net_job.update(running=False, note="虚拟机没在运行，串口进不去", out="",
                            ts=time.time())
            return False
        _net_job.update(running=True, note=note, out="", ts=time.time())

    def worker():
        try:
            if kind == "dhcp":
                out = guest_dhcp_retry()
            elif kind == "dhcp-mode":
                out = guest_net_mode("dhcp")
            elif kind == "net-restart":
                out = guest_net_restart()
            else:
                out = guest_net_mode("static", *args)
        except Exception as exc:
            out = "执行异常：" + str(exc)
        with _net_lock:
            _net_job.update(running=False, note=note, out=(out or "")[-1200:])

    threading.Thread(target=worker, daemon=True).start()
    return True


def _resolve_locked():
    """真正干活的发现链，只在持有 _lock 时调用。返回 dict：
      ip      探活通过、可以直接跳过去的地址（没有就 None）
      source  发现层（mac+arp / vnc-banner / mdns / domifaddr / sweep+arp /
              portscan8124 / manual）
      stage   ok / web-warming（找到地址但两扇门都没开）/ no-ip（开好了没地址）/
              stopped（虚拟机没开）/ booting（刚确认开着、地址还没查出来，
              由 resolve_ip 的开关机态校正临时给出，发现链不产出这个值）
      probing 探活没通过的那个候选地址，纯给排障看
      stale   该候选已经不应答了（ARP/租约残留），此时 stage 是 no-ip
      mac     虚拟机网卡 MAC，排障用

    这条链在「没有地址」时最费时间（VNC 抓帧 + mDNS + domifaddr + 整段局域网
    ping 扫描 + 端口探测，实测单次可达十几秒），所以绝不直接在 HTTP 请求里
    调用它——统一走下面的 resolve_ip()，请求侧只读缓存。
    """
    now = time.time()
    with _lock:
        # 手动指定过地址就优先用它（仍要探活），发现链救不了的场景兜底
        man = manual_ip()
        if man and target_alive(man):
            return {"ip": man, "source": "manual", "stage": "ok",
                    "probing": None, "stale": False, "mac": vm_mac()}
        if not vm_running():
            _cache["ip"] = None
            return {"ip": None, "source": None, "stage": "stopped",
                    "probing": None, "stale": False, "mac": vm_mac()}
        # 缓存命中：快速验证后直接采用
        if (_cache["ip"] and now - _cache["ts"] < CACHE_TTL
                and mac_ok_for(_cache["ip"]) and target_alive(_cache["ip"])):
            return {"ip": _cache["ip"], "source": _cache["source"], "stage": "ok",
                    "probing": None, "stale": False, "mac": _cache["mac"]}
        mac = vm_mac()
        # 1) MAC → ARP
        ip = arp_lookup(mac)
        source = "mac+arp" if ip else None
        # 2) VNC 横幅直读（最快路径：屏幕上的 IP，TCP 验证后采信）
        if not ip:
            ip = vnc_banner_ip()
            source = "vnc-banner" if ip else None
        # 3) mDNS
        if not ip:
            ip = mdns_probe()
            source = "mdns" if ip else None
        # 4) virsh domifaddr
        if not ip:
            ip = domifaddr()
            source = "domifaddr" if ip else None
        # 5) ping 扫描后再查 ARP
        if not ip and now - _cache["sweep_ts"] > SWEEP_COOLDOWN:
            _cache["sweep_ts"] = now
            ping_sweep()
            ip = arp_lookup(mac)
            if ip:
                source = "sweep+arp"
        # 6) 端口探测补齐（专打 :8124）
        if not ip:
            ip = portscan_fallback()
            source = "portscan8124" if ip else None
        # 统一守门：任何一层拿到的候选都要核对网卡 MAC。mdns/端口扫描层内部
        # 已经查过，但 VNC 横幅、domifaddr、ping 扫描没有——局域网里还有别的
        # Home Assistant / ESPHome 设备同样广播 homeassistant.local、同样开
        # 8123， ARP 明确说那地址是别人的 MAC 就不能跳过去。
        if ip and not mac_ok_for(ip):
            ip, source = None, None
        probing, stale = None, False
        if ip and not target_alive(ip):
            # 两扇门都没通不等于「Web 还在起」，要先看那地址上到底有没有机器：
            #   有人应答 → 系统起来了、HA 还在拉镜像起服务 → web-warming
            #   没人应答 → ARP/租约里的残留地址 → no-ip（得让用户去手动处理）
            # 一律报 web-warming 的话，路由器不发地址时入口能挂着「已找到
            # 192.168.1.x，还在起来」挂一整天，把排障带偏。
            # 地址作废时 source 也得一起作废，否则 /api/status 会出现
            # {"ip": null, "source": "mac+arp"} 这种自相矛盾的排障线索。
            probing, ip, source = ip, None, None
            if not host_alive(probing) and not just_powered_on():
                stale = True
        if ip:
            _cache.update(ip=ip, mac=mac, ts=now, source=source,
                          last_ip=ip, last_ts=now)
            return {"ip": ip, "source": source, "stage": "ok",
                    "probing": None, "stale": False, "mac": mac}
        # 开机宽限期内刚拿到的地址别急着丢：虚拟机刚点亮那几十秒里 ARP 表项会
        # 先变成 FAILED/残留，MAC 守门于是把每一层的候选都否掉，入口就会闪一句
        # 「路由器 DHCP 没发地址」+「手动处理」——其实只是网卡还没回话
        #（2026-09-14 真机录屏 +35.6s 复现）。宽限期内改口「还在起来」。
        if not ip and just_powered_on():
            # 开机宽限期内不说「路由器 DHCP 没发地址」：这一分钟里 ARP 会先变成
            # FAILED/残留，MAC 守门把每层候选都否掉，实测（录屏 +64.5s）会闪一句
            # 甩锅路由器的话、还催用户点手动处理，而 HAOS 本来就在后台持续重试。
            # 宽限期锚在开机时刻，不看「上次成功」——重启后那次成功往往早就过期了。
            return {"ip": None, "source": None, "stage": "web-warming",
                    "probing": probing or _cache.get("last_ip"),
                    "stale": False, "mac": mac}
        _cache["ip"] = None
        return {"ip": None, "source": None,
                "stage": "no-ip" if (stale or not probing) else "web-warming",
                "probing": probing, "stale": stale, "mac": mac}


# 发现结果由后台线程维护，HTTP 请求只读缓存：没地址时入口也能瞬间出画面。
# ask 是「上一次有人真的来看」的时间戳——没人看的时候把节奏放慢，别空转。
_last = {"res": None, "ts": 0.0, "ask": 0.0}
_kick = threading.Event()     # 有人发现开关机态变了，喊后台立刻跑一轮
DISCOVERY_INTERVAL = 5      # 有人在看：每 5 秒发现一次
DISCOVERY_IDLE = 20         # 没人看：降到每 20 秒


def _power_transition(on_now):
    """开关机态刚翻转时的过渡结论（只在请求线程里拼，绝不跑发现链）。

    关机方向可以当场断定（没什么可发现的），所以顺手把缓存也改对，
    入口页下一眼就是「已关机 + 启动虚拟机」；开机方向地址还不知道，
    先给中立的 booting（页面表现就是「正在启动」），地址交给后台那一轮。
    """
    if not on_now:
        res = {"ip": None, "source": None, "stage": "stopped", "probing": None,
               "stale": False, "mac": _cache.get("mac")}
        with _lock:
            _cache["ip"] = None
            _last.update(res=res, ts=time.time())
        return res
    return {"ip": None, "source": None, "stage": "booting", "probing": None,
            "stale": False, "mac": _cache.get("mac")}


def resolve_ip():
    """入口页与状态接口统一用它：瞬间返回后台发现线程维护的最新结论。

    冷启动（一次都还没跑过）才现算一次，避免首页空着。返回的结论最多旧一个
    发现周期（5 秒），跟入口页自己的轮询节奏一致，用户看不出差别——
    唯独开关机态例外：那个太便宜，值得每次跟虚拟机实际状态对一遍。
    """
    res = _last["res"]
    _last["ask"] = time.time()
    if res is None:
        with _lock:
            if _last["res"] is None:
                _last.update(res=_resolve_locked(), ts=time.time())
            res = _last["res"]
    else:
        on_now = vm_running_now()
        if (res.get("stage") != "stopped") != on_now:
            _kick.set()             # 让后台发现线程别等到下个周期
            res = _power_transition(on_now)
    return dict(res) if res else {"ip": None, "source": None, "stage": "no-ip",
                                  "probing": None, "stale": False, "mac": None}


def discovery_loop():
    """后台单飞：按节奏跑发现链，结果写进 _last 供所有请求读。"""
    while True:
        try:
            with _lock:
                res = _resolve_locked()
            _last.update(res=res, ts=time.time())
        except Exception:
            pass    # 单轮失败下一轮再来，别把线程搞死
        # 入口页开着时勤快点，没人看时省点 CPU；
        # 有人看见开关机态变了会 _kick.set()，这里立刻醒过来跑一轮
        gap = (DISCOVERY_INTERVAL
               if time.time() - _last["ask"] < 30 else DISCOVERY_IDLE)
        _kick.wait(gap)
        _kick.clear()


PAGE_TMPL = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">\
<meta name="viewport" content="width=device-width,initial-scale=1">\
$REFRESH<title>冬瓜HAOS</title><style>\
*{box-sizing:border-box}body{font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;\
background:#0b1117;color:#e6edf3;display:flex;align-items:center;justify-content:center;\
min-height:100vh;margin:0;-webkit-font-smoothing:antialiased}\
.c{text-align:center;max-width:420px;padding:24px}\
.dot{width:10px;height:10px;border-radius:50%;background:#4b5b6b;display:block;margin:0 auto 22px}\
.dot.wait{background:#e8a03a;animation:br 1.6s ease-in-out infinite}\
@keyframes br{50%{opacity:.35}}\
h1{font-size:20px;font-weight:600;margin:0 0 10px;letter-spacing:.5px}\
p{color:#8296a8;font-size:14px;margin:0 0 24px;line-height:1.7}\
code{font-size:12px;color:#5f7385;background:#131c26;padding:2px 8px;border-radius:6px}\
button{font-size:15px;padding:11px 34px;border-radius:999px;border:0;background:#2563eb;\
color:#fff;cursor:pointer;letter-spacing:1px}button:hover{background:#3b76f0}\
button.alt{background:#1d2b38;color:#a9bccd}a{color:#4f8df9;text-decoration:none}\
\
.mini{color:#5f7385;font-size:12px;margin:0 0 14px;line-height:1.75;word-break:break-all}\
.row{display:flex;gap:10px;justify-content:center;align-items:center;flex-wrap:wrap;margin:0 0 12px}\
.row form{margin:0}\
button.sm{font-size:14px;padding:9px 20px;letter-spacing:.5px}\
details{max-width:340px;margin:0 auto 12px;border:1px solid #16222e;border-radius:14px;\
padding:0 14px;text-align:left}\
summary{font-size:13px;color:#8296a8;padding:12px 0;cursor:pointer;list-style:none;outline:none}\
summary::-webkit-details-marker{display:none}\
summary::before{content:"▸  ";color:#4f8df9}\
details[open] summary::before{content:"▾  "}\
details form{margin:4px 0 14px;display:flex;gap:8px;flex-wrap:wrap}\
details input,details select,details button{margin:0}\
details input{flex:1 1 118px;min-width:0}\
.sec{text-align:left;font-size:13px;color:#5f7385;margin:20px 0 10px;\
border-top:1px solid #16222e;padding-top:14px}\
form{margin:0 0 12px}input,select{font-size:14px;padding:10px 12px;border-radius:10px;\
border:1px solid #223140;background:#101a23;color:#e6edf3;margin:0 6px 10px 0}\
pre{font-size:12px;color:#8296a8;background:#101a23;padding:10px 12px;border-radius:10px;\
text-align:left;overflow:auto;white-space:pre-wrap}\
</style></head><body><div class="c">$BODY</div></body></html>"""


STATE_FILE = "/tmp/haos-install.state"
DISK_MARK_FILE = "/vol1/vm/haos.disk-version"


def os_version():
    """磁盘里的 HAOS 版本：装机时写的标记，退回虚拟机定义里的 osVersion。"""
    try:
        with open(DISK_MARK_FILE) as f:
            v = f.readline().strip()
            if v:
                return v
    except OSError:
        pass
    xml = _sh(f"virsh -c qemu:///system dumpxml {VM_NAME}", 8)
    m = re.search(r"Home Assistant OS ([0-9.]+)", xml)
    return m.group(1) if m else None


def install_state():
    """后台安装进度（install_callback 秒回模式写入）；无文件返回 None。"""
    try:
        with open(STATE_FILE) as f:
            return f.read().strip()[:200] or None
    except OSError:
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "DongGuaHaFinder/1.2"

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _page(self, title, msg, refresh=0, dot=""):
        rf = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
        body = f'<span class="dot {dot}"></span><h1>{title}</h1><p>{msg}</p>'
        html = PAGE_TMPL.replace("$REFRESH", rf).replace("$BODY", body)
        return html.encode("utf-8")

    def _raw(self, body, refresh=0):
        rf = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
        html = PAGE_TMPL.replace("$REFRESH", rf).replace("$BODY", body)
        return html.encode("utf-8")

    def _redirect(self, host, port, tail):
        self.send_response(302)
        self.send_header("Location", f"http://{host}:{port}{tail}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _net_page(self, note=""):
        """网络修复页：与入口页同一套简洁风——默认只露「重新获取 IP」，
        静态地址、手动跳转地址、串口输出都折进可展开的小节，状态压成一行小字。
        页面不自动刷新（表单页刷新会把填一半的东西冲掉），
        只有串口任务在跑时才 3 秒轮一次。"""
        st = resolve_ip()
        man = manual_ip() or ""
        with _net_lock:
            job = dict(_net_job)
        esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;")
        busy = job["running"]
        stage_cn = {"ok": "就绪", "web-warming": "Home Assistant 启动中",
                    "no-ip": "还在获取 IP", "stopped": "虚拟机未开机",
                    "booting": "系统启动中"}.get(st["stage"], st["stage"])
        p = ['<div><span class="dot%s"></span></div><h1>网络修复</h1>'
             % (" wait" if busy else ""),
             '<p class="mini">下面几步经 libvirt 串口在虚拟机里执行，它没有 IP 也能用。</p>']
        if note:
            p.append('<p class="mini"><code>%s</code></p>' % esc(note))
        if busy:
            p.append('<p class="mini">串口正在执行：%s，约需一分钟，页面会自动刷新。</p>'
                     % esc(job["note"]))
        p.append('<div class="row"><form method="post" action="/net/dhcp">'
                 '<button type="submit">重新获取 IP</button></form></div>')
        p.append('<div class="row">'
                 '<form method="post" action="/net/restart">'
                 '<button class="alt sm" type="submit">重启网络</button></form>'
                 '<form method="post" action="/net/dhcp-mode">'
                 '<button class="alt sm" type="submit">恢复自动获取</button></form></div>')
        p.append('<details><summary>路由器不发地址时：指定静态 IP</summary>'
                 '<form method="post" action="/net/static">'
                 '<input name="ip" placeholder="IP 192.168.1.50" inputmode="decimal">'
                 '<select name="prefix"><option value="24">/24</option>'
                 '<option value="16">/16</option><option value="8">/8</option></select>'
                 '<input name="gw" placeholder="网关（可留空）">'
                 '<input name="dns" placeholder="DNS（可留空）">'
                 '<button class="sm" type="submit">应用</button></form></details>')
        p.append('<details><summary>虚拟机在别的网段：手动指定跳转地址</summary>'
                 '<form method="post" action="/net/manual">'
                 '<input name="ip" placeholder="%s" inputmode="decimal">'
                 '<button class="sm" type="submit">保存</button></form>%s</details>'
                 % (esc(man) or "已知地址 192.168.1.1",
                    ('<p class="mini">当前 %s</p>'
                     '<form method="post" action="/net/manual">'
                     '<button class="alt sm" type="submit" name="clear" value="1">'
                     '清除手动地址</button></form>' % esc(man)) if man else ""))
        if not busy and job["out"]:
            p.append('<details><summary>串口输出</summary><pre>%s</pre></details>'
                     % esc(job["out"]))
        p.append('<p class="mini">%s · %s · HAOS %s</p>'
                 % (esc(stage_cn), esc(st["mac"] or "无网卡"),
                    esc(os_version() or "未知")))
        p.append('<p><a href="/">← 返回入口</a></p>')
        return self._raw("".join(p), refresh=3 if busy else 0)

    def _state_page(self, r, ha=False):
        """寻踪没结果时的分状态页（两个图标共用）。"""
        st = install_state()
        if st and st != "ready":
            return self._page("正在准备",
                              f"首次安装要下载镜像本体约 886~904 MB（视所选版本），"
                              f"视带宽约 5~20 分钟"
                              f"<br><code>{st}</code>", refresh=5, dot="wait")
        if not vm_defined():
            return self._page("准备中", "正在等待应用完成安装。", refresh=5, dot="wait")
        if r["stage"] == "stopped":
            if os.path.exists(STOP_MARK):
                # 用户在应用中心点过「停用」：那就别擅自开机，给简洁关机页 +
                # 一键开机按钮（点了才开，开了自动跳）。
                # 第二个图标（/ha）进来就带上 next=/ha，否则开完机只会落到
                # 默认落点（管理后台），用户从 HA 图标点却进了后台。
                nxt = "?next=/ha" if ha else ""
                btn = (f'<form method="POST" action="/power/start{nxt}">'
                       '<button type="submit">启动虚拟机</button></form>')
                return self._page("冬瓜HAOS 已关机", btn, refresh=3)
            # 不是用户关的（应用重启、虚拟机里手动关机等）：打开入口就顺手补开机。
            # 应用中心的「启用」不会回调应用脚本（实测 cmd/main 只收到 stop）。
            vm_power_on()
            # 这里不能写 url=：本页是从 GET "/" 或 GET "/ha" 直接渲染的，
            # 不带 url 的 meta refresh 会重刷当前路径，正好各自保持入口；
            # 就绪后 do_GET 按 /ha 跳 8123、其它跳管理后台。
            return self._page("正在启动", "Home Assistant 就绪后会自动打开。",
                              refresh=3, dot="wait")
        if r["stage"] == "booting":
            # 刚确认虚拟机开着、地址还等后台那一轮：给中立的「正在启动」，
            # 这时说「路由器 DHCP 没发地址」为时过早。
            return self._page("正在启动", "Home Assistant 就绪后会自动打开。",
                              refresh=3, dot="wait")
        if r["stage"] == "web-warming":
            if not r.get("probing"):
                # 宽限期内连候选都还没有：别硬凑一句「已找到 None」
                return self._page("正在启动",
                                  "Home Assistant 就绪后会自动打开。",
                                  refresh=3, dot="wait")
            return self._page(
                "正在启动",
                f"已找到 <code>{r['probing']}</code>，Home Assistant 还在起来"
                f"（首次开机要从上游拉取运行镜像，可能需 15~40 分钟）。",
                refresh=5, dot="wait")
        if r["stage"] == "ok":
            # 地址有了、只是「要去的那扇门」还在起（多半是 8123 比 8124 慢）：
            # 上一道闸门把跳转挡下来了，这里就得说人话，别掉到最后那句「获取不到
            # 地址」——那样等于把已经拿到的地址又吞回去骗用户。
            return self._page(
                "正在启动",
                f"已找到 <code>{r['ip']}</code>，Home Assistant 还在起来"
                f"（首次开机要从上游拉取运行镜像，可能需 15~40 分钟）。",
                refresh=3, dot="wait")
        # 虚拟机开着却始终没有可用地址：现实里真发生过——链路通，但 LAN 上没有
        # DHCPv4 服务器应答。这时不能只让用户干等，给出串口修复入口。
        residue = (f"<br>ARP 里还留着 <code>{r['probing']}</code>，"
                   f"但它现在不应答，是上次开机留下的。"
                   if r.get("stale") and r.get("probing") else "")
        return self._page(
            "正在获取地址",
            f"虚拟机已开机，HAOS 还没拿到 IPv4（网卡 <code>{r['mac'] or '未知'}</code>）。"
            f"{residue}<br>HAOS 会在后台一直重试，拿到地址就自动打开；"
            f"要是不想等，可以<a href=\"/net\">手动处理</a>"
            f"（重试 DHCP / 设静态地址）。",
            refresh=5, dot="wait")

    def do_GET(self):
        path = self.path
        pure = path.split("?")[0]
        if pure in ("/healthz", "/api/healthz"):
            self._send(200, b"ok", "text/plain")
            return
        if pure == "/api/status":
            r = resolve_ip()
            payload = json.dumps(
                # running 用同一份快照判断：早先是再调一次 vm_running()，与 stage
                # 的来源不是同一次 virsh，出现过 stage=no-ip 而 running=false 的
                # 自相矛盾输出。
                {"running": r["stage"] != "stopped", "ip": r["ip"],
                 "source": r["source"], "stage": r["stage"], "probing": r["probing"],
                 "mac": r["mac"], "os_version": os_version(), "manual": manual_ip(),
                 "stale": bool(r.get("stale")),
                 "admin_port": ADMIN_PORT, "ha_port": HA_PORT,
                 "admin_proxy": ADMIN_PROXY_PORT,
                 "install_state": install_state()},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
            return
        if pure == "/net":
            self._send(200, self._net_page())
            return
        if pure == "/power/start":
            # 开机接口只认 POST。浏览器停在 /power/start 时刷新、后退或
            # meta refresh 都会走到这里，必须回本站 "/"，不能把控制路径
            # 原样镜像给虚拟机。
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        r = resolve_ip()
        ha_wanted = pure == "/ha" or pure.startswith("/ha/")
        # 要去的那扇门没开就别跳：留着「正在启动」页下一轮再看。/api/ 是排障
        # 接口，不受这道闸门影响（它就是要能说清「现在还没开」）。
        if (r["ip"] and not pure.startswith("/api/")
                and not door_ready(r["ip"], HA_PORT if ha_wanted else ADMIN_PORT)):
            self._send(200, self._state_page(r, ha=ha_wanted))
            return
        if r["ip"]:
            tail = pure or "/"
            if "?" in path:
                tail += "?" + path.split("?", 1)[1]
            if ha_wanted:
                # 桌面第二个图标「Home Assistant」→ 虚拟机上的 8123
                self._redirect(r["ip"], HA_PORT, tail[3:] or "/")
                return
            if pure.startswith("/api/"):
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            # 默认落点：冬瓜管理后台——走本机 36124 反代（去掉低版本浏览器横幅）
            host = ((self.headers.get("Host") or "").rsplit(":", 1)[0]
                    or self.server.server_address[0])
            self._redirect(host, ADMIN_PROXY_PORT, tail)
            return
        self._send(200, self._state_page(
            r, ha=(pure == "/ha" or pure.startswith("/ha/"))))

    def do_POST(self):
        pure = self.path.split("?")[0]
        raw = b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                raw = self.rfile.read(length)
        except ValueError:
            pass
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        field = lambda k: (form.get(k) or [""])[0].strip()

        if pure == "/power/start":
            qs = urllib.parse.parse_qs(
                self.path.split("?", 1)[1] if "?" in self.path else "")
            nxt = "/ha" if (qs.get("next") or [""])[0] == "/ha" else "/"
            res = vm_power_on()
            msg = {"starting": "Home Assistant 就绪后会自动打开。",
                   "resumed": "Home Assistant 就绪后会自动打开。",
                   "already-running": "虚拟机已在运行。",
                   "undefined": "正在等待应用完成安装。"}[res]
            # 刷新必须显式回到 "/"：只写 content="5" 会拿当前 URL
            # （/power/start）重新 GET，等于把控制路径跳给虚拟机。
            self._send(200, self._page("正在启动", msg, refresh=f"5;url={nxt}",
                                       dot="wait"))
            return

        if pure == "/net/dhcp":
            ok = start_net_job("dhcp", note="让 NetworkManager 重新获取地址")
            self._send(200, self._net_page("" if ok else "上一个串口任务还没结束，稍等一下"))
            return
        if pure == "/net/dhcp-mode":
            ok = start_net_job("dhcp-mode", note="把网卡改回自动获取")
            self._send(200, self._net_page("" if ok else "上一个串口任务还没结束，稍等一下"))
            return
        if pure == "/net/restart":
            ok = start_net_job("net-restart", note="重启虚拟机网络")
            self._send(200, self._net_page("" if ok else "上一个串口任务还没结束，稍等一下"))
            return
        if pure == "/net/static":
            ip, gw, dns, prefix = field("ip"), field("gw"), field("dns"), field("prefix") or "24"
            bad = [v for v in (ip, gw, dns) if v and not valid_v4(v)]
            if not valid_v4(ip) or bad or prefix not in ("8", "16", "24"):
                self._send(200, self._net_page("地址不合法：请填写正确的 IPv4（网关/DNS 可留空）"))
                return
            ok = start_net_job("static", args=(ip, prefix, gw, dns),
                               note=f"设静态地址 {ip}/{prefix}")
            self._send(200, self._net_page("" if ok else "上一个串口任务还没结束，稍等一下"))
            return
        if pure == "/net/manual":
            if field("clear") == "1":
                save_manual_ip("")
                self._send(200, self._net_page("已清除手动跳转地址。"))
                return
            v = field("ip")
            if v and not valid_v4(v):
                self._send(200, self._net_page("地址不合法：请填写正确的 IPv4"))
                return
            save_manual_ip(v)
            self._send(200, self._net_page(
                f"手动跳转地址已{'保存，探活通过就会直接跳过去' if v else '清除'}"
                f'{": " + v if v else ""}。'))
            return
        self._send(404, b"not found", "text/plain")

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        pass

    def handle_error(self, request, client_address):
        """入口页每 3~5 秒轮询，浏览器经常提前掐线；这种 ConnectionResetError
        不是故障，别把它刷进 journal。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
              "content-length"}


class ProxyHandler(Handler):
    """反向代理冬瓜管理后台（VM:8124），透传的同时改写前端 UA 检测，去掉横幅提示。"""

    server_version = "DongGuaHaProxy/1.1"

    def _linkfix(self):
        """本机自供的链接改写脚本（每次现算虚拟机 IP，绝不缓存）。"""
        ip = resolve_ip().get("ip")
        if not ip:
            data = LINKFIX_NOOP.encode("utf-8")
        else:
            data = (LINKFIX_JS.replace("__VM_IP__", ip)
                    .replace("__SELF_PORT__", str(ADMIN_PROXY_PORT))).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _proxy(self):
        if self.path.split("?")[0] == LINKFIX_PATH:
            return self._linkfix()
        r = resolve_ip()
        ip = r["ip"]
        if not ip:
            # 与入口页同一套分状态文案，别在这里另写一套口径
            self._send(200, self._state_page(r))
            return
        body = None
        cl = self.headers.get("Content-Length")
        if cl:
            try:
                body = self.rfile.read(int(cl))
            except (ValueError, OSError):
                body = None
        conn = None
        try:
            hdrs = {k: v for k, v in self.headers.items()
                    if k.lower() not in ("host", "connection", "accept-encoding",
                                         "content-length")}
            hdrs["Host"] = f"{ip}:{ADMIN_PORT}"
            hdrs["Connection"] = "close"
            conn = http.client.HTTPConnection(ip, ADMIN_PORT, timeout=20)
            conn.request(self.command, self.path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            ctype = (resp.getheader("Content-Type") or "").lower()
            if "javascript" in ctype or "ecmascript" in ctype:
                data, n = UA_CHECK_RE.subn(b"return!0", data)
                if n:
                    print(f"[dongguaha-web] ua-check patched x{n} in {self.path.split('?')[0]}")
            if "text/html" in ctype:
                data = inject_linkfix(data)
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in HOP_BY_HOP:
                    continue
                if k.lower() == "location":
                    v = v.replace(f"http://{ip}:{ADMIN_PORT}",
                                  f"http://{self.headers.get('Host') or self.client_address[0] + ':' + str(ADMIN_PROXY_PORT)}")
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except Exception as e:
            self._send(502, self._page(
                "管理后台暂不可达",
                f"无法连接 <code>{ip}:{ADMIN_PORT}</code>（{type(e).__name__}），"
                f"若系统刚启动请稍后重试。", refresh=5, dot="wait"))
        finally:
            if conn:
                try:
                    conn.close()
                except OSError:
                    pass

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = _proxy


def main():
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    proxyd = http.server.ThreadingHTTPServer(("0.0.0.0", ADMIN_PROXY_PORT), ProxyHandler)
    proxyd.daemon_threads = True
    threading.Thread(target=proxyd.serve_forever, daemon=True).start()
    threading.Thread(target=discovery_loop, daemon=True).start()
    print(f"[dongguaha-web] {PORT} finder, {ADMIN_PROXY_PORT} admin-proxy "
          f"-> vm '{VM_NAME}' admin:{ADMIN_PORT} ha:{HA_PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
