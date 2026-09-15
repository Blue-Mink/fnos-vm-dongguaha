# 🏠 fnos-vm-dongguaha

![GitHub release](https://img.shields.io/github/v/release/Blue-Mink/fnos-vm-dongguaha?style=flat-square)
![Platform](https://img.shields.io/badge/platform-fnOS%20x86_64-blue?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![HAOS](https://img.shields.io/badge/HAOS-18.2%20%7C%2018.1%20%7C%2018.0%20%7C%2017.3.1-red?style=flat-square)

> 在 x86 fnOS 系统中创建冬瓜HAOS虚拟机。
> 内置「IP 寻踪」固定入口：应用中心/桌面图标一键打开 Home Assistant，无需关心虚拟机 IP 变化。

---

## ✨ 特性

- 🎛️ **向导选配置**：安装时自选 CPU / 内存 / 磁盘 / HAOS 版本（下拉选择），参数直通虚拟机真实配置
- 🔍 **IP 寻踪**：应用中心图标的「打开」按钮固定指向 `http://NAS_IP:36123/`，自动追踪虚拟机当前 IP；发现链六级：MAC→ARP → **VNC 横幅直读**（OCR 虚拟机控制台横幅上的 IP，亚秒级）→ mDNS(homeassistant.local) → virsh domifaddr → 局域网扫描 → **:8124 端口探测补齐**，DHCP 地址变化无感跟随
- 🖥️ **桌面双入口**：安装后飞牛桌面出现两个图标——**冬瓜HAOS**（→ 冬瓜管理后台 `:8124`，经反代优化）与 **Home Assistant**（`/ha` → Home Assistant `:8123`），均自动跟随虚拟机 IP 变化
- 🚫 **去横幅**：管理后台经本机 `36124` 反向代理，自动改写前端的「浏览器版本过低」UA 检测（QQ 浏览器等低版本号 UA 不再误弹横幅）
- ⚡ **秒回安装**：提交即完成，886~904 MB 镜像的下载、三重完整性校验与建机在后台进行，进度可查、失败可续跑
- ⏻ **与应用中心联动**：点「停用」即向虚拟机发 ACPI 优雅关机，从入口页开机也会把应用中心状态扳回「运行中」，不会出现「虚拟机在跑却关不掉」
- 🔌 **网络修复页**：全部经虚拟机串口执行（重新获取地址 / 重启网络 / 设静态 IP / 恢复自动获取 / 手动跳转），路由器不给 DHCP 时也能进去，不依赖任何网络前提
- 💾 **换版本不丢数据**：磁盘版本会被记录，换版本先把旧盘整块留档到 `/vol1/vm/backup` 再灌所选版本；选同一版本原盘复用，重装沿用同一网卡 MAC 与 UUID
- 🖲️ **反代下按钮也直达**：冬瓜管理后台里的「HA 登录页 / TTYD」按钮自动改写回虚拟机真实地址，不会落到飞牛登录页或空端口
- 🖥️ 虚拟机未在运行时，入口页一键开机并显示启动进度（已关机 / 正在启动 / 正在获取地址 / Home Assistant 启动中）
- 🧩 冬瓜HAOS 版本可选：18.2 / 18.1 / 18.0 / 17.3.1（镜像均来自冬瓜官方 CDN）

---

## 📦 安装

1. 下载 [com.dongguaha.vm-18.2.7-fnos-amd64.fpk](https://github.com/Blue-Mink/fnos-vm-dongguaha/releases/download/v18.2.7/com.dongguaha.vm-18.2.7-fnos-amd64.fpk)（[SHA256](https://github.com/Blue-Mink/fnos-vm-dongguaha/releases/download/v18.2.7/com.dongguaha.vm-18.2.7-fnos-amd64.fpk.sha256)：`d18f108c83a6b713a6aebeb121d8c86627728af6c441605cd27538ce173503d8`）
2. 在飞牛 NAS 应用中心选择「从文件安装」，按向导选择 CPU / 内存 / 磁盘 / HAOS 版本
3. 安装完成后点应用图标打开入口页，页面上「启动虚拟机」即可一键开机（也可照旧在「虚拟机」应用里操作）
4. **打开 Web 界面（三选一）**：
   - 应用中心冬瓜HAOS图标 → 「打开」按钮
   - 桌面图标：**冬瓜HAOS**（管理后台）/ **Home Assistant**（HA 界面）
   - 浏览器直接访问 `http://NAS_IP:36123/`
   默认经 `36124` 反代进入**冬瓜管理后台**（已去除浏览器版本横幅）；`http://NAS_IP:36123/ha` 直达 **Home Assistant `:8123`**
5. 首次启动较慢（见下方注意事项），虚拟机就绪后会自动跟随其 IP 变化

---

## 📱 手机端 Home Assistant 客户端

| 平台 | 下载 | 说明 |
|------|------|------|
| Android (APK) | [下载 APK](https://github.com/Blue-Mink/fnos-vm-dongguaha/releases/download/v18.2/Home-Assistant.apk) | 本地安装包 |
| Android (Google Play) | [Google Play](https://play.google.com/store/apps/details?id=io.homeassistant.companion.android) | 官方商店 |
| iOS | [App Store](https://apps.apple.com/cn/app/home-assistant/id1099568401) | 官方商店 |

---

## ⚙️ 系统要求

| 项目 | 要求 | 备注 |
|------|------|------|
| 系统 | fnOS x86_64 | 64 位系统 |
| 依赖应用 | `trim.vm` | 必备虚拟机组件 |
| CPU | 2 核 | 推荐 2 核以上 |
| 内存 | 2 GB | 推荐 4 GB 更流畅 |
| 磁盘 | 32 GB | 建议 SSD |

---

## 🛠️ 技术栈

| 组件 | 版本/说明 |
|------|----------|
| 虚拟化 | KVM 硬件加速 |
| 操作系统 | Home Assistant OS（冬瓜优化版）18.2 / 18.1 / 18.0 / 17.3.1 |
| IP 寻踪 | Python3 标准库 HTTP 转发器（`app/bin/haos-web-redirect.py`，入口 36123 + 后台反代 36124，systemd 单元 `dongguaha-web` 托管） |
| 管理后台反代 | 同进程 36124 端口，透传 VM:8124 并改写前端 UA 检测（无 WebSocket，无损代理） |
| 后台安装任务 | `app/bin/haos-install-worker.sh`（安装回调秒回，下载/校验/入池/建机在后台推进状态机）|
| 打包规范 | 飞牛 fnOS FPK 应用规范 |

---

## ⚠️ 注意事项

- **首次启动很慢是正常现象**：冬瓜HAOS 的 Supervisor 首启需要从 `r.hassbus.com` 拉取全套运行镜像，视网络可能需要 **15~40 分钟**；期间 `:8123` 无响应、IP 寻踪页会显示"寻找中"。重启虚拟机会快很多（镜像已落盘）
- 若 `:8123` 长时间无响应而 `:8124` 已通：多为冬瓜镜像 landingpage 容器退出（冬瓜镜像内部行为），重启虚拟机通常可恢复
- 安装向导里的 CPU/内存/磁盘会真实写入虚拟机配置（`virsh dumpxml` 与虚拟机应用内可见）；HAOS 版本在安装后不可变更，换版本在卸载重装时于向导里选即可——旧磁盘会整块留档到 `/vol1/vm/backup`，同一版本则原盘复用（跳过下载与预置，约 10~40 秒装完）
- `36123`（寻踪入口/跳转）与 `36124`（管理后台反代去横幅）同由 `dongguaha-web.service` 监听，只用于发现、跳转与页面改写，不提供业务数据；直连 `VM_IP:8124` 仍可用但没有去横幅效果；面板里的「HA 登录页 / TTYD」按钮与 `/ha` 入口都是**浏览器直连虚拟机 IP**，跨网段、VPN 之外或 AP 隔离时会打不开（`36123/36124` 两个入口本身只要到得了 NAS 就可用）
- 请勿直接修改虚拟机配置文件，应通过应用中心管理
- 点「停用」后应用中心会立刻记为已停用，虚拟机真正落定还需 **45~70 秒**（HAOS 要逐个停掉核心 / Supervisor / 插件容器），期间别急着再点启用
- 向导里磁盘填 `0` 表示直通本机空闲整盘；HAOS 首次启动会自己把数据区扩满
- 虚拟机网卡 MAC 与 UUID 在重装时会沿用（另存 `/vol1/vm/haos.vm-net`），路由器上按 MAC 的绑定不会因重装失效
- 如遇网络问题，请检查 NAS 与虚拟机的网络配置（虚拟机使用 OVS 网桥直通局域网）

---

## 🔨 构建

### 环境准备

| 工具 | 版本 | 说明 |
|------|------|------|
| fnpack | 最新版 | fnOS FPK 打包工具 |
| Python | 3.8+ | 用于打包脚本 |
| tar / gzip | 系统自带 | 用于打包 app.tgz |

> **提示**：推荐在飞牛 NAS 或 Docker 容器中构建，以确保打包规范兼容。

### 构建步骤

1. **克隆仓库**

```bash
git clone https://github.com/Blue-Mink/fnos-vm-dongguaha.git
cd fnos-vm-dongguaha
```

2. **安装 fnpack**

```bash
# 在飞牛 NAS 上安装
apt update && apt install -y fnpack

# 或使用 Docker
docker run --rm -v $(pwd):/work -w /work alpine:latest sh -c "apk add --no-cache fnpack && fnpack build -d ."
```

3. **修改 manifest 版本号（可选）**

如需要发布新版本，编辑 `manifest` 文件中的 `version` 字段：

```ini
version = 18.2.7
```

4. **执行打包**

```bash
# 目录结构（app/ 为载荷源，fnpack 自动打包成 app.tgz，无需手工维护）
ls -la app/bin app/ui cmd/ config/ wizard/ ICON.PNG ICON_256.PNG manifest

# 执行打包（fnpack 会在当前目录生成 com.dongguaha.vm.fpk）
fnpack build -d .
mv com.dongguaha.vm.fpk dist/com.dongguaha.vm-18.2.7-fnos-amd64.fpk
```

5. **验证构建产物**

```bash
# 检查文件大小（正常约 100KB）
ls -lh dist/com.dongguaha.vm-18.2.7-fnos-amd64.fpk

# 查看包内结构
tar -tzf dist/com.dongguaha.vm-18.2.7-fnos-amd64.fpk | head -20
```

### 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| `fnpack: command not found` | fnpack 未安装 | 在飞牛 NAS 上执行 `apt install fnpack` |
| `Permission denied` | 目录权限不足 | 使用 `sudo` 或在有写权限目录下构建 |
| `checksum mismatch` | 文件被篡改 | 重新执行打包，确保 `app.tgz` 未被修改 |
| `manifest 缺失字段` | manifest 格式错误 | 对照飞牛 FPK 规范检查 manifest 文件 |

### 构建产物说明

打包完成后，`dist/` 目录下会生成：

```
dist/
└── com.dongguaha.vm-18.2.7-fnos-amd64.fpk  # 可直接安装的 FPK 包
```

可直接在飞牛 NAS 应用中心「从文件安装」测试。

---

## 📄 许可证

MIT

## 👤 作者

[Blue-Mink](https://github.com/Blue-Mink)  
https://github.com/Blue-Mink/FnDepot

---

## 🙏 鸣谢

- [Home Assistant](https://www.home-assistant.io/) — 开源智能家居平台
- [冬瓜HAOS的由来](https://bbs.hassbian.com/thread-24065-1-1.html)
- [冬瓜HAOS 镜像包](https://bbs.hassbian.com/thread-23791-1-1.html) — 镜像包下载
- [RROrg/fn-apps](https://github.com/RROrg/fn-apps/tree/main/fn-vfnOS) — 项目参考
