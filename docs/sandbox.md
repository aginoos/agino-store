# 沙箱 VM 运行器（agino-store-sandbox）

运行在 store 主机上，用户 `agino-sandbox`（属于 `kvm` 组）。它把不可信的厂商 deb 装进一次性的 KVM 虚拟机，检查安装、第三方源、启动、卸载，写出签名报告（格式见 `docs/server.md` 第 9 节）。代码：`server/agino-store-sandbox`（入口）和 `server/agino_store_sandbox/`；测试：`tests/sandbox/`；配置样例：`server/sandbox.conf.example`。只用 Python 3.13 标准库、`qemu-img`、`qemu-system-x86_64`、`openssl`。

## 1. 命令

```
agino-store-sandbox [--config /etc/agino-store-sandbox/sandbox.conf] run [--only NAME] [--slots N]
agino-store-sandbox [--config …] shots-reset NAME
agino-store-sandbox [--config …] check-base
agino-store-sandbox [--config …] session serve SLOT     # 交互式截图会话守护进程，SLOT = vm0、vm1…（第 9 节）
agino-store-sandbox [--config …] session status SLOT
agino-store-sandbox [--config …] session cleanup
```

- `run`：处理队列里还没有结果的任务，最多 `slots` 个并行（每个槽一个 flock，一台 VM）。`--only NAME`（`<app>_<sha12>`，可带 `.json`）强制重跑这一个，忽略已有状态。退出码：0 正常，1 有任务落在 `error` 状态，2 配置或用法错误。
- `check-base`：不带任务启动基础镜像，只等 qga、tester 图形会话和 `xprop -root`，打印 `OK: …` 或 `FAIL: …`（退出码 0/1）。

## 2. 配置（`[sandbox]` 段）

| 键 | 默认 | 说明 |
|---|---|---|
| `queue` | `/srv/store/sandbox/queue` | 服务器写入的 `<app>_<sha12>.json` + `.deb` |
| `state` | `/srv/store/sandbox/state` | 每个任务一个 `<名>.json` |
| `run` | `/srv/store/sandbox/run` | 槽锁 `slotN.lock` 和每槽目录 `<N>/` |
| `shot_dir` | `/srv/store/sandbox/shots` | 启动检查的整屏截图 `<名>.png`，只在主机侧（旧配置里写成路径的 `shots` 仍被当作它） |
| `shots` | `on` | `on`/`off`：是否拍商店截图（第 7 节） |
| `shots_out` | `/srv/store/sandbox/shots-out` | 商店截图输出目录 |
| `corner_radius` | 8 | 截图窗口圆角半径（像素，0..64） |
| `screen` | `1920x1080` | virtio-vga 的 `xres`x`yres`（所有检查都用，缩放 1） |
| `reports` | `/srv/store/reports` | 报告 `<名>.json` 和 `<名>.json.sig` |
| `base` | `/srv/store/sandbox/base/current.qcow2` | 只读基础镜像 |
| `ovmf_code` | OVMF 4M | 固件代码（只读） |
| `base_vars` | `/srv/store/sandbox/base/current.vars` | 基础镜像自己的 UEFI NVRAM（含安装时的启动项），每次运行复制成 `vars.fd`；不要用空白的 OVMF_VARS 模板 |
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

## 7. 截图阶段（shots）

队列 JSON 带 `shots` 对象时（格式见截图方案契约，内容来自 store-content 的 `apps/<id>/shots.yaml`），且配置 `shots = on`，在全部沙箱检查通过之后、purge 之前拍商店截图。截图问题永远不改变沙箱的通过/失败；`shots` 或 `demo` 字段无效时只关掉截图，原因写进 `details.shots.error`。

- **演示数据**：队列里有 `<名>.demo.tar` 时，作为只读 raw virtio-blk 盘挂入（serial `sbxdemo`），guest 里按文件大小截取并核对 sha256，存为 `/var/tmp/sbx/demo.tar`。每个场景开始前删除并重建 `~tester/Demo`，只把该场景 `demo` 列出的目录以 tester 身份解出（`tar --no-same-owner --no-same-permissions`）。
- **前提**：基础镜像必须带 `xdotool`；没有就不拍，`details.shots.error` 记录原因。
- **语言**：先 zh_CN，再 en_US，同一个会话里拍（2026-11-03 起，用户指出不必重启会话）：应用启动时带该语言的 `LANG`/`LANGUAGE`/`LC_ALL`，只截应用窗口，所以桌面外壳的语言不进截图。开拍前检查 guest 里有 `zh_CN.UTF-8` 和 `en_US.UTF-8`，缺了用 `localedef` 生成（基础镜像本身也会生成这两个）。原来的做法（`LangSelector1.SetLocale` + 重启 LightDM，同 `login-as.sh`）每个任务要多花约 2 分钟，已去掉。
- **每个场景**：启动前记录 tester 的 PID 集合和窗口列表；解析桌面文件的 `Exec`（按 Desktop Entry 规范先处理转义，再处理双引号；`%f %F %u %U` 换成场景 `args`，`~/` 展开为 `/home/tester/`，其他字段码丢弃，`%%` 还原；Exec 里完全没有这四个码而 `args` 非空时把 `args` 追加在末尾）；用 `runuser -u tester -- env …` 运行；最多等 30 秒出现新的顶层窗口（`_NET_CLIENT_LIST`，排除启动画面、对话框、工具窗口和菜单类型，取面积最大的，并且要连续 3 次轮询都是同一个窗口才算数：GIMP 等应用先显示启动画面）；`xdotool windowsize` + `windowmove` 居中；执行 `steps`：`wait`、`key`、`type`、`click`（`mousemove --window` 加 `click`，窗口内坐标）、`wait_window`（≤20 秒，标题含该文字，后续步骤与截图以它为目标；`type` 和 `wait_window` 可按语言写不同的值，`args` 里的 `{lang}` 换成当前语言）；再等 2 秒；若活动窗口是新窗口就以它为目标。步骤失败只记 problem 并继续。
- **关应用**：场景结束后只终止“场景开始后才出现的 tester 进程”（前后 PID 集合之差，先 TERM 后 KILL），绝不按名字 pkill。进入截图阶段前也用同样方法关掉启动检查留下的应用实例。
- **取图**：QMP `screendump`（ppm，写在槽目录 `shot.ppm`，用完即删）；guest 给出窗口的 `xdotool getwindowgeometry --shell` 和 `_NET_FRAME_EXTENTS`，主机夹到屏幕范围内后裁剪，圆角外的像素 alpha 置 0（边缘 1 像素抗锯齿），纯 Python 写 RGBA PNG（zlib），不用 ImageMagick。
- **输出**：`<shots_out>/<app>_<sha12>/<lang>/<n>-<场景id>.png` 和 `<shots_out>/<app>_<sha12>/shots.json`（`schema/app/version/sha256/scenes[{id,n,caption,files{zh_CN,en_US:{name,sha256,width,height}},problems}]`）。路径只由配置、任务名、固定语言名和已校验的场景 id 组成。
- **自动检查**（进入该场景的 `problems`，前缀为语言）：窗口没找到、图片小于 400x300、图片几乎单色（亮度标准差 < 4）、存在额外的对话框窗口（新窗口里类型为 `_NET_WM_WINDOW_TYPE_DIALOG` 的标题）、切换语言失败、步骤失败。
- **报告**：`details.shots` = `{sha256: shots.json 的 sha256, images, problems[, error]}`。
- **不可信输入**：guest 的几何数字夹到屏幕内；窗口 id、PID 只接受严格格式；窗口标题限长并去控制字符；场景/步骤来自队列 JSON，读取时再次校验类型和上限（场景 ≤ 6、步骤 ≤ 30、参数 ≤ 8×300 字符、窗口 800..1900 x 600..1050）。代码：`shots_scene.py`（校验、Exec、几何、shots.json）、`shots_img.py`（PPM/PNG）、`shots_guest.py`（guest 操作）、`shots.py`（流程）。

## 8. 仅截图任务（`mode: "shots"`）

milkyway 里的应用不进沙箱队列，已通过的候选也不会再排队，所以截图有自己的任务类型。队列 JSON 里没有 `mode` 就是 `check`（第 3、4 节的全部行为不变）。

- **字段**：`mode: "shots"` 加 `source`：
  - `deb`：`sha256`、`size`、`deb` 必填，大小和 sha256 的核对与检查任务完全相同；`install_profile` 照常应用。
  - `milkyway`：`package` 须匹配 `^[a-z0-9][a-z0-9+.-]+$`，`version` 按 Debian 版本字符集校验，`sha256`/`size`/`deb` 必须为空（没有 deb 文件，VM 不挂 deb 盘）。任务名必须是 `<app>_<h12>`，`h12` = sha256(`milkyway:<包>=<版本>`) 的前 12 位十六进制，不符即 `invalid`。
  - 两种都必须带有效的 `shots`（以及它引用的 demo 包）；缺了或无效是 `invalid`（检查任务里这只会关掉截图）。`mode` 或 `source` 未知、检查任务带 `source` 都是 `invalid`。
- **运行**：起 VM → 等 qga → 等 tester 会话 → 安装（`deb`：拷进 guest 并核对、应用 profile、`apt-get update`、`apt-get install -y /var/tmp/sbx/x.deb`；`milkyway`：`apt-get update && apt-get install -y <包>`，update 失败即安装失败）→ 找桌面文件 → 跑截图阶段（第 7 节，写出 `shots.json`）。不做沙箱检查，不写报告，不 purge（VM 用完即弃）。需要 `shots = on`，否则状态为 `invalid`。
- **状态**：成功记 `done`（带 `mode: "shots"`、`shots` = 截图阶段的 `{images, problems, sha256[, error]}`，没有 `result`/`report`）。安装失败（apt 非零退出、包没装上、没有桌面文件）记 `error`，附 `apt_exit` 和限长（900 字符）的 `apt_tail`；基础设施错误，或截图阶段中途中止且一张图都没有，也记 `error`。重试规则和检查任务一样：`error` 且尝试 < 3 会被再选中，第 3 次之后停在 `error`（不写失败报告，因为没有报告）。
- **日志**：每行带任务名：`waiting for the session`、`installing (deb|milkyway)`，随后是截图阶段的 `scene …` 行；最后一行为 `<名>: done|error`。
- 代码：`jobs.py`（解析）、`shots_job.py`（运行路径）、`runner.py`（`_run_shots`，状态）。
- **重拍**：仅截图任务的名字只取决于 deb 或包版本，改了 `shots.yaml` 名字不变。`shots-reset NAME` 把 `<shots_out>/NAME` 改名为 `NAME.prev`（覆盖更旧的一份，便于对比），删掉状态 `<state>/NAME.shots.json`；之后在服务端重新 `shots-request APP`，下一次队列导出和运行就会重拍。

## 9. 交互式截图会话（`session`）

设计和安全评审见 agino-store-admin `docs/interactive-shots.md`（D-203；第 14 节是第二版：一个会话复用一台虚拟机）；本节是沙箱侧的实现。守护进程 `session serve SLOT`（槽位名 `vm0`、`vm1`…，`^vm[0-9]$`）由 systemd 模板单元 `agino-store-session@SLOT.service`（用户 `agino-sandbox`）启动，每个槽位一个进程、**一台一直开着的 KVM 虚拟机**、一个沙箱槽位（tap 编号）；一个会话里可以依次装、截、回滚很多个应用，不重启虚拟机。平台（Go）通过 `agino-session-ctl` 启停，通过 `web.sock` 使用；部署件在 `deploy/session/`（单元、助手、sudoers、tmpfiles、清理定时器、`nft-egress.md`）。

### 9.1 配置（`[session]` 段，全部可省略）

| 键 | 默认 | 说明 |
|---|---|---|
| `runtime_dir` | `/run/agino-session` | `<slot>/web.sock`（0660，组 `web_group`）、`<slot>/session.id`、`<slot>/token.once`（0600）；`<slot>/private/`（0700）放 `qmp.sock`、`qga.sock`、`vnc.sock`、`shot.ppm`、`status.json` |
| `captures` | `/srv/store/sandbox/captures` | `<会话 id>/NN-<kind>-<lang>.png` 和 `captures.json`；目录 2750（setgid：文件继承 agino-store 组，导入命令才读得到），文件 0640 |
| `web_group` | `agino-admin` | `web.sock` 和槽位目录的组；组不存在时记日志，保持进程的组 |
| `max_sessions` | 2 | 全站并发上限（助手里另有同值常量） |
| `idle_minutes` / `total_minutes` / `install_minutes` | 10 / 120 / 15 | 无输入空闲、整个会话的总时长（`extend` 一次 +30 分钟）、单个应用的安装时限（开机加做快照也按它计）；`run_minutes` 不再使用，只为旧配置能加载而保留 |
| `overlay_cap_gib` / `min_free_gib` | 12 / 20 | 唯一一层 overlay 的大小上限（含快照状态，起步约 2.1 GiB；每 10 秒检查）；`[sandbox] run` 所在文件系统剩余空间低于此值拒绝开始 |
| `max_captures` / `retention_hours` | 40 / 24 | 每会话截图数；`session cleanup` 保留时长 |
| `traffic_cap_gib` | 2 | 会话期间槽位 tap 的 rx+tx 上限（读 `/sys/class/net/agsbx-tN/statistics/`） |

### 9.2 生命周期和状态机

状态 `queued → starting → idle → installing → running → restoring → idle`；安装或启动失败进入 `failed_app`（虚拟机完好，状态里 `app` 和 `error` 保留，`finish_app` 或 `rollback` 恢复）；`rollback` 是 `restoring → installing`（同一应用同一语言）；任何阶段都可到 `ending` 或 `failed`（整个会话失败）。全部转换在一把锁下，不合法的转换被忽略。

1. 启动时创建运行目录、会话 id（`secrets.token_hex(8)`）、token（`secrets.token_bytes(32)`），在 `umask 0177` 下绑定 `web.sock`，再 `chmod 0660` 并改组；先写 `session.id` 再把 token 的 hex 写入 `token.once`（0600，`O_EXCL`，不进日志）。此后状态 `queued` 已可经 `web.sock` 查询。
2. 排队：`slot_lock` 每 2 秒重试，最多 60 秒，否则 `failed`，错误 `sandbox busy`；`[sandbox] run` 所在文件系统剩余空间 < `min_free_gib` 时 `failed`。槽位一直持有到会话结束。
3. 开机（`starting`，总共 `install_minutes`，超时 `failed`，错误 `boot timed out`）：一层 overlay（`overlay.qcow2` 叠在只读基础镜像上）；OVMF 变量盘用 `qemu-img convert -f raw -O qcow2` 转成 `vars.qcow2`，用 `-blockdev` 打开，`-machine q35,accel=kvm,pflash0=code,pflash1=vars`（code 是只读 raw `-blockdev`）；系统盘 `-drive …,id=sys,node-name=sysn`（节流不变）；VNC unix 套接字、virtio 键鼠、一个固定在 `pcie.0` 地址 `0x1e` 的 `pcie-root-port`（给厂商 deb 盘热插拔用，不挤占其他设备的地址，guest 里网卡名不变）。普通沙箱运行路径仍用 raw pflash 和 `-drive`，不受影响。
4. 等 qga 和 tester 图形会话，生成 zh_CN/en_US 语言环境（成为干净状态的一部分），再等 20 秒让桌面稳定，记下 tester 的进程号集合（`clean_pids`，快照恢复后进程号不变，是以后“杀本会话新起的进程”的基线），然后 QMP `snapshot-save`：快照名 `clean`，`devices ["sysn","vars"]`，`vmstate "sysn"`，用 `job-id` 发，轮询 `query-jobs` 到 `concluded`，`job-dismiss`，有 `error` 就失败。完成后状态 `idle`（干净系统，没有应用）。
5. `select_app`（只在 `idle`，应用 id 须匹配 `^[a-z0-9][a-z0-9-]{0,63}$`）：读队列里该应用最新的有效任务（`<app>_<12 位十六进制>.json`，重新校验并核对 deb；**平台必须先跑 `session-prepare APP`** 保证有这样的任务；没有则 `no_job`，状态不变），然后**异步**进入 `installing`：厂商应用先把 deb 作为只读 virtio 盘（序列号 `sbxdeb`）经 `blockdev-add` + `device_add` 热插到 `debport`，`shots_job.install` 装完（milkyway：`apt-get update`、`install <包> + l10n`；deb：装候选 deb）后 `device_del`，轮询 `blockdev-del` 到成功（guest 放手了）才继续，失败则 `failed_app`；再读桌面文件的 `Exec`（字段码丢弃），以 tester 身份、所选语言启动（`timeout` 为剩余总时长加一次 `extend` 再加 5 分钟，stdout 丢弃），状态 `running`。安装或启动失败（含 apt 失败、没有桌面文件、`Exec` 读不出）是 `failed_app`，不是会话失败。
6. 回滚（`finish_app` 和 `rollback` 的底层，**同步**，返回时已恢复；`rollback` 返回后应用的安装在后台继续，状态 `installing`）：状态 `restoring`，断开 VNC，先卸下 deb 盘（若还插着），QMP `snapshot-load`（同样的 job 流程），VM 状态不是 `running` 就 `cont`；qga 先用原通道 `guest-sync`（20 秒），不通则关闭旧通道重连（60 秒）；然后 qga `guest-set-time`（宿主机 `time.time_ns()`，快照里的时钟是旧的）；`wait_session` 重读 tester 会话环境。`desktop_lang` 回到 `zh_CN`，进程基线回到 `clean_pids`。任何一步失败：会话 `failed`，错误 `rollback failed: …`（`restoring` 总共不超过 300 秒，超时同样失败）。`finish_app` 之后 `app` 清空、应用语言回到 `zh_CN`，截图保留。
7. 结束：QMP `quit`，10 秒后只对自己的 PID `kill`；只删本次登记的运行文件；`web.sock`、运行目录删除；截图保留。`failed` 之后守护进程再服务 60 秒（让平台读到 `error`）才退出。`SIGTERM` 等于 `end`。单元的 `RuntimeMaxSec` 必须不小于 `(total_minutes + 30) * 60` 再加清理余量。

限制（后台线程每秒检查，只置状态并杀本会话的 qemu，清理由主线程做）：总时长 `total_minutes`（默认 120 分钟，`extend` 一次 +30 分钟，任何非结束状态都可 `extend`）；空闲 `idle_minutes`（只在 `idle`、`running`、`failed_app` 三个等待操作员的状态计，`installing`、`restoring`、`starting` 不计；进入这些状态时重置），**只数 VNC 过滤器放行的 Key 和 Pointer 事件**（更新请求不算）；overlay 超过 `overlay_cap_gib`、tap 流量超过 `traffic_cap_gib`、qemu 意外退出都是 `failed`。qemu 的系统盘有 `throttling.bps-write=50 MiB/s`、`iops-write=3000`。

并发：VNC 转发、控制命令、qga、回滚、QMP 退出互斥；长操作（`capture`、`lang`、`restart`、`finish_app`、`rollback`、`select_app` 的安装线程）同一时刻只有一个，另一个得到 `busy`；状态不对得到 `bad_state`（先查状态再抢锁）；`status` 永远不阻塞。qga 和 QMP 调用都在操作线程或主线程里，不在 VNC 转发线程里，每个调用有超时；窗口截图的 guest 查询另有总预算 40 秒；qga 超时后关闭旧通道，重新连接并用 `guest-sync` 同步。

**语言**（`lang` `{"lang","desktop"}`，只在 `running`，`desktop` 缺省 `false`）：窗口档杀掉 tester 在基线之后新起的进程，用目标语言的 `LANG`/`LANGUAGE`/`LC_ALL` 重新启动应用（`shots_guest.start_app`）。整屏档先做同样的清理，再 `ShotsGuest.switch_desktop_locale`：1）以 root 用 `sed` 改 `/etc/default/locale` 和 `/etc/locale.conf` 里的 `LANG` 和全部 `LC_*`；2）`busctl --system call org.deepin.dde.Accounts1 /org/deepin/dde/Accounts1/User1000 org.deepin.dde.Accounts1.User SetLocale s <locale>`；3）`systemctl set-environment LANG=… LC_ADDRESS=… … LC_TIME=…`；4）`runuser -u tester -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user set-environment …`（lingering 的用户管理器启动桌面进程，不改这一层桌面不变）；5）`systemctl restart lightdm`。全部命令固定在代码里（每步 30 秒，重启 90 秒），只有 locale 取自白名单 `{zh_CN.UTF-8, en_US.UTF-8}`，作为命令行参数或 shell 位置参数传入，不拼接任何外部输入。之后守护进程 `wait_session`，等 9 秒桌面稳定，再 `wait_session` 刷新环境，重取 tester 进程基线（新桌面会话的进程号都变了，否则下一次重启应用会杀掉桌面），最后以目标语言启动应用。`desktop_lang` 在 `lightdm` 重启成功后才改；任何一步失败回 `guest_failed`，应用语言保持原值，会话保持 `running`（桌面可能处于中间状态，操作员可 `rollback`）。状态里 `lang`（应用语言）和 `desktop_lang`（桌面语言）分开记录，截图记录两者。

### 9.3 web.sock 协议

`CHALLENGE <32 hex>` → `AUTH <VNC|CTL> <hex>`（`HMAC-SHA256(token, 挑战 ‖ 模式)`，`hmac.compare_digest`）→ `OK`。首行 <= 128 字节、5 秒内到达，否则断开；同时最多 16 个连接。`VNC` 模式只在 `idle`、`installing`、`running`、`failed_app` 状态可用，否则回 `ERR not_running`（qemu 的 VNC 套接字连不上回 `ERR vnc_unavailable`）后关闭；同一时刻只有一个 VNC 连接，新的顶掉旧的；回滚主动断开它。`CTL`：每行一个 JSON，<= 4096 字节（更长回 `bad_request` 并断开），每连接每秒 5 个请求（超出回 `rate_limited`，连接保留）。操作：`status`、`select_app {app,lang}`、`finish_app`、`rollback`、`lang {lang, desktop?}`（`desktop` 可省，必须是布尔）、`restart`、`extend`、`end`、`capture {kind}`、`captures`、`get {n}`。`status` 的字段：`ok state id slot app lang desktop_lang remaining_s idle_s captures extended error`（`app` 为空表示没有应用）。未知 `op`、多余字段、缺字段、类型不对：`{"ok":false,"error":"bad_request"}`；状态不对（例如 `idle` 时 `capture`）：`bad_state`。其他错误串：`no_job`（`select_app` 时队列里没有该应用的有效任务）、`busy`、`too_many`（已达 `max_captures`）、`too_big`（编码后 > 3 MiB）、`bad_image`、`capture_failed`、`guest_failed`、`no_xdotool`、`no_window`、`no_such_capture`、`already_extended`、`rollback_failed`（恢复失败，会话已 `failed`）、`internal`。

### 9.4 VNC 过滤器（`rfb_filter.py`）

浏览器到 qemu 方向是 qemu 内置 VNC 服务（C 代码）会解析的字节，所以只放行白名单，每条消息按解析出的字段重新组装（填充清零）：握手 `RFB 003.008\n`、安全类型 1（None）、`ClientInit`（0 或 1）；之后 `SetPixelFormat`（20 字节，bpp ∈ {8,16,32}、depth <= bpp、颜色最大值为 2^n-1 且不超 bpp、非真彩色只允许 8 位）、`SetEncodings`（个数 <= 20，只保留 Raw、CopyRect、Tight、ZRLE、Cursor、DesktopSize、ExtendedDesktopSize、LastRect 和 Tight 的质量/压缩级别伪编码 -32..-23、-256..-247，其余包括 QEMU 扩展键盘 -258 和音频 -259 被剔除，个数重写）、`FramebufferUpdateRequest`（10 字节）、`KeyEvent`（8 字节）、`PointerEvent`（6 字节）。其他任何类型（含 `ClientCutText`、类型 255）：记日志并断开。qemu 到浏览器方向不解析，只统计字节。测试：`tests/sandbox/test_rfb_filter.py`（逐条合法、各种畸形、分片、逐字节、固定种子的模糊测试）。

### 9.5 截图（`session_capture.py`）

QMP `screendump` 写 PPM 到 `private/shot.ppm`：先 `stat`（普通文件且 <= 128 MiB，否则不读直接删除），读头部校验宽 640..4096、高 480..4096 且文件大小恰好等于头加 `宽*高*3`，再用 `shots_img.parse_ppm(max_side=4096)` 解码；用后一定删除。输出是纯 RGB PNG（只有 IHDR/IDAT/IEND，不带 alpha）。窗口截图：qga 里 `xdotool getactivewindow`（须在 `_NET_CLIENT_LIST` 里，类型为普通或对话框），否则面积最大的普通窗口；几何和边框只收整数，经 `frame_rect` 和 `clamp_rect` 夹进屏幕；标题去控制字符、<= 120 字符，只用于显示。单张编码后 > 3 MiB：拒绝（`too_big`），不落盘。`problems`：宽 < 900 为 `too_narrow`，高 < 300 为 `too_short`（仍保存）。文件名 `NN-<kind>-<lang>.png`（`lang` 是截图当时应用运行的语言；编号在整个会话里递增，跨应用不重复），`captures.json`（`schema` 2）原子写入，每项除序号、文件、类型、语言、尺寸、体积、sha256、标题、`problems` 外还有 `app`（截图时选中的应用）和 `desktop_lang`（桌面语言）。`capture` 的响应带同样的两个字段；只在 `running` 可用。目录/文件权限见 9.1。

### 9.6 命令和部署

- `session status SLOT`：打印守护进程每 5 秒（和状态变化时）写到 `private/status.json` 的状态（剩余秒数、空闲秒数按文件年龄推算），供调试。
- `session cleanup`：删除 `captures` 下早于 `retention_hours` 的 `^[0-9a-f]{16}$` 目录；只删显式的文件名（`NN-<kind>-<lang>.png`、`captures.json`、`.*.tmp`），目录里有别的东西就不删目录；不跟随符号链接。
- `deploy/session/agino-session-ctl start | stop SLOT`（root，`/usr/local/sbin`；接口见 interactive-shots.md 14.3，另有人维护）：`start` 无参数，加锁后选第一个没有活动单元的槽位并启动 `agino-store-session@<slot>.service`，读 `token.once` 后删除，输出 `SLOT <slot>`、`SESSION <id>`、`TOKEN <hex>`，全部槽位占用以 3 退出；`stop` 的参数须匹配 `vm[0-9]`。
- 导入：`agino-store-server shots-session-import APP --session ID --reviewer NAME --pick NN[,NN…]`（agino-store `docs/server.md` 第 12 节）。**只导入 `captures.json` 里 `app` 等于 `APP`（且文件名与记录一致）的截图**，其余拒绝并计入 problems；`captures.json` 读不了或格式不对则一张也不导入。该文件按敌对输入处理（`O_NOFOLLOW`、1 MiB 上限、只认格式正确的项，只用它的 `app`，其余字段仍从文件名和图片重新得出）。
- 测试：`tests/sandbox/test_session*.py`（`test_session_vm.py` 覆盖快照 job 和 deb 热插拔的 QMP 流程）、`test_rfb_filter.py`、`test_session_ctl.py`（假 `systemctl`，`AGINO_SESSION_CTL_TEST=1` 才允许 `--runtime-dir`）、`tests/server/test_shots_session.py`。
