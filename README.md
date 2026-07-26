# 🏠 fnos-vm-dongguaha

![GitHub release](https://img.shields.io/github/v/release/Blue-Mink/fnos-vm-dongguaha?style=flat-square)
![Platform](https://img.shields.io/badge/platform-fnOS%20x86_64-blue?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![HA](https://img.shields.io/badge/Home%20Assistant-18.0-red?style=flat-square)

> 在 x86 fnOS 系统中创建同架构的 **冬瓜HAOS** 虚拟机，享受 KVM 硬件加速性能。

---

## 📦 安装

1. 下载 [com.dongguaha.vm-18.0-fnos-amd64.fpk](https://github.com/Blue-Mink/fnos-vm-dongguaha/releases/download/v18.0/com.dongguaha.vm-18.0-fnos-amd64.fpk)
2. 在飞牛 NAS 应用中心选择「从文件安装」
3. 安装完成后，在「虚拟机」应用中启动 / 停止虚拟机
4. VNC 中查看虚拟机 IP，通过 `http://虚拟机IP:8123` 访问 Home Assistant Web UI
5. 通过 `http://虚拟机IP:8124` 访问冬瓜HAOS伴侣

---

## 📱 手机端 Home Assistant 客户端

| 平台 | 下载 | 说明 |
|------|------|------|
| Android (APK) | [下载 APK](https://github.com/Blue-Mink/fnos-vm-dongguaha/releases/download/v18.0/Home-Assistant.apk) | 本地安装包 |
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
| 操作系统 | Home Assistant OS 18.0 |
| 打包规范 | 飞牛 fnOS FPK 应用规范 |

---

## ⚠️ 注意事项

- 安装后需手动在「虚拟机」应用中启动虚拟机
- 首次启动 Home Assistant 需要完成初始化设置
- 请勿直接修改虚拟机配置文件，应通过应用中心管理
- 如遇网络问题，请检查 NAS 与虚拟机的网络配置

---

## 🔨 构建

```bash
# 解压源码
tar -xzf app.tgz

# 打包
fnpack build -d . -o dist/com.dongguaha.vm-18.0-fnos-amd64.fpk
```

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
