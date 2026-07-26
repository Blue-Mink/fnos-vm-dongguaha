# fnos-vm-dongguaha

在 x86 fnOS 系统中创建同架构的冬瓜HAOS虚拟机，享受KVM硬件加速性能。

## 安装

1. 下载 `dist/com.dongguaha.vm-18.0-fnos-amd64.fpk`
2. 在飞牛NAS应用中心选择「从文件安装」
3. 安装完成后，在「虚拟机」应用中启动/停止虚拟机
4. VNC中查看虚拟机IP，通过 `http://虚拟机IP:8123` 访问Home Assistant Web UI
5. 通过 `http://虚拟机IP:8124` 访问冬瓜HAOS伴侣

## 手机端 Home Assistant 客户端

- [Home Assistant for Android (APK)](https://github.com/Blue-Mink/fnos-vm-dongguaha/releases/download/v18.0/Home-Assistant.apk)

## 系统要求

- fnOS x86_64 系统
- 已安装 `trim.vm` 应用
- 建议配置：2核CPU / 2GB内存 / 32GB磁盘

## 技术栈

- KVM硬件虚拟化
- Home Assistant OS 18.0
- 飞牛fnOS FPK应用规范

## 注意事项

- 安装后需手动在「虚拟机」应用中启动虚拟机
- 首次启动Home Assistant需要完成初始化设置
- 请勿直接修改虚拟机配置文件，应通过应用中心管理

## 构建

本项目使用飞牛fnOS FPK规范构建。如需自行构建：

```bash
# 解压源码
tar -xzf app.tgz

# 打包
fnpack build -d . -o dist/com.dongguaha.vm-18.0-fnos-amd64.fpk
```

## 许可证

MIT

## 作者

Blue-Mink  
https://github.com/Blue-Mink/FnDepot
