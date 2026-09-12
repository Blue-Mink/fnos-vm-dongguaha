#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冬瓜HAOS IP 寻踪转发器

监听 NAS 固定端口（默认 36123），为应用中心/桌面图标提供固定入口：
  · GET /      → 302 到冬瓜管理后台 http://<VM_IP>:8124/
  · GET /ha... → 302 到 Home Assistant http://<VM_IP>:8123/
  · GET /api/status → JSON 状态

虚拟机 IP 多级发现链（找到即缓存 20s，缓存命中先做 TCP 快速验证）：
  1. libvirt XML 的 MAC → /proc/net/arp 反查（最准）
  2. mDNS：avahi-resolve homeassistant.local（HAOS 默认广播），TCP 验证
  3. virsh domifaddr --source arp/lease
  4. 对本机 /24 做并行 ping 扫描逼出 ARP，再按 MAC 反查
  5. 端口补齐：对 ARP 表中活跃主机探测 TCP :8124（冬瓜管理后台特征端口），
     命中即认定为目标（排除 NAS 自身），并排除仅有 :8123 的普通 HA 设备
"""
import http.client
import http.server
import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

VM_NAME = os.environ.get("HAOS_VM_NAME", "haos")
PORT = int(os.environ.get("HAOS_WEB_PORT", "36123"))
ADMIN_PORT = int(os.environ.get("HAOS_ADMIN_PORT", "8124"))   # 冬瓜管理后台（默认落点）
HA_PORT = int(os.environ.get("HAOS_HA_PORT", "8123"))          # Home Assistant 页面
ADMIN_PROXY_PORT = int(os.environ.get("HAOS_ADMIN_PROXY_PORT", "36124"))  # 管理后台反代（去横幅）
MDNS_NAME = os.environ.get("HAOS_MDNS_NAME", "homeassistant.local")
CACHE_TTL = 20
SWEEP_COOLDOWN = 45

# 冬瓜管理后台前端的“浏览器版本过低”UA 检测（minified），反代时改写为恒通过。
UA_CHECK_RE = re.compile(
    rb"return!\(t&&parseInt\(t\[1\],10\)<\d+\|\|n&&parseInt\(n\[1\],10\)<\d+\|"
    rb"\|r&&parseInt\(r\[1\]\|\|r\[2\],10\)<\d+\|\|o&&parseInt\(o\[1\],10\)<\d+\)"
)

_lock = threading.Lock()
_cache = {"ip": None, "mac": None, "ts": 0.0, "sweep_ts": 0.0, "source": None,
          "vnc_fail_ts": 0.0}

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


def vm_running():
    return "running" in _sh(f"virsh -c qemu:///system domstate {VM_NAME}", 8)


def vm_mac():
    xml = _sh(f"virsh -c qemu:///system dumpxml {VM_NAME}", 8)
    m = re.search(r"<mac address='([0-9a-f:]{17})'", xml)
    if m:
        return m.group(1)
    m = re.search(r"52:54:(?:[0-9a-f]{2}:){3}[0-9a-f]{2}", xml)
    return m.group(0) if m else None


def arp_lookup(mac):
    if not mac:
        return None
    try:
        with open("/proc/net/arp") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[3].lower() == mac.lower():
                    return parts[0]
    except OSError:
        pass
    return None


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


def mdns_probe():
    if not shutil.which("avahi-resolve"):
        return None
    out = _sh(f"avahi-resolve -4 -n {MDNS_NAME}", 8)
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
    if not m:
        return None
    ip = m.group(1)
    if ip in local_ips():
        return None
    if tcp_open(ip, ADMIN_PORT, 0.6) or tcp_open(ip, HA_PORT, 0.6):
        return ip
    return None


def ping_sweep():
    """对物理/LAN 接口的子网做快速 ping 扫描，逼出 ARP 表。
    跳过 docker/virbr 等虚拟网桥；修复旧版 split()[0] 取到 'inet' 导致
    扫描从未真正执行的 bug。"""
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
    hits = []
    def probe(ip):
        return ip if tcp_open(ip, ADMIN_PORT, 0.4) else None
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
    if tcp_open(ip, ADMIN_PORT, 0.6) or tcp_open(ip, HA_PORT, 0.6):
        return ip
    _cache["vnc_fail_ts"] = now
    return None


def resolve_ip():
    now = time.time()
    with _lock:
        # 缓存命中：TCP 快速验证后直接采用
        if (_cache["ip"] and now - _cache["ts"] < CACHE_TTL
                and vm_running()
                and (tcp_open(_cache["ip"], ADMIN_PORT) or tcp_open(_cache["ip"], HA_PORT))):
            return _cache["ip"], _cache["source"]
        if not vm_running():
            _cache["ip"] = None
            return None, "stopped"
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
        if ip:
            _cache.update(ip=ip, mac=mac, ts=now, source=source)
        else:
            _cache["ip"] = None
        return ip, source


PAGE_TMPL = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">\
<meta name="viewport" content="width=device-width,initial-scale=1">\
$REFRESH<title>冬瓜HAOS · IP 寻踪</title><style>\
body{font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;\
background:#0f1720;color:#e6edf3;display:flex;align-items:center;\
justify-content:center;min-height:100vh;margin:0}\
.c{max-width:520px;padding:40px 32px;background:#18222e;border-radius:16px;\
box-shadow:0 8px 30px rgba(0,0,0,.4);text-align:center}\
h1{font-size:22px;margin:0 0 12px}p{color:#9fb0c0;line-height:1.7;margin:8px 0}\
code{background:#0d1520;padding:2px 8px;border-radius:6px;color:#7fd1ae}\
.s{margin-top:18px;font-size:13px;color:#5c6f80}</style></head>\
<body><div class="c">$BODY<div class="s">端口 $PORT · 管理后台 :$ADMIN / HA :$HA / 固定入口，地址变化自动跟随</div></div></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "DongGuaHaFinder/1.1"

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _page(self, title, msg, refresh=0):
        rf = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
        body = f"<h1>🎃 {title}</h1><p>{msg}</p>"
        html = PAGE_TMPL.replace("$REFRESH", rf).replace("$BODY", body)
        html = html.replace("$PORT", str(PORT))
        html = html.replace("$ADMIN", str(ADMIN_PORT)).replace("$HA", str(HA_PORT))
        return html.encode("utf-8")

    def _redirect(self, ip, port, path):
        target = f"http://{ip}:{port}{path}"
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        path = self.path
        pure = path.split("?")[0]
        if pure in ("/healthz", "/api/healthz"):
            self._send(200, b"ok", "text/plain")
            return
        if pure == "/api/status":
            ip, source = resolve_ip()
            payload = json.dumps(
                {"running": bool(ip) or vm_running(), "ip": ip, "source": source,
                 "admin_port": ADMIN_PORT, "ha_port": HA_PORT},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
            return
        ip, _src = resolve_ip()
        if ip:
            if pure == "/ha" or pure.startswith("/ha/"):
                tail = pure[3:] or "/"
                self._redirect(ip, HA_PORT, tail + ("?" + path.split("?", 1)[1] if "?" in path else ""))
            elif pure.startswith("/api/"):
                self._send(404, b'{"error":"not found"}', "application/json")
            else:
                # 默认落点：冬瓜管理后台——走本机反代（去掉"浏览器版本过低"横幅）
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0] or self.server.server_address[0]
                self._redirect(host, ADMIN_PROXY_PORT,
                               (pure or "/") + ("?" + path.split("?", 1)[1] if "?" in path else ""))
            return
        if not vm_running():
            self._send(200, self._page(
                "虚拟机未在运行",
                "请先在 <b>「虚拟机」应用</b> 中启动 <code>haos</code> 虚拟机。"
                "本页面每 5 秒自动重试。", refresh=5))
        else:
            self._send(200, self._page(
                "已运行，正在寻踪 IP…",
                "刚启动时获取 DHCP 地址需要时间（冬瓜首启拉取镜像可能需 15~40 分钟，后续开机 2~10 分钟）。"
                "寻踪会自动尝试 MAC/ARP、mDNS 与端口探测，每 5 秒重试。", refresh=5))

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        pass


HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
              "content-length"}


class ProxyHandler(Handler):
    """反向代理冬瓜管理后台（VM:8124），透传的同时改写前端 UA 检测，去掉横幅提示。"""

    server_version = "DongGuaHaProxy/1.0"

    def _proxy(self):
        ip, _src = resolve_ip()
        if not ip:
            if not vm_running():
                self._send(200, self._page("虚拟机未在运行",
                    "请先在 <b>「虚拟机」应用</b> 中启动 <code>haos</code> 虚拟机。", refresh=5))
            else:
                self._send(200, self._page("正在寻踪 IP…", "管理后台地址尚未确定，稍候自动重试。", refresh=5))
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
                    if k.lower() not in ("host", "connection", "accept-encoding", "content-length")}
            hdrs["Host"] = f"{ip}:{ADMIN_PORT}"
            hdrs["Connection"] = "close"
            conn = http.client.HTTPConnection(ip, ADMIN_PORT, timeout=20)
            conn.request(self.command, self.path, body=body, headers=hdrs)
            r = conn.getresponse()
            data = r.read()
            ctype = (r.getheader("Content-Type") or "").lower()
            if "javascript" in ctype or "ecmascript" in ctype:
                data, n = UA_CHECK_RE.subn(b"return!0", data)
                if n:
                    print(f"[dongguaha-web] ua-check patched x{n} in {self.path.split('?')[0]}")
            self.send_response(r.status)
            for k, v in r.getheaders():
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
            self._send(502, self._page("管理后台暂不可达",
                f"无法连接 <code>{ip}:{ADMIN_PORT}</code>（{type(e).__name__}），若系统刚启动请稍后重试。", refresh=5))
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
    print(f"[dongguaha-web] {PORT} finder, {ADMIN_PROXY_PORT} admin-proxy -> vm '{VM_NAME}' admin:{ADMIN_PORT} ha:{HA_PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
