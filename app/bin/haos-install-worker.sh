#!/bin/bash
# 冬瓜HAOS FPK - 后台安装 worker（由 install_callback 秒回后异步拉起）
#
# 为什么要秒回：fnOS 平台对安装回调有约 190 秒看门狗，超时直接判
# APP_INSTALL_FAILED_INSTALL_CALLBACK_EXCEPTION 并把 install-fpk 打段错误。
# 冬瓜四个镜像都是 900MB 级（实测 886~904 MB），慢一点的 CDN 就会撞线，
# 所以重活全部挪到这里异步做。
#   进度: /tmp/haos-install.state    日志: /tmp/haos-install.log
set -e
ENV_FILE="${1:-}"
if [ -n "${ENV_FILE}" ] && [ -f "${ENV_FILE}" ]; then set -a; . "${ENV_FILE}"; set +a; fi

STATE_FILE="/tmp/haos-install.state"
LOCK_FILE="/tmp/haos-install.lock"
LOG="${TRIM_TEMP_LOGFILE:-/tmp/haos-install.log}"
set_state() { echo "$1" > "${STATE_FILE}.tmp" && mv -f "${STATE_FILE}.tmp" "${STATE_FILE}"
              echo "[$(date +%T)] STATE: $1" >>"${LOG}" 2>/dev/null || true; }
CUR="启动"
# 注意：脚本里裸 `exit` 不会触发 ERR trap（1.0.x 踩过：静默自杀且状态文件没留痕），
# 所以每条失败路径都要自己把状态写清楚。
trap 'set_state "failed: ${CUR}（详见 ${LOG}）"' ERR
trap 'set_state "failed: 进程被信号终止($1,$$)"; rm -f "${LOCK_FILE}"; exit 1' TERM HUP INT QUIT
trap 'rm -f "${LOCK_FILE}"' EXIT
if [ -f "${LOCK_FILE}" ]; then
    OLD="$(cat "${LOCK_FILE}" 2>/dev/null || true)"
    if [ -n "${OLD}" ] && [ "${OLD}" != "$$" ] && kill -0 "${OLD}" 2>/dev/null; then
        echo "worker 已在运行 (pid ${OLD})，本实例退出"; exit 0
    fi
fi
echo $$ > "${LOCK_FILE}"
set_state "starting"
echo "============================================"
echo "[$(date)] 冬瓜HAOS 后台安装开始 (pid $$)"

# ---- 输入参数 ----
HAOS_VERSION="${wizard_haos_version:-18.2}"
HAOS_CPU="${wizard_haos_cpu:-2}"
HAOS_MEM="${wizard_haos_mem:-2048}"
HAOS_DISK="${wizard_haos_disk:-32}"
HAOS_AUTOSTART="${wizard_haos_autostart:-false}"

# ---- 数值兜底（别信上游）----
# 向导的 pattern 只管 GUI：env 是外部可写的（appcenter-cli install-fpk --env 直接
# 喂），显式传 wizard_haos_disk=0 时上面的默认值不生效，会一路直通
# `qemu-img resize ... 0G` / `virsh vol-create-as ... 0G`，把系统盘建成 0G。
# 2026-09-14 真机装机时就撞过一次 0（VM 未关机导致读不到虚拟大小）。
dgh_clamp() {  # $1=变量名 $2=下限 $3=上限 $4=回退默认
    local name="$1" lo="$2" hi="$3" def="$4" v
    v="${!name}"
    case "$v" in ''|*[!0-9]*) v="$def" ;; esac
    [ "$v" -lt "$lo" ] && v="$lo"
    [ "$v" -gt "$hi" ] && v="$hi"
    if [ "$v" != "${!name}" ]; then
        echo "⚠️ 参数 ${name}=${!name:-空} 不在 ${lo}~${hi} 内，按 ${v} 处理"
    fi
    printf -v "$name" '%s' "$v"
}
dgh_clamp HAOS_CPU 1 16 2
dgh_clamp HAOS_MEM 1024 65536 2048
dgh_clamp HAOS_DISK 16 256 32

# ---- 版本 → 官方不可变 URL + 精确字节数 ----
# 官方（wghaos）不发布校验和，因此这里钉的是「文件名（自带发布时间戳与 git
# 短哈希，不可变）+ 精确字节数」，再叠加下载后的 xz 容器 CRC 自检与 qcow2
# 魔数检查；同时把实际 SHA256 记进日志，跨设备可比对。
vm_case_url() {
    case "$1" in
        18.2)   HAOS_URL="https://fw.wghaos.com/haos/x86-64-vm/haos_x86-64-vm_cn-18.2.release.20260812_172923.517b83c7db.qcow2.xz"
                HAOS_BYTES=904451672 ;;
        18.1)   HAOS_URL="https://fw.wghaos.com/haos/x86-64-vm/haos_x86-64-vm_cn-18.1.release.20260730_195953.0e63feaaf8.qcow2.xz"
                HAOS_BYTES=900117820 ;;
        18.0)   HAOS_URL="https://fw.wghaos.com/haos/x86-64-vm/haos_x86-64-vm_cn-18.0.release.20260709_214639.fd913a0968.qcow2.xz"
                HAOS_BYTES=900466016 ;;
        17.3.1) HAOS_URL="https://fw.wghaos.com/haos/x86-64-vm/haos_x86-64-vm_cn-17.3.1.release.20260617_191651.de9faeae3f.qcow2.xz"
                HAOS_BYTES=885510064 ;;
        *)      return 1 ;;
    esac
}
if ! vm_case_url "${HAOS_VERSION}"; then
    echo "⚠️ 未知版本 ${HAOS_VERSION}（可用: 18.2/18.1/18.0/17.3.1），回退到 18.2"
    HAOS_VERSION="18.2"
    vm_case_url "18.2"
fi
echo ">>> 目标版本 ${HAOS_VERSION}，官方包 ${HAOS_BYTES} 字节"

# ---- 内存再校验（回调已校验过，这里防手工调用）----
MEM_TOTAL_MB=$(awk '/MemTotal/{print int($2/1024); exit}' /proc/meminfo)
MEM_MAX_MB=$((MEM_TOTAL_MB - 1536)); [ "${MEM_MAX_MB}" -gt 65536 ] && MEM_MAX_MB=65536
case "${HAOS_MEM}" in ''|*[!0-9]*) HAOS_MEM=0 ;; esac
if [ "${HAOS_MEM}" -lt 1024 ] || [ "${HAOS_MEM}" -gt "${MEM_MAX_MB}" ]; then
    echo "错误: 内存 ${HAOS_MEM}MB 超出 1024~${MEM_MAX_MB}（本机 ${MEM_TOTAL_MB}MB，需为飞牛留 1536MB）"
    set_state "failed: 内存参数超范围"; exit 1
fi

# ---- 架构 ----
ARCH=$(uname -m)
VM_NAME="haos"
case "${ARCH}" in
    x86_64) QEMU_BIN="qemu-system-x86_64"; UEFI_CODE="/usr/share/OVMF/OVMF_CODE.fd"
            UEFI_VARS="/usr/share/OVMF/OVMF_VARS.fd"; MACHINE="q35" ;;
    *) echo "不支持的架构: ${ARCH}"; set_state "failed: 架构不支持 ${ARCH}"; exit 1 ;;
esac

# ---- 共享路径与网桥 ----
SHARE_PATH=""
for p in $(echo "${TRIM_DATA_SHARE_PATHS:-}" | tr ':' ' '); do
    [ -d "${p}" ] && { SHARE_PATH="${p}"; break; }
done
[ -z "${SHARE_PATH}" ] && { echo "错误: 未找到数据共享路径"; set_state "failed: 无数据共享路径"; exit 1; }
OVS_BRIDGE="$(ovs-vsctl list-br 2>/dev/null | head -n 1)"
[ -z "${OVS_BRIDGE}" ] && { echo "错误: 未检测到 OVS 网桥"; set_state "failed: 无 OVS 网桥"; exit 1; }

XCOW_NOTE=""
QCOW2_FILE="${SHARE_PATH}/haos.qcow2"
XZ_FILE="${SHARE_PATH}/haos.qcow2.xz"
# 磁盘位置必须在这里就参与判断：装完机后磁盘会被移进 libvirt 存储池，
# 若只认共享目录，重装就会走「重新下载 + vol-delete 覆盖」把用户的盘刷掉。
POOL_NAME="vol1"
POOL_PATH="/vol1/vm/pool"
POOL_QCOW2="${POOL_PATH}/haos.qcow2"
# 磁盘里装的是哪个 HAOS 版本：记在池目录之外——目录型存储池会把多出来的文件
# 当成卷列出来，虚拟机界面会冒出乱七八糟的磁盘（iStoreOS 同款坑）。
DISK_MARK_FILE="/vol1/vm/haos.disk-version"
DISK_BACKUP_DIR="/vol1/vm/backup"
VM_NET_FILE="/vol1/vm/haos.vm-net"

# 已有磁盘里是什么版本：先读标记；老装机没标记就退回虚拟机定义里的 osVersion。
existing_disk_version() {
    if [ -s "${DISK_MARK_FILE}" ]; then
        head -n1 "${DISK_MARK_FILE}" | tr -d ' \t\r'
        return 0
    fi
    virsh -c qemu:///system dumpxml "${VM_NAME}" 2>/dev/null | \
        sed -n 's:.*Home Assistant OS \([0-9.]\{1,\}\).*:\1:p' | head -1
}

# ---- 步骤1: 已有磁盘的判断（复用 / 换版本留档），没盘才下载 ----
# 置全局 SKIP_PROV（1=本轮不重装磁盘）与 QCOW2_FILE（本轮要用的磁盘路径）
# 选盘优先级：虚拟机定义里真正挂着的那块 → 存储池里的 → 共享目录里的。
# 顺序很关键：18.2 及之前的装机用 cp 入池，共享目录里会留一份从此不再更新的
# 过期副本；先认共享目录就会把虚拟机指到那块旧盘上，用户的系统「看起来回档了」
# ——18.2.1 首测在真机踩过一次，因此把这条顺序钉死并在注释里留下原因。
handle_existing_disk() {
    LIVE=""
    DOMSRC="$(virsh -c qemu:///system dumpxml "${VM_NAME}" 2>/dev/null | \
              sed -n "s:.*<source file='\([^']*\)'.*:\1:p" | head -1)"
    if [ -n "${DOMSRC}" ] && [ -s "${DOMSRC}" ]; then LIVE="${DOMSRC}"; fi
    [ -z "${LIVE}" ] && [ -s "${POOL_QCOW2}" ] && LIVE="${POOL_QCOW2}"
    [ -z "${LIVE}" ] && [ -s "${QCOW2_FILE}" ] && LIVE="${QCOW2_FILE}"
    [ -z "${LIVE}" ] && return 0
    DISK_VER="$(existing_disk_version)"
    if [ -n "${DISK_VER}" ] && [ "${DISK_VER}" != "${HAOS_VERSION}" ]; then
        # 向导里明确选了另一个版本 → 真的换系统：旧盘整块改名留档再灌新盘。
        # 旧写法是 `if [ -f haos.qcow2 ]; then 跳过下载`，换版本被静默忽略，
        # 选 17.3.1 装完还是原来的 18.2。
        CUR="换版本留档旧盘"; set_state "swap-disk ${DISK_VER}->${HAOS_VERSION}"
        echo ">>> 步骤1: 磁盘里是 ${DISK_VER}，本次要装 ${HAOS_VERSION} → 换盘"
        mkdir -p "${DISK_BACKUP_DIR}"
        SWAP_FILE="${DISK_BACKUP_DIR}/haos.qcow2.swap-${DISK_VER}-$(date +%Y%m%d%H%M%S)"
        mv "${LIVE}" "${SWAP_FILE}"
        echo "    旧盘已留档：${SWAP_FILE}"
        rm -f "${DISK_MARK_FILE}"
        virsh pool-refresh "${POOL_NAME}" 2>/dev/null || true
        return 0
    fi
    CUR="复用已有磁盘"; set_state "reuse-disk${DISK_VER:+ ${DISK_VER}}"
    echo ">>> 步骤1: 跳过下载（复用 ${LIVE}，磁盘版本=${DISK_VER:-未知}）"
    [ -z "${DISK_VER}" ] && {
        echo "    ⚠️ 判断不出这块盘里的版本（老装机没写版本标记），按原样保留不覆盖"
        echo "    ⚠️ 因此本次「换版本」不会生效；确实要换系统请先删除该磁盘"
        echo "       （virsh vol-delete --pool ${POOL_NAME} haos.qcow2）再重装"
        set_state "reuse-disk 版本未知(换版本不会生效)"
    }
    QCOW2_FILE="${LIVE}"
    SKIP_PROV=1
    # 另一处若还留着同名磁盘，那是历史遗留副本：改名让位（不删），
    # 免得下一次装机又把它错认成系统盘。
    for other in "${SHARE_PATH}/haos.qcow2" "${POOL_QCOW2}"; do
        [ "${other}" = "${QCOW2_FILE}" ] && continue
        [ -s "${other}" ] || continue
        KEEP="${other}.superseded-$(date +%Y%m%d%H%M%S)"
        mv "${other}" "${KEEP}"
        echo "    ⚠️ 另一处遗留的同名磁盘不是虚拟机在用的那块，已改名让位：${KEEP}"
    done
    if [ ! -s "${DISK_MARK_FILE}" ] && [ -n "${DISK_VER}" ]; then
        echo "${DISK_VER}" > "${DISK_MARK_FILE}"
        echo "    补写版本标记：${DISK_MARK_FILE} = ${DISK_VER}"
    fi
}
SKIP_PROV=0
DISK_VER=""
handle_existing_disk


# ---- 步骤2~4: 下载 / 完整性校验 / 解压扩容（仅全新装或换版本时）----
if [ "${SKIP_PROV}" != "1" ]; then
    CUR="下载镜像 v${HAOS_VERSION}"; set_state "downloading ${HAOS_VERSION}"
    echo ">>> 步骤2: 下载冬瓜HAOS v${HAOS_VERSION}（约 $((HAOS_BYTES/1024/1024))MB）"
    cd "${SHARE_PATH}"
    dl_ok=0
    for t in 1 2 3; do
        if [ "${t}" -eq 1 ]; then
            # 首轮允许断点续传；后续轮次一律整包重下——跨代理续传拼出来的
            # 文件可能大小对、内容错（iStoreOS 踩过：只有整包重下才可信）
            curl -skL --connect-timeout 30 -C - -e https://www.wghaos.com \
                 -o "$(basename "${XZ_FILE}")" "${HAOS_URL}" && dl_ok=1 && break
        else
            rm -f "${XZ_FILE}"
            echo "第 ${t} 次下载：丢弃残留整包重下..."
            curl -skL --connect-timeout 30 -e https://www.wghaos.com \
                 -o "$(basename "${XZ_FILE}")" "${HAOS_URL}" && dl_ok=1 && break
        fi
        echo "下载第 ${t} 次失败，稍后重试"; sleep 3
    done
    [ "${dl_ok}" = "1" ] || { echo "错误: 镜像下载失败"; set_state "failed: 下载失败"; exit 1; }

    CUR="镜像完整性校验"; set_state "verifying"
    echo ">>> 步骤3: 完整性校验（大小 → xz 容器 CRC → qcow2 魔数）"
    ACT_BYTES=$(stat -c %s "${XZ_FILE}")
    if [ "${ACT_BYTES}" != "${HAOS_BYTES}" ]; then
        echo "错误: 下载大小 ${ACT_BYTES} != 官方 ${HAOS_BYTES}（传输被截断或被替换）"
        rm -f "${XZ_FILE}"; set_state "failed: 下载大小不符 ${ACT_BYTES}"; exit 1
    fi
    if ! xz -t "${XZ_FILE}"; then
        echo "错误: xz 容器自检失败（文件损坏）"; rm -f "${XZ_FILE}"
        set_state "failed: xz 自检失败"; exit 1
    fi
    echo "实际 SHA256（跨设备可比对）: $(sha256sum "${XZ_FILE}" | awk '{print $1}')"

    CUR="解压镜像"; set_state "extracting"
    echo ">>> 步骤4: 解压 qcow2.xz"
    rm -f "${QCOW2_FILE}"
    xz -d -c "${XZ_FILE}" > "${QCOW2_FILE}" || {
        rc=$?; echo "错误: xz 解压失败 rc=${rc}"; set_state "failed: 解压失败"; exit 1; }
    rm -f "${XZ_FILE}"
    head -c 4 "${QCOW2_FILE}" | grep -q 'QFI' || {
        echo "错误: 解出来的不是 qcow2（魔数不对）"; set_state "failed: 镜像魔数不对"; exit 1; }

    CUR="扩容磁盘"; set_state "resizing ${HAOS_DISK}G"
    echo ">>> 步骤5: 扩容到 ${HAOS_DISK}G"
    # 必须按**字节**比：`qemu-img info` 的人读输出是 "virtual size: 4 GiB (4294967296 bytes)"，
    # 早先用 grep -o "virtual size: [0-9]*" | awk '{print $3}' 取到的其实是 GiB 那个数字
    # （4 GiB → 4），拿去和 HAOS_DISK*1073741824 比永远为假 —— “已够大就跳过扩容”成了死
    # 代码，真遇到盘不小于目标时反而会去 resize（qcow2 不能缩容）→ 误报「磁盘扩容失败」。
    # 改读 JSON 里的 virtual-size（本文件下面算 DISK_GB 用的就是它）。
    CUR_BYTES=$(qemu-img info "${QCOW2_FILE}" --output json 2>/dev/null | \
        python3 -c "import sys,json; print(int(json.load(sys.stdin).get('virtual-size') or 0))" 2>/dev/null)
    # 解析不出来就当 0，老老实实走扩容分支，别拿脏值去比大小
    case "${CUR_BYTES}" in ''|*[!0-9]*) CUR_BYTES=0 ;; esac
    WANT_BYTES=$(( HAOS_DISK * 1073741824 ))
    if [ -n "${CUR_BYTES}" ] && [ "${CUR_BYTES}" -ge "${WANT_BYTES}" ]; then
        # qcow2 不能缩容：现有盘已经不小于所选大小就跳过，别把失败留着往下走
        echo "现有虚拟大小 ${CUR_BYTES}B 已不小于 ${WANT_BYTES}B，跳过扩容"
    elif qemu-img resize "${QCOW2_FILE}" "${HAOS_DISK}G" >/dev/null; then
        echo "✅ 已扩容到 ${HAOS_DISK}G"
    else
        echo "错误: 磁盘扩容失败（${HAOS_DISK}G）"; set_state "failed: 磁盘扩容失败"; exit 1
    fi

    # ---- 磁盘入 libvirt 存储池（修「虚拟机界面显示 0MB」）----
    if virsh pool-info "${POOL_NAME}" &>/dev/null; then
        CUR="磁盘入存储池"; set_state "provisioning"
        echo ">>> 步骤6: 注册磁盘到 ${POOL_NAME} 存储池"
        virsh vol-delete --pool "${POOL_NAME}" haos.qcow2 2>/dev/null || true
        virsh vol-create-as "${POOL_NAME}" haos.qcow2 "${HAOS_DISK}G" --format qcow2
        # 移进池里并删掉共享目录副本：留着就是白占一份空间（旧写法用 cp）
        mv -f "${QCOW2_FILE}" "${POOL_QCOW2}"
        chown libvirt-qemu:libvirt-qemu "${POOL_QCOW2}" 2>/dev/null || true
        virsh pool-refresh "${POOL_NAME}" || true
        QCOW2_FILE="${POOL_QCOW2}"
        echo "✅ 磁盘路径 -> ${QCOW2_FILE}"
    else
        echo "⚠️ 存储池 ${POOL_NAME} 不存在，跳过注册（用共享目录里的磁盘）"
    fi
    echo "${HAOS_VERSION}" > "${DISK_MARK_FILE}"
    echo ">>> 记录磁盘版本标记：${DISK_MARK_FILE} = ${HAOS_VERSION}"
fi

DISK_GB=$(qemu-img info "${QCOW2_FILE}" --output json | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d[\"virtual-size\"]/1024/1024/1024:.1f}')")
echo "磁盘实际容量: ${DISK_GB} GB"

# ---- 沿用虚拟机身份：重装/换版本都不换网卡 MAC 与 UUID ----
# 换 MAC 会让路由器按 MAC 的绑定与租约全部作废，HAOS 也按 MAC 认网口，
# 表现就是「IP 寻踪突然失效」（iStoreOS 同款教训）。卸载会 undefine 掉
# domain，所以 MAC 必须另外落盘保存。
CUR="生成虚拟机定义"; set_state "defining-vm"
OLD_XML="$(virsh -c qemu:///system dumpxml "${VM_NAME}" 2>/dev/null || true)"
VM_UUID="$(printf '%s' "${OLD_XML}" | sed -n 's:.*<uuid>\([^<]*\)</uuid>.*:\1:p' | head -1)"
MAC_ADDR="$(printf '%s' "${OLD_XML}" | sed -n "s:.*<mac address='\([^']*\)'.*:\1:p" | head -1)"
if [ -z "${MAC_ADDR}" ] && [ -s "${VM_NET_FILE}" ]; then
    MAC_ADDR="$(head -n1 "${VM_NET_FILE}" | tr -d ' \t\r')"
fi
[ -n "${MAC_ADDR}" ] && echo ">>> 沿用虚拟机标识 mac=${MAC_ADDR}${VM_UUID:+ uuid=${VM_UUID}}"
[ -n "${VM_UUID}" ] || VM_UUID="$(uuidgen)"
if ! printf '%s' "${MAC_ADDR}" | grep -qE '^52:54:[0-9a-f]{2}(:[0-9a-f]{2}){3}$'; then
    MAC_ADDR="52:54:$(printf '%02x:%02x:%02x:%02x' $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))"
    echo ">>> 首次装机，随机生成 mac=${MAC_ADDR}"
fi
printf '%s\n' "${MAC_ADDR}" > "${VM_NET_FILE}"

# ---- 虚拟机 XML（含飞牛「虚拟机」应用要读的 metadata）----
XML_FILE="${SHARE_PATH}/haos.xml"
cat > "${XML_FILE}" << 'XMLBODY'
<domain type='kvm'>
  <name>VM_NAME_PH</name>
  <title>冬瓜HAOS</title>
  <uuid>UUID_PH</uuid>
  <memory unit='MiB'>MEM_PH</memory>
  <currentMemory unit='MiB'>MEM_PH</currentMemory>
  <vcpu placement='static'>CPU_PH</vcpu>
  <os>
    <type arch='ARCH_PH' machine='MACHINE_PH'>hvm</type>
    <loader readonly='yes' type='pflash'>UEFI_CODE_PH</loader>
    <nvram>NVRAM_PH</nvram>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough' check='none'/>
  <clock offset='utc'>
    <timer name='rtc' tickpolicy='catchup'/>
    <timer name='pit' tickpolicy='delay'/>
    <timer name='hpet' present='no'/>
  </clock>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <emulator>/usr/bin/QEMU_BIN_PH</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='writeback' io='threads'/>
      <source file='DISK_PATH_PH'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='bridge'>
      <mac address='MAC_PH'/>
      <source bridge='OVS_PH'/>
      <virtualport type='openvswitch'/>
      <model type='virtio'/>
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <graphics type='vnc' socket='VNC_SOCKET_PH' autoport='yes' power-control='on'>
      <listen type='socket'/>
    </graphics>
    <video><model type='virtio' heads='1' vram='16384'/></video>
    <memballoon model='virtio'><stats period='10'/></memballoon>
  </devices>
  <metadata>
    <customMeta xmlns="customMeta">
      <title xmlns="title">冬瓜HAOS</title>
      <osType xmlns="osType">linux</osType>
      <osVersion xmlns="osVersion">Home Assistant OS VERSION_PH</osVersion>
      <diskSize xmlns="diskSize">DISKGB_PH</diskSize>
      <autostart xmlns="autostart">AUTOSTART_PH</autostart>
      <createdTime xmlns="createdTime">CTIME_PH</createdTime>
    </customMeta>
  </metadata>
</domain>
XMLBODY

sed -i "s|VM_NAME_PH|${VM_NAME}|g; s|UUID_PH|${VM_UUID}|g; s|MEM_PH|${HAOS_MEM}|g;
        s|CPU_PH|${HAOS_CPU}|g; s|ARCH_PH|${ARCH}|g; s|MACHINE_PH|${MACHINE}|g;
        s|UEFI_CODE_PH|${UEFI_CODE}|g; s|QEMU_BIN_PH|${QEMU_BIN}|g;
        s|DISK_PATH_PH|${QCOW2_FILE}|g; s|MAC_PH|${MAC_ADDR}|g;
        s|OVS_PH|${OVS_BRIDGE}|g; s|VERSION_PH|${HAOS_VERSION}|g;
        s|DISKGB_PH|${DISK_GB}|g; s|AUTOSTART_PH|${HAOS_AUTOSTART}|g;
        s|CTIME_PH|$(date +%s)|g;
        s|NVRAM_PH|${SHARE_PATH}/haos_VARS.fd|g;
        s|VNC_SOCKET_PH|/var/run/vms/${VM_NAME}.vnc.sock|g" "${XML_FILE}"

mkdir -p /var/run/vms
chmod 777 /var/run/vms 2>/dev/null || true
[ -f "${SHARE_PATH}/haos_VARS.fd" ] || cp "${UEFI_VARS}" "${SHARE_PATH}/haos_VARS.fd"
chown -R libvirt-qemu:kvm "${QCOW2_FILE}" "${SHARE_PATH}/haos_VARS.fd" "${XML_FILE}" 2>/dev/null || true
chmod 644 "${QCOW2_FILE}" "${SHARE_PATH}/haos_VARS.fd" "${XML_FILE}" 2>/dev/null || true

virsh destroy "${VM_NAME}" 2>/dev/null || true
virsh undefine "${VM_NAME}" --nvram 2>/dev/null || true
# undefine --nvram 会把 UEFI 变量一起删掉（老装机沿用过的话会丢 EFI 项），
# 缺了就从现在的固件补一份
[ -f "${SHARE_PATH}/haos_VARS.fd" ] || cp "${UEFI_VARS}" "${SHARE_PATH}/haos_VARS.fd"
virsh define "${XML_FILE}" || { echo "错误: virsh define 失败"; set_state "failed: define 失败"; exit 1; }
if [ "${HAOS_AUTOSTART}" = "true" ]; then
    virsh autostart "${VM_NAME}" || true
else
    virsh autostart --disable "${VM_NAME}" 2>/dev/null || true
fi
echo "✅ 虚拟机已定义（osVersion=Home Assistant OS ${HAOS_VERSION}）"


# ---- 「IP 寻踪」转发器（两个桌面图标的固定落点）----
APP_NAME="com.dongguaha.vm"
WEB_PORT=36123
ADMIN_PROXY_PORT=36124
# 后段用到的变量必须在本段自己解析：秒回模式下这里可能是「复用磁盘」的快路径，
# 前段的赋值不一定存在（iStoreOS 1.0.2 因蹭前段变量把路径拼成 /bin/xxx 静默失败）
APP_DIR="${TRIM_PKGDIR:-${TRIM_APPDEST:-/vol1/@appcenter/${APP_NAME}}}"
[ -f "${APP_DIR}/bin/haos-web-redirect.py" ] || APP_DIR="/var/apps/${APP_NAME}/target"
[ -f "${APP_DIR}/bin/haos-web-redirect.py" ] || APP_DIR="/var/apps/${APP_NAME}"
REDIRECT_SCRIPT="${APP_DIR}/bin/haos-web-redirect.py"
UNIT_FILE="/etc/systemd/system/dongguaha-web.service"

if [ -f "${REDIRECT_SCRIPT}" ]; then
    CUR="启动 IP 寻踪入口"; set_state "entry-service"
    cat > "${UNIT_FILE}" <<EOF
[Unit]
Description=DongGua HAOS Web Finder (port ${WEB_PORT})
After=network-online.target libvirtd.service trim_app_center.service
Wants=network-online.target
# 注意：StartLimitIntervalSec 属于 [Unit] 段，写在 [Service] 里老 systemd 会告警并忽略
StartLimitIntervalSec=0

[Service]
Environment=HAOS_VM_NAME=${VM_NAME}
Environment=HAOS_WEB_PORT=${WEB_PORT}
Environment=HAOS_ADMIN_PROXY_PORT=${ADMIN_PROXY_PORT}
ExecStart=/usr/bin/python3 ${REDIRECT_SCRIPT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable dongguaha-web >/dev/null 2>&1 || true
    # payload 文件部署与回调存在时序差，restart 后必须健康检查，
    # 否则服务可能仍跑着旧进程/旧代码（表现为功能不生效）
    ok=0
    for t in 1 2 3; do
        systemctl restart dongguaha-web 2>/dev/null || systemctl start dongguaha-web 2>/dev/null || true
        sleep 2
        if curl -sf -m 3 "http://127.0.0.1:${WEB_PORT}/healthz" >/dev/null 2>&1; then ok=1; break; fi
    done
    if [ "${ok}" = "1" ]; then
        echo "✅ IP 寻踪已启动并通过健康检查: http://<NAS_IP>:${WEB_PORT}/"
    else
        echo "⚠️ IP 寻踪健康检查未通过（入口会在下一次开机自愈）"
    fi
else
    echo "⚠️ 未找到 ${REDIRECT_SCRIPT}，跳过 IP 寻踪（桌面图标将不可用）"
fi

# ---- 回写 AppCenter：图标按钮显示「打开」并指向固定入口 ----
# 关键时序：install_callback 结束时 AppCenter 才会提交/覆盖该应用的数据行，
# 回调内直接 UPDATE 会被平台终写冲掉，因此交给守护脚本持续校验重写。
CUR="回写应用中心"; set_state "appcenter-sync"
if command -v psql >/dev/null 2>&1; then
    psql -h /var/run/postgresql -d appcenter -U postgres -tAc \
        "UPDATE app SET service_url='http://' || chr(36) || '{host}:${WEB_PORT}/', status='running', is_stop=true, is_uninstall=true, updated_at=now() WHERE app_name='${APP_NAME}'" \
        >/dev/null 2>&1 || true
    SYNC_SCRIPT="${APP_DIR}/bin/dongguaha-db-sync.sh"
    if [ -f "${SYNC_SCRIPT}" ]; then
        # pkill -f 会匹配到自己的命令行，用 [d] 断链
        pkill -f 'dongguaha-db[-]sync.sh' 2>/dev/null || true
        HAOS_WEB_PORT="${WEB_PORT}" setsid nohup /bin/sh "${SYNC_SCRIPT}" \
            >/tmp/dongguaha-db-sync.log 2>&1 </dev/null &
        echo "✅ AppCenter 回写已转入后台守护（首次约需 1-2 分钟生效）"
    else
        echo "⚠️ 缺少 ${SYNC_SCRIPT}，请在应用中心点一次「设置→保存」"
    fi
fi

# ---- 开机：装完顺手把虚拟机叫起来（入口页也能一键开机）----
CUR="启动虚拟机"; set_state "power-on"
rm -f /tmp/haos-vm-start.req /tmp/dongguaha-user-stopped 2>/dev/null || true
: > /tmp/haos-vm-start.req 2>/dev/null || true
virsh -c qemu:///system start "${VM_NAME}" >/dev/null 2>&1 || true

set_state "ready"
echo "========================================"
echo "✅ 冬瓜HAOS ${HAOS_VERSION} 安装完成"
echo "   磁盘: ${QCOW2_FILE}（版本标记 ${DISK_MARK_FILE}）"
echo "   入口: http://<NAS_IP>:${WEB_PORT}/   管理后台反代 :${ADMIN_PROXY_PORT}"
echo "   桌面双图标：冬瓜HAOS（管理后台）/ Home Assistant（/ha → VM:8123）"
echo "   首次开机 Home Assistant 要拉取运行镜像，Web 可能需 15~40 分钟"
echo "========================================"
exit 0
