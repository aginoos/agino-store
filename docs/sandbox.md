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
- **语言**：先 zh_CN，再 en_US。切换方法同 `infra/tools/iso-test/login-as.sh`：runner 把一个等价的 root 脚本经 qga 写进 guest（`guest-file-*`，不可用时退回 `cat`），做 `LangSelector1.SetLocale`、删除生成的输入法文件、重启 LightDM（自动登录 drop-in）、等 tester 的 dde-shell，再重新取会话环境。应用启动时同时带 `LANG`/`LANGUAGE`/`LC_ALL`。
- **每个场景**：启动前记录 tester 的 PID 集合和窗口列表；解析桌面文件的 `Exec`（按 Desktop Entry 规范先处理转义，再处理双引号；`%f %F %u %U` 换成场景 `args`，`~/` 展开为 `/home/tester/`，其他字段码丢弃，`%%` 还原；Exec 里完全没有这四个码而 `args` 非空时把 `args` 追加在末尾）；用 `runuser -u tester -- env …` 运行；最多等 30 秒出现新的顶层窗口（`_NET_CLIENT_LIST`，多个时取面积最大的）；`xdotool windowsize` + `windowmove` 居中；执行 `steps`：`wait`、`key`、`type`、`click`（`mousemove --window` 加 `click`，窗口内坐标）、`wait_window`（≤20 秒，标题含该文字，后续步骤与截图以它为目标）；再等 2 秒；若活动窗口是新窗口就以它为目标。步骤失败只记 problem 并继续。
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
