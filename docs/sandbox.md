# 沙箱 VM 运行器（agino-store-sandbox）

运行在 store 主机上，用户 `agino-sandbox`（属于 `kvm` 组）。它把不可信的厂商 deb 装进一次性的 KVM 虚拟机，检查安装、第三方源、启动、卸载，写出签名报告（格式见 `docs/server.md` 第 9 节）。代码：`server/agino-store-sandbox`（入口）和 `server/agino_store_sandbox/`；测试：`tests/sandbox/`；配置样例：`server/sandbox.conf.example`。只用 Python 3.13 标准库、`qemu-img`、`qemu-system-x86_64`、`openssl`。

## 1. 命令

```
agino-store-sandbox [--config /etc/agino-store-sandbox/sandbox.conf] run [--only NAME] [--slots N]
agino-store-sandbox [--config …] check-base
```

- `run`：处理队列里还没有结果的任务，最多 `slots` 个并行（每个槽一个 flock，一台 VM）。`--only NAME`（`<app>_<sha12>`，可带 `.json`）强制重跑这一个，忽略已有状态。退出码：0 正常，1 有任务落在 `error` 状态，2 配置或用法错误。
- `check-base`：不带任务启动基础镜像，只等 qga、tester 图形会话和 `xprop -root`，打印 `OK: …` 或 `FAIL: …`（退出码 0/1）。

## 2. 配置（`[sandbox]` 段）

| 键 | 默认 | 说明 |
|---|---|---|
| `queue` | `/srv/store/sandbox/queue` | 服务器写入的 `<app>_<sha12>.json` + `.deb` |
| `state` | `/srv/store/sandbox/state` | 每个任务一个 `<名>.json` |
| `run` | `/srv/store/sandbox/run` | 槽锁 `slotN.lock` 和每槽目录 `<N>/` |
| `shots` | `/srv/store/sandbox/shots` | 截图 `<名>.png`，只在主机侧 |
| `reports` | `/srv/store/reports` | 报告 `<名>.json` 和 `<名>.json.sig` |
| `base` | `/srv/store/sandbox/base/current.qcow2` | 只读基础镜像 |
| `ovmf_code` / `ovmf_vars` | OVMF 4M | 固件和每次运行复制的变量模板 |
| `key` | `/etc/agino-store-sandbox/report.pem` | Ed25519 私钥（PEM）；必须是当前用户拥有的 0600 普通文件 |
| `qemu` / `qemu_img` | | 可执行文件名或路径 |
| `qemu_sandbox` | `auto` | `auto` 先试探 qemu 是否支持 `-sandbox on`；`on`/`off` 强制 |
| `slots` | 4 | 1..4，对应 tap `agsbx-t0`..`agsbx-t3`、MAC `52:54:00:5b:00:0N` |
| `memory_mb` / `cpus` | 4096 / 4 | 每台 VM |

## 3. 任务与状态

- 任务 = 队列里名字匹配 `^[a-z0-9][a-z0-9-]*_[0-9a-f]{12}\.json$` 的文件。读取时重新校验全部字段（app/包名/版本/launchable 的字符集、`size`、`deb` 必须等于 `<名>.deb`、`install_profile` 规则），并核对 deb 的大小和 sha256；不符的任务状态记为 `invalid`，不出报告。
- 状态 `<state>/<名>.json`：`status` 为 `done`（带 `result`=pass/fail 和 `report`）、`error`（基础设施错误，带 `reason`、`attempts`）或 `invalid`。没有状态或 `error` 且尝试次数 < 3 的任务会被选中；`done`、`invalid` 只能用 `--only` 重跑。
- 基础设施错误（VM 起不来、qga 超时、会话没出现、guest 里 xprop 失败）不出报告，记 `error`；第 3 次仍失败时写 `status: fail` 的报告，`summary` 为 `sandbox could not run: <原因>`，状态记 `done`。

## 4. 一次运行

1. 槽目录 `<run>/<N>/` 里创建 `overlay.qcow2`（`qemu-img create -b <base> -F qcow2`）、`vars.fd`（OVMF 变量副本）、`qmp.sock`、`qga.sock`、`shot.png`、`qemu.log`；这是唯一会被创建和删除的文件集合（`RunFiles`），运行结束（含失败、超时）只删本次登记过的这些。
2. qemu：`-enable-kvm -machine q35 -cpu host -nodefaults -no-user-config -display none`，virtio-vga，deb 作为只读 raw virtio-blk（serial `sbxdeb`），网络只用预先建好的 tap（`script=no`，绝不用 slirp），qga 走 virtio-serial，QMP 走 unix 套接字，可用时加 `-sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny`。结束时先 QMP `quit`，10 秒后仍在则只对自己启动的那个 PID `kill`。
3. guest 步骤全部经 `guest-exec`，每步套 `timeout -k 10 N`，输出上限 1 MiB：
   等 qga（≤180 s）→ 等 tester 会话（`/run/user/1000/bus` + 会话进程环境，≤120 s）→ 记下六个目录（`/etc/apt/sources.list.d`、`/etc/apt/trusted.gpg.d`、`/etc/apt/keyrings`、`/usr/share/keyrings`、`/etc/systemd/system`、`/etc/cron.d`）的文件列表和 journal 游标 → 把 deb 拷进 guest 并核对 sha256 → 按 helper 的规则应用 `install_profile`（debconf 经 stdin 给 `debconf-set-selections`；`/etc/default/*` 文件 0644，内容经 stdin）→ `apt-get update`、`apt-get install -y /var/tmp/sbx/x.deb`（900 s）→ 删除四个 apt 目录里 `dpkg -S` 查不到属主的新文件（记入 `details.third_party_sources_removed`）→ 重新列目录：四个 apt 目录里仍然存在的任何新文件（含包自己带的源文件）都算 `third_party_sources` → 找桌面文件（`launchable` 对应的 `/usr/share/applications/<id>`，没有就取 `dpkg -L <包>` 的第一个 `.desktop`）→ 记 `xprop -root _NET_CLIENT_LIST`，以 tester 身份 `runuser -u tester -- env … gio launch`，30 秒内出现新窗口即 `window_seen` → QMP `screendump` 到 `<shots>/<名>.png` → 统计 `journalctl -p err --after-cursor` 的行数（前 20 行进 `details.error_lines`）→ `apt-get purge -y --autoremove <包>`（`--autoremove` 一并清掉为它装的依赖，避免依赖留下 systemd/cron 文件造成误报）→ 包已不在，且六个目录里没有新文件，即 `removed_clean`。
4. 通过规则：`installed && third_party_sources == [] && desktop_file 找到 && window_seen && removed_clean`。`new_errors` 只作参考。否则 `fail`，`summary` 列出失败的检查。

## 5. 报告

`<reports>/<名>.json`，字段同 `docs/server.md` 第 9 节（`schema/app/version/sha256/status/started/finished/host/checks/summary`），另加顶层 `details` 对象（`package`、`apt_*_exit`、`apt_install_tail`、`third_party_sources_removed`、`desktop_path`、`launch_exit`、`error_lines`、`leftover_files`，文字都限长）。注意：服务器端 `reports.py` 目前的 `TOP_KEYS` 不含 `details`，需要放行这个键（最多 64 KiB 的 JSON 对象，只当数据）才会接收。

写法：JSON 精确字节 → `openssl pkeyutl -sign -rawin -inkey <key>` → `<名>.json.sig`（一行 base64）。先原子写 `.sig`，再原子写 `.json`，所以服务器不会看到没有签名的报告。`host` 是 `socket.gethostname()`。

## 6. 安全模型

- 来自 guest 的一切都是不可信数据：输出限 1 MiB；解析出的路径必须落在被监视目录之内且不含 `..`、控制字符；环境变量值只允许可打印 ASCII 且限长；报告里的文字去掉控制字符并限长；不使用 eval，不拼 shell：guest 里的脚本是固定文本，可变部分一律作为位置参数或 stdin。
- guest 给出的名字不会用于主机路径；主机路径只来自配置、任务名（严格正则）和固定的运行文件名。
- VM 只用 tap 网络（桥 `agsbx0` 由基础设施 role 管理），不碰 slirp；基础镜像只读，每次运行用新的 overlay。
- 私钥只有 `agino-sandbox` 能读（0600，属主检查），不进 guest，不进日志。
- 不用 `pkill`；只终止自己启动的 qemu PID；只删本次登记的运行文件。
