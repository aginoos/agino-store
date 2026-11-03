# 沙箱 VM 运行器（agino-store-sandbox）

运行在 store 主机上，用户 `agino-sandbox`（属于 `kvm` 组）。它把不可信的厂商 deb 装进一次性的 KVM 虚拟机，检查安装、第三方源、启动、卸载，写出签名报告（格式见 `docs/server.md` 第 9 节）。代码：`server/agino-store-sandbox`（入口）和 `server/agino_store_sandbox/`；测试：`tests/sandbox/`；配置样例：`server/sandbox.conf.example`。只用 Python 3.13 标准库、`qemu-img`、`qemu-system-x86_64`、`openssl`。

## 1. 命令

```
agino-store-sandbox [--config /etc/agino-store-sandbox/sandbox.conf] run [--only NAME] [--slots N]
agino-store-sandbox [--config …] shots-reset NAME
agino-store-sandbox [--config …] check-base
agino-store-sandbox [--config …] session serve APP [--lang zh_CN|en_US]   # 交互式截图会话守护进程（第 9 节）
agino-store-sandbox [--config …] session status APP
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

设计和安全评审见 agino-store-admin `docs/interactive-shots.md`（D-203）；本节是沙箱侧的实现。守护进程 `session serve APP` 由 systemd 模板单元 `agino-store-session@APP.service`（用户 `agino-sandbox`）启动，每个会话一个进程、一台 KVM 虚拟机、一个槽位。平台（Go）通过 `agino-session-ctl` 启停，通过 `web.sock` 使用；部署件在 `deploy/session/`（单元、助手、sudoers、tmpfiles、清理定时器、`nft-egress.md`）。

### 9.1 配置（`[session]` 段，全部可省略）

| 键 | 默认 | 说明 |
|---|---|---|
| `runtime_dir` | `/run/agino-session` | `<app>/web.sock`（0660，组 `web_group`）、`<app>/session.id`、`<app>/token.once`（0600）；`<app>/private/`（0700）放 `qmp.sock`、`qga.sock`、`vnc.sock`、`shot.ppm`、`status.json` |
| `captures` | `/srv/store/sandbox/captures` | `<会话 id>/NN-<kind>-<lang>.png` 和 `captures.json`；目录 0750，文件 0640 |
| `web_group` | `agino-admin` | `web.sock` 和应用目录的组；组不存在时记日志，保持进程的组 |
| `max_sessions` | 2 | 全站并发上限（助手里另有同值常量） |
| `run_minutes` / `idle_minutes` / `total_minutes` / `install_minutes` | 30 / 10 / 60 / 15 | 运行阶段、无输入空闲、含排队和安装的总时长、启动加安装 |
| `overlay_cap_gib` / `min_free_gib` | 8 / 20 | 两层 overlay 总大小上限（每 10 秒检查）；`[sandbox] run` 所在文件系统剩余空间低于此值拒绝开始 |
| `max_captures` / `retention_hours` | 40 / 24 | 每会话截图数；`session cleanup` 保留时长 |
| `traffic_cap_gib` | 2 | 会话期间槽位 tap 的 rx+tx 上限（读 `/sys/class/net/agsbx-tN/statistics/`） |

### 9.2 生命周期和状态机

状态 `queued → starting → installing → starting → running → ending`，任何阶段都可到 `failed`（回滚时 `running → starting → running`）。全部转换在一把锁下，不合法的转换被忽略。

1. 启动时创建运行目录、会话 id（`secrets.token_hex(8)`）、token（`secrets.token_bytes(32)`），在 `umask 0177` 下绑定 `web.sock`，再 `chmod 0660` 并改组；先写 `session.id` 再把 token 的 hex 写入 `token.once`（0600，`O_EXCL`，不进日志）。此后状态 `queued` 已可经 `web.sock` 查询。
2. 找任务：队列里该应用最新的有效任务（`<app>_<12 位十六进制>.json`，重新校验并核对 deb；`deb` 或 `milkyway`，`shots` 字段不要求）。没有则 `failed`。**平台必须保证队列里有这样的任务**（例如先 `shots-request` 再 `sandbox-queue`）。
3. 排队：`slot_lock` 每 2 秒重试，最多 60 秒，否则 `failed`，错误 `sandbox busy`；`[sandbox] run` 所在文件系统剩余空间 < `min_free_gib` 时 `failed`。
4. 启动加安装（总共 `install_minutes`，超时 `failed`，错误 `install timed out`）：第一层 overlay（`overlay.qcow2` 叠在只读基础镜像上），挂 deb 盘；等 qga 和 tester 会话；`shots_job.install`（milkyway：`apt-get update`、`install <包> + l10n`；deb：装候选 deb）；生成 zh_CN/en_US 语言环境；`sync`；QMP `quit`；第二层 overlay（`overlay2.qcow2` 叠在第一层上，UEFI 变量另存 `vars2.fd`）并重启，这是回滚点。
5. 运行：以 tester 身份、所选语言启动应用（桌面文件的 `Exec`，字段码丢弃，`timeout` 为总时长加 5 分钟，stdout 丢弃）。
6. 结束：QMP `quit`，10 秒后只对自己的 PID `kill`；只删本次登记的运行文件；`web.sock`、运行目录删除；截图保留。`failed` 之后守护进程再服务 60 秒（让平台读到 `error`）才退出。`SIGTERM`（`RuntimeMaxSec=3900` 也会发）等于 `end`。

限制（后台线程每秒检查，只置状态并杀本会话的 qemu，清理由主线程做）：运行 `run_minutes`（`extend` 一次 +15 分钟，永远不超过总时长）；空闲 `idle_minutes` **只数 VNC 过滤器放行的 Key 和 Pointer 事件**（更新请求不算）；总时长 `total_minutes`；overlay 超过 `overlay_cap_gib`、tap 流量超过 `traffic_cap_gib`、qemu 意外退出都是 `failed`。qemu 的系统盘有 `throttling.bps-write=50 MiB/s`、`iops-write=3000`。

并发：VNC 转发、控制命令、qga、回滚、QMP 退出互斥；长操作（`capture`、`lang`、`restart`、`rollback`）同一时刻只有一个，另一个得到 `busy`，`status` 永远不阻塞。qga 和 QMP 调用都在操作线程或主线程里，不在 VNC 转发线程里，每个调用有超时；窗口截图的 guest 查询另有总预算 40 秒；qga 超时后关闭旧通道，重新连接并用 `guest-sync` 同步。

### 9.3 web.sock 协议

`CHALLENGE <32 hex>` → `AUTH <VNC|CTL> <hex>`（`HMAC-SHA256(token, 挑战 ‖ 模式)`，`hmac.compare_digest`）→ `OK`。首行 <= 128 字节、5 秒内到达，否则断开；同时最多 16 个连接。`VNC` 模式不在运行状态时回 `ERR not_running`（qemu 的 VNC 套接字连不上回 `ERR vnc_unavailable`）后关闭；同一时刻只有一个 VNC 连接，新的顶掉旧的；回滚主动断开它。`CTL`：每行一个 JSON，<= 4096 字节（更长回 `bad_request` 并断开），每连接每秒 5 个请求（超出回 `rate_limited`，连接保留）。未知 `op`、多余字段、类型不对：`{"ok":false,"error":"bad_request"}`。其他错误串：`not_running`、`busy`、`too_many`（已达 `max_captures`）、`too_big`（编码后 > 3 MiB）、`bad_image`、`capture_failed`、`guest_failed`、`no_xdotool`、`no_window`、`no_such_capture`、`already_extended`、`rollback_failed`、`internal`。

### 9.4 VNC 过滤器（`rfb_filter.py`）

浏览器到 qemu 方向是 qemu 内置 VNC 服务（C 代码）会解析的字节，所以只放行白名单，每条消息按解析出的字段重新组装（填充清零）：握手 `RFB 003.008\n`、安全类型 1（None）、`ClientInit`（0 或 1）；之后 `SetPixelFormat`（20 字节，bpp ∈ {8,16,32}、depth <= bpp、颜色最大值为 2^n-1 且不超 bpp、非真彩色只允许 8 位）、`SetEncodings`（个数 <= 20，只保留 Raw、CopyRect、Tight、ZRLE、Cursor、DesktopSize、ExtendedDesktopSize、LastRect 和 Tight 的质量/压缩级别伪编码 -32..-23、-256..-247，其余包括 QEMU 扩展键盘 -258 和音频 -259 被剔除，个数重写）、`FramebufferUpdateRequest`（10 字节）、`KeyEvent`（8 字节）、`PointerEvent`（6 字节）。其他任何类型（含 `ClientCutText`、类型 255）：记日志并断开。qemu 到浏览器方向不解析，只统计字节。测试：`tests/sandbox/test_rfb_filter.py`（逐条合法、各种畸形、分片、逐字节、固定种子的模糊测试）。

### 9.5 截图（`session_capture.py`）

QMP `screendump` 写 PPM 到 `private/shot.ppm`：先 `stat`（普通文件且 <= 128 MiB，否则不读直接删除），读头部校验宽 640..4096、高 480..4096 且文件大小恰好等于头加 `宽*高*3`，再用 `shots_img.parse_ppm(max_side=4096)` 解码；用后一定删除。输出是纯 RGB PNG（只有 IHDR/IDAT/IEND，不带 alpha）。窗口截图：qga 里 `xdotool getactivewindow`（须在 `_NET_CLIENT_LIST` 里，类型为普通或对话框），否则面积最大的普通窗口；几何和边框只收整数，经 `frame_rect` 和 `clamp_rect` 夹进屏幕；标题去控制字符、<= 120 字符，只用于显示。单张编码后 > 3 MiB：拒绝（`too_big`），不落盘。`problems`：宽 < 900 为 `too_narrow`，高 < 300 为 `too_short`（仍保存）。文件名 `NN-<kind>-<lang>.png`（`lang` 是截图当时应用运行的语言），`captures.json` 原子写入。目录/文件权限见 9.1。

### 9.6 命令和部署

- `session status APP`：打印守护进程每 5 秒（和状态变化时）写到 `private/status.json` 的状态（剩余秒数、空闲秒数按文件年龄推算），供调试。
- `session cleanup`：删除 `captures` 下早于 `retention_hours` 的 `^[0-9a-f]{16}$` 目录；只删显式的文件名（`NN-<kind>-<lang>.png`、`captures.json`、`.*.tmp`），目录里有别的东西就不删目录；不跟随符号链接。
- `deploy/session/agino-session-ctl start|stop APP`（root，`/usr/local/sbin`）：严格校验 APP，不读环境（测试模式除外），`flock` 之后数活动单元（>= `max_sessions` 或同一应用已在运行就拒绝），先删旧的 `token.once`/`session.id`，`systemctl start agino-store-session@APP.service`，最多等 20 秒读 `token.once`（`O_NOFOLLOW`、普通文件、属主 `agino-sandbox`、内容必须是 64 位小写 hex）并删除，输出 `SESSION <id>` 和 `TOKEN <hex>`；超时则停止单元并以 1 退出（3：并发上限或已存在；2：用法）。
- 导入：`agino-store-server shots-session-import`（agino-store `docs/server.md` 第 12 节）。
- 测试：`tests/sandbox/test_session*.py`、`test_rfb_filter.py`、`test_session_ctl.py`（假 `systemctl`，`AGINO_SESSION_CTL_TEST=1` 才允许 `--runtime-dir`）、`tests/server/test_shots_session.py`。
