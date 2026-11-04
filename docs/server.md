# agino-store 服务端设计（第一阶段 PoC）

需求和总体设计见 infra 仓库 `docs/app-store-server.md`（D-192 补充）。本文是实现规格：配置、内容仓库格式、状态文件、命令和模块分工。目录输出格式就是客户端用的 schema 1（`design.md` 第 4 节）。

## 1. 组件

```
server/agino-store-server              命令入口（python3 -I，调用 agino_store_server.cli）
server/agino_store_server/
  config.py      读 server.conf
  state.py       状态目录：锁、厂商跟踪记录、候选版本、放行记录、milkyway 版本
  content.py     读取并校验 store-content（YAML）
  catalog.py     生成 schema 1 目录、签名、续期（复用 catalogwriter）
  catalogwriter.py  写目录树、manifest、Ed25519 签名（从 tools/make-catalog.py 抽出，两边共用）
  cli.py         子命令
  fetch.py       HTTPS 下载：域名白名单、大小上限、条件请求、限速
  aptrepo.py     厂商 apt 源：InRelease 验签、Packages 解析
  trackers.py    五种跟踪方式，产出候选版本
  debinfo.py     读 deb：control、maintainer 脚本、文件清单
  checks.py      依赖检查（dose-debcheck）、和上一版的差异
  reports.py     导入沙箱报告
  content_edit.py / content_patch.py / content_git.py   content-edit、content-confirm：补丁校验、写回 app.yaml、提交和推送（第 13 节）
tests/server/    unittest；fixture 在测试里现场生成（gpg 临时密钥、dpkg-deb 打的小包、本地 http 服务）
```

依赖（Debian 包）：`python3`（3.13）、`python3-yaml`、`python3-apt`（版本比较，用 `apt_pkg`）、`gpgv`、`gpg`（只在测试里生成密钥）、`dpkg`、`dose-distcheck`、`openssl`（签名，与 make-catalog 相同）。

## 2. 配置 `/etc/agino-store-server/server.conf`

INI（configparser），全部是路径和地址，没有机密（私钥只给路径）：

```ini
[paths]
content = /srv/store/content          # store-content 的检出
state = /srv/store/state
downloads = /srv/store/downloads      # deb：<sha256>/<原文件名>
preview = /srv/store/preview          # 生成的目录：catalog-<serial>/ 和 current -> catalog-<serial>
reports = /srv/store/reports          # 从沙箱主机拉回的报告
sandbox_queue = /srv/store/sandbox/queue   # 可选（默认值即此）；交给沙箱主机的 deb + JSON，见第 10 节
shots_out = /srv/store/sandbox/shots-out   # 可选（默认值即此）；沙箱主机写的截图，只读它来关闭截图请求，见第 10 节
metrics = /var/lib/prometheus/node-exporter
sandbox_state = /srv/store/sandbox/state   # 可选（默认值即此）；只有 status 读：沙箱主机每个任务的状态文件
captures = /srv/store/sandbox/captures   # 可选（默认值即此）；交互式截图会话的截图，只由 shots-session-import 读取，见第 12 节
screenshots = /srv/store/screenshots       # 可选（默认值即此）；服务端管理的截图目录和 meta.json，见第 12 节
shots_staging = /var/lib/agino-admin/shots-staging   # 可选（默认值即此）；管理后台放上传文件的暂存目录，shots-ingest 读完即删

[shots]                                # 可选，整段可省略
max_per_lang = 6                       # 每个应用每种语言最多几张（不含 rejected），1..20
min_width = 900                        # 截图最小宽度（像素），100..4096

[content]                              # 可选，整段可省略（第 13 节）
push = no                              # yes：content-edit / content-confirm 提交后推送到 origin 的 main；默认 no（只在本地提交）
push_ssh_command =                     # 推送和 pull --rebase 用的 GIT_SSH_COMMAND，例如 ssh -i /srv/store/.ssh/content -o BatchMode=yes；空 = 普通 ssh

[milkyway]
url = https://repo.aginoos.com        # 取 dists/milkyway/<component>/binary-{amd64,all}/Packages.gz
suite = milkyway
components = main contrib non-free
arch = amd64
keyring = /usr/share/keyrings/agino-archive-keyring.gpg   # 可选；设了就取 InRelease 并验签
fingerprints = <主密钥指纹，空格分隔>                      # 设了 keyring 就必须设

[catalog]
# key 和 sign_command 必须恰好设一个
sign_command = sudo -n -u agino-store-sign /usr/local/bin/agino-store-sign preview   # 生产：签名交给独立用户的辅助程序
#key = /etc/agino-store-server/keys/preview.pem   # Ed25519 私钥（PEM）；仅测试和虚拟机实验用
deb_base_url = http://51.38.63.36:8080/store-preview/debs/
valid_days = 14
renew_after_hours = 20
keep_catalogs = 3

[fetch]
user_agent = agino-store-server/0.1 (+https://aginoos.com)
min_interval_seconds = 1
max_deb_bytes = 2147483648
max_index_bytes = 134217728
# Test only: allow http:// and file:// (tests and VM experiments). The command line refuses to start
# with this set unless the environment variable AGINO_STORE_SERVER_TESTS=1 is present.
allow_insecure = false

[reports]
keys = /etc/agino-store-server/sandbox-keys   # 沙箱公钥目录：每个 *.pub 一行，原始 32 字节 Ed25519 公钥的 base64
```

`[reports] keys` 不在 `config.py` 里，由 `cli.py` 从同一个 `--config` 文件单独读取，作为参数传给 `reports.ingest`。缺省值就是上面的路径。

## 3. 内容仓库 store-content

```
categories.yaml
featured.yaml
apps/<id>/app.yaml
apps/<id>/icon.svg | icon.png
apps/<id>/screenshots/zh_CN/*.png, en_US/*.png     （可选，已弃用：截图改放 paths.screenshots，见第 12 节）
apps/<id>/shots.yaml                               （可选）自动截图的场景脚本
apps/<id>/promo/…                                  （可选）厂商宣传图，人工审核
demo/<name>/…                                      （可选）自动截图用的演示数据
vendors/<vendor-id>.yaml
vendors/keys/<name>.asc                            厂商 apt 源的公钥（审核后入库，不在运行时下载）
```

`id`、`vendor-id`：`^[a-z0-9][a-z0-9.+-]*$`，即目录名。

`categories.yaml`：

```yaml
- id: graphics
  name: {zh_CN: 图形, en_US: Graphics}
```

`featured.yaml`（顺序即显示顺序；第一个 `banner: false` 的是"装机必备"）：

```yaml
- id: essentials
  title: {zh_CN: 装机必备, en_US: Essentials}
  subtitle: {zh_CN: …, en_US: …}     # 可选
  banner: false
  apps: [chrome, wechat, gimp]
```

`apps/<id>/app.yaml`：

```yaml
name: {zh_CN: GIMP, en_US: GIMP}
summary: {zh_CN: 图像编辑器, en_US: Image editor}
description:                          # 段落列表
  zh_CN: [段落一, 段落二]
  en_US: [Paragraph one, Paragraph two]
categories: [graphics]                # 必须在 categories.yaml 里
source: system                        # system | official | personal
license: GPL-3.0-or-later             # SPDX 表达式，闭源写 LicenseRef-proprietary
proprietary: false
developer: {zh_CN: GIMP 团队, en_US: The GIMP Team}
homepage: https://www.gimp.org/
maintainer: null                      # personal 必填：{name: …, contact: …}
launchable: org.gimp.GIMP.desktop     # 可选；元包必填
icon: icon.png
screenshots:                          # 可选；图片相对于应用目录
  zh_CN: [{image: screenshots/zh_CN/1.png}]
  en_US: [{image: screenshots/en_US/1.png}]
description_source: confirmed          # 可选：ai | confirmed | submitter | official；缺省 = submitter（第 13 节）
description_confirmed:                # 可选，只允许在 description_source: confirmed 时出现
  by: alice                           # ^[A-Za-z0-9._-]{1,32}$
  at: "2026-11-03T08:00:00Z"          # UTC，必须加引号（不加引号 YAML 会把它读成时间对象，校验拒绝）
package:
  kind: milkyway                      # 或 deb
  name: gimp
  # kind: deb 时：
  # vendor: chrome                    # vendors/chrome.yaml
install_profile:                      # 可选，格式同 design.md 4 节
  debconf: ["code code/add-microsoft-repo boolean false"]
  default_files: [{path: /etc/default/google-chrome, content: "repo_add_once=\"false\"\n"}]
release_notes:                        # 可选，按版本号
  "3.0.4-3+deb13u11": {zh_CN: […], en_US: […]}
```

规则：
- `name`、`summary`、`description` 必须同时有 `zh_CN` 和 `en_US`，其他语言可选。
- `kind: milkyway` 的 `source` 只能是 `system`；`kind: deb` 只能是 `official` 或 `personal`。
- 截图的宽高由 build 从 PNG 头读出；PoC 不生成缩略图（`thumb` 与 `image` 相同）。`screenshots:` 仍然接受（向后兼容），但某个应用在截图目录里有 approved 的图时，以目录为准（D-203，见第 12 节）。
- 未知字段报错（防止拼错的字段被静默忽略）。`description_source` 和 `description_confirmed` 是仅有的两个新增可选字段（D-203）。`description_source: ai` 的应用不能直接放行（第 5 节 `approve`）。

`apps/<id>/shots.yaml`（可选；没有这个文件就不为该应用自动截图。完整约定见 research/app-store-13/shots-contract.md）：

```yaml
schema: 1
window: {width: 1600, height: 1000}   # 可选，默认 1600x1000；宽 800..1900，高 600..1050
scenes:                               # 1..6 个（建议 4..6，校验器不对少于 4 个报警）
  - id: editor                        # ^[a-z0-9][a-z0-9-]{0,30}$，不重复
    caption: {zh_CN: …, en_US: …}     # 两种语言都必填，单行，<= 80 字符
    demo: [code-project]              # 可选：场景前复制到 ~/Demo/<name>/ 的演示树，必须存在于 demo/
    args: ["~/Demo/code-project"]     # 可选：<= 8 项，每项 <= 300 字符，不含换行；{lang} 在运行时换成 zh_CN / en_US
    window: {width: 1400, height: 900}  # 可选：本场景的窗口大小
    steps:                            # 可选，<= 30 步，只能是下面五种之一
      - wait: 3                       # 秒，0 < x <= 20
      - key: "ctrl+shift+p"           # ^[A-Za-z0-9_+]{1,40}$
      - type: "hello"                 # 可打印字符，<= 200，无换行；也可写 {zh_CN: …, en_US: …}
      - click: [200, 150]             # 窗口内逻辑像素，可加第三项 left|right|middle
      - wait_window: {zh_CN: "首选项", en_US: "Preferences"}  # <= 80 字符；一个字符串表示两种语言相同
    capture: window                   # 目前只有 window
promo:                                # 可选：登录后才能看的应用的厂商宣传图
  - {image: promo/zh_CN/1.png, lang: zh_CN, source: "https://…", license: "…"}
```

规则：
- 未知字段报错；任何一项不合规，整个内容仓库无效（和其他错误一样，一行 `路径: 原因`）。
- `promo[].image` 必须是应用目录里已存在的 PNG 文件（检查文件头），<= 10 MiB；`lang` 只能是 `zh_CN` 或 `en_US`；`source`、`license` 必填。promo 只做校验和保存，build 不用它，目录里的截图以后只来自审核过的文件。
- `demo/<name>/`：`<name>` 匹配 `^[a-z0-9][a-z0-9-]{0,40}$`；每棵树合计 <= 20 MiB，单个文件 <= 5 MiB，最多 5000 项、16 层；任何位置都不许符号链接，只许普通文件和目录（权限位不保留）。`demo/` 里所有树都会校验，包括暂时没人用的。
- `shots.yaml`、`promo/` 和 `demo/` 都不进 `content_digest`，所以改它们不会让 build 的输入哈希变化、也不会生成新的目录序列号。
- 校验后的结果挂在应用条目上：`shots`（没有文件就是 `None`；`window` 已按默认值补全，每个场景的 `window` 也已补全，`demo`、`args`、`steps` 缺省为空列表，`capture` 缺省为 `window`）和 `promo`。

`vendors/<vendor-id>.yaml`：

```yaml
package: google-chrome-stable         # deb 的 Package 字段必须等于它
arch: amd64
fetch: auto                           # auto：自动下载；manual：只发现版本，等人工 intake
allowed_hosts: [dl.google.com]        # 每一跳重定向都要在这里
track:
  type: apt                           # apt | json | head | page | redirect
  url: https://dl.google.com/linux/chrome/deb/
  suite: stable
  component: main
  keys: [keys/google.asc]
  fingerprints: [EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796]   # 主密钥指纹；InRelease 必须由其中之一（或其子密钥）签名
```

其他类型的 `track`：

| type | 字段 | 版本号从哪来 | 下载地址 |
|---|---|---|---|
| `json` | `url`、`strip_js: true`（取文本里第一个 `{…}` 对象）、`version_path: version`、`url_path: x64DownloadUrl.deb`（点号分隔） | JSON | JSON |
| `head` | `url` | 下载后从 deb control 读 | `url` 本身；ETag、Last-Modified、Content-Length 都没变就不下载 |
| `page` | `url`、`link_regex`（必须有命名组 `url`，可有 `version`） | 正则的 `version` 组，没有就下载后读 control | 正则的 `url` 组 |
| `redirect` | `url`、`version_regex`（作用于最终地址，必须有组 `version`） | 最终地址 | 最终地址 |

`allowed_hosts` 必须是完整的公网域名：不接受 IP 字面量、`localhost`、单标签名（`allow_insecure` 时放行，仅测试）。`track.url` 的端口只能是 443（`allow_insecure` 时放行）。`link_regex`、`version_regex` 限长 500，且拒绝嵌套量词（`(a+)+`、`(.*)*`、`(a+)*` 这类，简单启发式；运行时的保护在 fetch/trackers 一侧）。

`fetch: manual` 只能用于能直接得到版本号的类型（`json`、`page` 带 `version`、`redirect`、`apt`）：发现新版本后生成状态为 `awaiting-upload` 的候选，由 `intake` 补上文件。

## 4. 状态目录 `paths.state`

所有修改状态的命令先拿 `state/.lock`（fcntl 排他锁，非阻塞失败就报"另一个任务正在运行"）。JSON 一律原子写入（同目录临时文件 + fsync + rename）。`state.py` 提供下面的接口，其他模块不直接碰这些文件。

```
vendors/<vendor-id>.json     跟踪记录：{last_check, last_ok, last_error, etag, last_modified,
                             content_length, inrelease_date, seen_version}
candidates/<app-id>/<version>_<sha12>.json    候选版本（见下）；awaiting-upload 的文件名用 <version>_upload
approved.json                {<app-id>: {version, sha256, candidate, approved_at, by, note}}
approvals.log                每次 approve/reject 追加一行 JSON
milkyway.json                {<package>: {version, first_seen, size, installed_size}}
catalog.json                 {serial, inputs_sha256, built_at, revision}
```

候选版本：

```json
{
  "app": "chrome", "vendor": "chrome", "package": "google-chrome-stable", "arch": "amd64",
  "version": "156.0.8078.79-1",
  "sha256": "…", "size": 145633696, "installed_size": 467080192,
  "filename": "google-chrome-stable_156.0.8078.79-1_amd64.deb",
  "file": "<sha256>/google-chrome-stable_156.0.8078.79-1_amd64.deb",
  "vendor_url": "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/…",
  "origin": {"type": "apt", "signed_by": "<主密钥指纹>", "inrelease_sha256": "…"},
  "found": "2026-11-02T08:00:00Z",
  "status": "pending",
  "checks": {
    "deps": {"ok": true, "detail": "", "milkyway": "<检查时 milkyway Release 的 sha256>"},
    "inspect": {"scripts": {"postinst": "<sha256>"}, "flags": ["writes /etc/apt", "setuid usr/lib/x"]},
    "diff": {"against": "<上一版 version 或 null>", "scripts_changed": ["postinst"], "flags_added": [], "summary": "…"},
    "sandbox": {"status": "missing"}
  },
  "decided": null
}
```

- `origin.type`：`apt`（带 `signed_by`、`inrelease_sha256`）、`https`（带 `url`）、`manual`（带 `by`、`url`：人工下载的来源页）。
- `status`：`awaiting-upload` → `pending` → `approved` | `rejected` | `superseded` | `revoked`（同一应用有更新的候选被放行后，旧的 pending 自动变 superseded；`revoke` 撤销放行后变 revoked）。
- `checks.sandbox.status`：`missing` | `pass` | `fail`；导入报告后带 `report`（报告文件名）和 `summary`。
- maintainer 脚本原文和文件清单存在 `candidates/<app-id>/<version>_<sha12>.inspect/`（`control/` 目录 + `files.txt`），供 diff 和人工查看。

`state.py` 接口（签名固定，trackers/checks/cli 都按这个调用）：

```python
class State:
    def __init__(self, root: str)
    def lock(self) -> ContextManager                      # 拿不到锁抛 StateLocked
    def vendor_memo(self, vendor_id) -> dict              # 不存在返回 {}
    def save_vendor_memo(self, vendor_id, memo: dict) -> None
    def candidates(self, app=None, status=None) -> list[dict]   # 每项带 "_path"
    def find_candidate(self, app, version, sha256=None) -> dict | None
    def add_candidate(self, record: dict) -> dict         # 同 app+version+sha256 已存在则返回已有的
    def update_candidate(self, record: dict, changes: dict) -> dict   # 浅合并，返回新记录
    def inspect_dir(self, record: dict) -> str
    def approved(self) -> dict
    def set_approved(self, app, entry: dict, log: dict) -> None
    def append_log(self, entry: dict) -> None
    def milkyway(self) -> dict
    def save_milkyway(self, data: dict) -> None
    def catalog_state(self) -> dict
    def save_catalog_state(self, data: dict) -> None
    def cache_dir(self, name) -> str                    # state/<name>/，不存在就建
```

## 5. 命令 `agino-store-server [--config PATH] <命令>`

| 命令 | 做什么 | 模块 |
|---|---|---|
| `track [--vendor ID]` | 跟踪全部（或一个）厂商；新版本下载、校验、inspect、diff、依赖检查，写候选 | `trackers.run_track(cfg, st, content, vendor=None) -> list[dict]` |
| `check [--app ID]` | 拉 milkyway Packages，更新 `milkyway.json`，重查 pending 和已放行候选的依赖，A 类包不存在的报出来 | `checks.run_check(cfg, st, content, app=None) -> list[dict]`（返回问题列表） |
| `intake VENDOR FILE [--url 来源页]` | 人工下载的 deb 入库：包名、架构必须对，版本必须等于 awaiting-upload 的版本（没有就必须高于已放行的） | `trackers.intake(cfg, st, content, vendor, path, url) -> dict` |
| `pending [--json]` | 列出 pending / awaiting-upload：版本、大小、各项检查、diff 摘要 | cli |
| `approve APP[@VERSION] \| all [--no-sandbox] [--include-flagged] [--description-reviewed] [--note 文字]` | 放行。介绍来源（`description_source`）是 `ai` 时拒绝（退出码 1，"the description is AI-generated and not confirmed; confirm it or pass --description-reviewed"），给 `--description-reviewed` 才放行，`approvals.log` 的该行记 `"description_reviewed": true`（没给则为 false）；`all` 一律跳过并列出这类应用，`all` 不接受 `--description-reviewed`（退出码 2，要按名字放行）；其他来源不受影响。依赖失败的不能放行；沙箱不是 pass 的要 `--no-sandbox`（记入 log）。`all` 跳过不满足条件的并列出，也跳过有 inspect flags、`diff.scripts_changed` 或 `diff.flags_added` 的候选，除非给 `--include-flagged`；单个应用照常放行，但会先打印它放行的 flags 和改动的脚本 | cli |
| `revoke APP[@VERSION] [--note]` | 撤销放行：从 `approved.json` 删除该应用，对应候选记为 `revoked`，写入 `approvals.log`；下一次 `build` 不再包含它 | cli |
| `reject APP@VERSION [--note]` | 拒绝 | cli |
| `reports-ingest` | 导入 `paths.reports` 下的新报告 | `reports.ingest(cfg, st) -> list[dict]` |
| `sandbox-queue` | 把等沙箱结果的候选交给沙箱主机（见第 10 节） | `sandboxqueue.run(cfg, st, content) -> list[str]` |
| `shots-request APP [--for-session] \| --cancel APP` | 请沙箱主机为 APP 截图：写 `state/shots-requests/<app>.json`（`app`、`requested`、`by`）；APP 必须有有效的 `shots.yaml`。`--cancel` 撤回请求（普通和会话请求都可）。已有请求再请求不重复。`--for-session`（不能与 `--cancel` 同用）额外写 `session: true` 和 `expires`（现在起 2 小时，UTC）：交互会话守护进程需要队列里一直有该应用的任务，即使截图已存在；已有普通请求被升级，会话请求不会被之后的普通请求降级（见第 10 节） | cli、`state.shots_requests()` |
| `shots-import APP` | 把沙箱主机为 APP 拍的最新一组截图（`paths.shots_out/<APP>_<sha12>/` 里带 `shots.json` 的最新目录）导入截图目录，状态 `draft`、来源 `sandbox`、`origin` 为任务目录名；不改动 approved 的图，sha256 相同的跳过，超出每语言上限的报告并跳过。退出码 1 表示有图没能导入（见第 12 节） | `shots_import.cmd_import` |
| `shots-session-import APP --session ID --reviewer NAME --pick NN[,NN…]` | 把交互式截图会话（`paths.captures/<ID>/`，ID 为 16 位十六进制）里选中的图导入截图目录：状态 `draft`、来源 `manual`、`origin` 为 `session:<ID>`、场景 `session-NN`、语言取自文件名（`NN-<window\|screen>-<zh_CN\|en_US>.png`）。一个会话会截很多应用，只导入 `captures.json` 中 `app` 等于 `APP` 的截图，其余拒绝并计入 problems。按不可信数据读取（见第 12 节）；至多 40 个 `NN`，每个 `^[0-9]{2}$` 且不重复；会话截图体积上限 3 MiB（其他仍 1.5 MiB）；同语言已有 `max_per_lang` 张未拒绝的图时跳过并报告。退出码同 `shots-import`，用法错误为 2 | `shots_session.cmd_session_import` |
| `shots-ingest APP --lang L --scene S --file STAGED --source upload\|manual --status draft\|approved --reviewer NAME [--replace NN-scene.png]` | 取 `paths.shots_staging/STAGED` 的 PNG，校验、去掉附加块后存为 `<L>/NN-<S>.png`（或顶替 `--replace` 指定的图，旧图进 `.prev`），读完删除暂存文件 | `shots_cmds.cmd_ingest` |
| `shots-review APP --lang L --file NN-scene.png --status approved\|rejected\|draft --reviewer NAME [--note 文字]` | 改一张图的状态；approved 要重新校验文件并受每语言上限限制 | `shots_cmds.cmd_review` |
| `shots-remove APP --lang L --file NN-scene.png --reviewer NAME` | 把图移到 `.prev`，从 `meta.json` 去掉 | `shots_cmds.cmd_remove` |
| `content-edit APP --patch NAME.json --editor NAME` | 取 `paths.shots_staging/NAME.json` 的 JSON 补丁改 `apps/APP/app.yaml`，校验整个内容仓库，提交（可选推送）；全局锁；见第 13 节 | `content_edit.cmd_edit` |
| `content-confirm APP --editor NAME` | 把介绍标为人工确认（`description_source: confirmed` + `description_confirmed {by, at}`），提交（可选推送）；已确认则什么都不做（退出码 0）；全局锁；见第 13 节 | `content_edit.cmd_confirm` |
| `build [--if-changed]` | 生成并签名目录；`--if-changed`：输入没变且距上次不到 `renew_after_hours` 就什么都不做（输入没变但该续期了就只换 serial 和有效期） | `catalog.build(cfg, st, content, if_changed=False) -> dict` |
| `metrics` | 写 `agino_store_server.prom` | cli |
| `status --json [--section 名称 ...]` | 只读状态总览，给管理后台用（第 11 节）；不拿锁、不写、不联网 | cli、`status.build(cfg, sections, clean) -> str` |

`allow_insecure = true` 时命令行直接退出（码 2），除非环境变量 `AGINO_STORE_SERVER_TESTS=1`（测试设置它）。`pending` 和各命令输出里，厂商或候选提供的字符串会去掉控制字符（制表符、换行除外）。

退出码：0 成功；1 有问题需要人看（检查失败、跟踪出错、补丁或内容无效、推送失败）；2 用法或配置错误；3 锁被占用（`shots-*` 命令是某个应用的截图锁被占用；`content-*` 用全局锁）。

## 6. 跟踪和下载

- `fetch.py`：只用标准库 `urllib`；只允许 https（`allow_insecure` 时放行 http/file，仅测试）；每一跳重定向的主机都必须在 `allowed_hosts`；如实写 User-Agent（`[fetch] user_agent`），不伪装成浏览器；同一主机两次请求间隔 ≥ `min_interval_seconds`；边下边算 sha256，写到同目录临时文件，大小超过上限就中止；支持 `If-None-Match` / `If-Modified-Since`；超时 60 秒（连接）/ 900 秒（整体）。下载到 `paths.downloads/<sha256>/<原文件名>`，已存在就不重复下载。
- `aptrepo.py`：
  1. 下载 `<url>/dists/<suite>/InRelease`；用 `gpgv --status-fd 1 --keyring <临时 keyring>` 验签，keyring 由 `keys` 里的 `.asc` 现场转换（`gpg --dearmor`）；从 `VALIDSIG` 行取主密钥指纹（最后一个字段），必须在 `fingerprints` 里。
  2. `Date` 比上次记录的 `inrelease_date` 早就拒绝（防回滚）。
  3. 按 InRelease 里的 SHA256 下载 `<component>/binary-<arch>/Packages.gz`（没有就 `.xz`、再没有就无压缩），校验哈希和大小。
  4. 取 `Package == package` 且 `Architecture` 是 `arch` 或 `all` 的最高版本（`dpkg --compare-versions`）；下载 `Filename`，按 Packages 里的 `SHA256`、`Size` 校验。
- 新版本的判断：比已放行版本高，且同一版本还没有候选；同版本号出现不同 sha256 时不建新候选，记 `last_error` 并返回问题（告警）。
- 下载完成后由 `debinfo` 检查：`Package` 等于 vendor 的 `package`，`Architecture` 匹配，`Version` 与跟踪到的版本一致（`head`/`page` 没版本时以 control 为准）。

## 7. 检查

- **依赖**：`dose-debcheck --deb-native-arch=<arch> --explain --failures --bg <milkyway amd64 和 all 的 Packages> --fg <由候选 control 生成的一个 stanza>`。`--fg` 里的包都能装就是 ok；失败时 `detail` 放 dose 的解释（截断到 4 KiB）。milkyway 的 Packages 缓存在 `state/milkyway-cache/`，按 Release 的 SHA256 更新。设了 `[milkyway] keyring` 时取 `InRelease`，按厂商源同样的 gpgv 逻辑验签（主密钥指纹必须在 `fingerprints` 里），验签失败是 error；没设时取 `Release`，并给一条 alert "milkyway Release not signature-checked"。取不到时用最近的缓存，给一条带缓存时长（小时）的 alert。
- **inspect**：`dpkg-deb -e` 取 control 目录；`dpkg-deb -c` 取文件清单。标记（`flags`）：setuid/setgid 文件；`etc/apt/` 下的文件；systemd 单元（`lib/systemd/system`、`usr/lib/systemd/system`、`etc/systemd/system`）；`etc/cron.*`；`etc/sudoers.d`；`etc/profile.d`；udev 规则；maintainer 脚本里出现 `sources.list`、`apt-key`、`trusted.gpg`、`chmod 777`、`useradd`、`systemctl enable`、`setcap` 等关键词，以及设 setuid/setgid 位的 `chmod`（八进制特殊位 2/4/6，或符号 `+s`/`=s`；例如 QQ 的 postinst `chmod 4755 chrome-sandbox`）（只标出，不阻止）。
- **diff**：和该应用当前放行的候选比较：哪些 maintainer 脚本变了（统一 diff 存进 inspect 目录的 `scripts.diff`）、flags 增减、Depends/Pre-Depends/Conflicts/Breaks/Provides/Replaces 的变化。没有放行过的版本，`against` 为 null，所有 flags 都算新增。

## 8. 生成目录

- 放行记录的校验：`kind: deb` 的应用，候选必须是 `approved` 状态且依赖检查 ok；build 会流式重算下载目录里 deb 的 sha256，和放行时记录的值比对，不符的应用带原因跳过（不进目录）。`preview/debs/<sha>/<文件名>` 已存在时，只有它与源文件是同一个 inode 或哈希相同才保留，否则替换。
- 输入：内容仓库（校验通过）、`approved.json`、`milkyway.json`、截图目录里 approved 的图。`inputs_sha256` 是这些规范化后的哈希（内容仓库按文件路径排序后逐个哈希，含图标；截图目录部分是每张 approved 图的 `(应用, 语言, 文件名, sha256)`，没有任何 approved 图时不进哈希，所以只加草稿不触发重建，批准或撤回会）。
- 截图（D-203）：每个应用取 `paths.screenshots/<应用>/meta.json` 里状态为 `approved` 的图，按（语言, 文件名）排序，复制到 `assets/<sha256>.png`（与图标同一套按哈希命名的机制，进清单和签名），`width`/`height` 读自 PNG 头；两种语言的键都输出（没有图的为空列表）。应用有 approved 的图时目录优先于 `app.yaml` 的 `screenshots:`；都没有时字段为空，同以前。磁盘上缺失或与记录的 sha256 不符的图被略过，原因进 `build` 摘要的 `warnings`（命令行打印 `warning: …`），不会让应用掉出目录；`meta.json` 损坏同样只是警告。
- `kind: deb` 的应用没有放行版本时不进目录；`kind: milkyway` 的包在 `milkyway.json` 里没有时不进目录（`check` 会报出来）。
- `package.deb`：`version`、`url`（`deb_base_url + <sha256>/<文件名>`）、`vendor_url`、`sha256`、`size`、`installed_size`（字节）。deb 文件硬链接到 `preview/debs/<sha256>/<文件名>`。
- `updated`：deb 类用放行日期，milkyway 类用 `first_seen`。`releases`：当前版本 + `release_notes` 里有的旧版本，按版本降序，最多 5 条。
- `serial`：UTC 日期 `YYYYMMDD` × 100 + 当天序号（01–99），必须大于 `catalog.json` 里的上一个；`revision`：`r<serial>-<inputs_sha256 前 12 位>`。
- 写到 `preview/catalog-<serial>/`，校验通过后原子替换 `preview/current` 符号链接；只保留最近 `keep_catalogs` 份（按明确的目录名删除）。生成失败不动 `current`。如果换了 `current` 之后、保存状态之前崩溃，留下的 `catalog-<serial>` 不会卡住下一次构建：serial 已存在就顺延到下一个。`preview/debs/<sha>/` 中不被任何保留的目录引用、且超过 `valid_days + 7` 天的会按确切路径删除。签名要求私钥是当前用户拥有的普通文件，且 `mode & 0o077 == 0`；openssl 失败只报 "signing failed"，不带私钥路径。目录里单个图片资源不得超过 10 MiB。

**签名辅助程序**：服务进程（用户 `agino-store`）不应读到私钥。`[catalog] sign_command` 是一个用 shlex 切分的命令行；设了它就不能设 `key`（两者必须恰好设一个，否则 `ConfigError`）。生成目录时 `catalogwriter.sign` 运行这个命令：manifest 原始字节走 stdin，超时 60 秒，stdout 是 base64 签名，必须解码为 64 字节，再按原格式写 `manifest.json.sig`。命令失败、超时或输出不对一律报 "signing failed"，不带 stderr 内容。

辅助程序是 `server/agino-store-sign NAME`（只用 Python 3 标准库，安装到 `/usr/local/bin/agino-store-sign`）：

- `NAME` 必须匹配 `^[a-z][a-z0-9-]{0,31}$`，私钥是 `/etc/agino-store-sign/keys/NAME.pem`。目录 0700、私钥 0600，属主都是 `agino-store-sign`；私钥要求与 `catalogwriter._check_key_file` 相同（普通文件、属主是当前用户、无组/其他权限位）。只有同时设了 `AGINO_STORE_SERVER_TESTS=1` 时，环境变量 `AGINO_STORE_SIGN_KEYS` 才能改密钥目录（给测试用）。
- 从 stdin 读 manifest，最大 8 MiB；必须是 JSON 对象，`schema` 为 1，且有 `serial` 和 `expires`，否则拒绝。用 `openssl pkeyutl -sign -rawin` 签名，要求 64 字节，向 stdout 输出 base64 加换行；向 stderr 写一行日志（名字、serial、输入的 sha256），进 journal。
- 退出码：0 成功，2 用法或输入错误，1 签名失败。错误信息不含密钥路径。

sudoers 规则形如：

```
agino-store ALL=(agino-store-sign) NOPASSWD: /usr/local/bin/agino-store-sign preview
```

服务单元不能设 `NoNewPrivileges`，否则 sudo 的 setuid 不生效。

## 9. 沙箱报告

沙箱主机写、repo 拉回的报告（`paths.reports/*.json`），必须签名：每个 `<name>.json` 旁边要有 `<name>.json.sig`，内容是一行 base64，为对 JSON 原始字节的 Ed25519 签名。验证用的公钥放在 `[reports] keys` 目录（默认 `/etc/agino-store-server/sandbox-keys`）下的 `*.pub` 文件里，每个文件一行，原始 32 字节公钥的 base64；验证命令是 `openssl pkeyutl -verify -rawin -pubin -keyform DER -inkey <DER 公钥>`，DER 为 `302a300506032b6570032100` + 原始公钥。没有签名、签名不对、或没有配置公钥的报告一律移到 `reports/rejected/`；验签通过之前不解析内容。只当数据解析：

```json
{"schema": 1, "app": "chrome", "version": "156.0.8078.79-1", "sha256": "…",
 "status": "pass", "started": "…", "finished": "…", "host": "store",
 "checks": {"installed": true, "third_party_sources": [], "desktop_file": "google-chrome.desktop",
            "window_seen": true, "new_errors": 0, "removed_clean": true},
 "summary": "…"}
```

沙箱写报告时文件名用 `<应用>_<sha12>.json` 加 `<应用>_<sha12>.json.sig`（`sha12` 是候选 sha256 的前 12 位十六进制，与第 10 节队列里的文件同名）。文件名只是约定：导入时不看文件名，只看内容里的字段。

`reports.ingest(cfg, st, keys_dir)` 按 `app` + `version` + `sha256` 找候选；找不到或字段不对的报告（连同 `.sig`）移到 `reports/rejected/`，处理过的移到 `reports/done/`。

符号链接安全：`reports/`、`done/`、`rejected/` 都用 `O_DIRECTORY|O_NOFOLLOW` 打开，用目录 fd 列目录、`lstat` 式检查、读文件和 `rename`；`done/` 或 `rejected/` 是符号链接或不是目录时 `reports-ingest` 报错退出（码 1），什么都不移动。

## 10. 沙箱队列 `sandbox-queue`

沙箱主机不碰状态目录。`agino-store-server sandbox-queue` 在状态锁内把需要测试的候选放进 `paths.sandbox_queue`（默认 `/srv/store/sandbox/queue`，目录属组 `agino-sandbox`、setgid；程序不 chown）。

对每个 `status` 为 `pending`、`checks.sandbox.status` 为 `missing` 且已下载文件的候选，队列里要有一对文件（`sha12` 为候选 sha256 前 12 位十六进制，权限都是 0640）：

- `<应用>_<sha12>.deb`：`downloads/` 里那个 deb 的副本。先写成同目录临时文件再 rename；用 `copy_file_range` 复制，XFS 上会 reflink。
- `<应用>_<sha12>.json`：**在 deb 之后**原子写入，所以沙箱看到 JSON 时 deb 一定已完整。UTF-8，键排序：

```json
{"schema": 1, "app": "chrome", "version": "156.0.8078.79-1", "sha256": "…", "size": 123456,
 "package": "google-chrome-stable", "filename": "google-chrome-stable_156.0.8078.79-1_amd64.deb",
 "deb": "chrome_0123456789ab.deb", "launchable": "google-chrome.desktop",
 "install_profile": {"debconf": ["…"], "default_files": [{"path": "/etc/default/…", "content": "…"}]},
 "queued": "2026-11-02T08:00:00Z"}
```

`launchable` 和 `install_profile` 取自 store-content 里该应用的 `app.yaml`（已校验过的值），没有就是 `null`。`package` 是 deb 的包名。

- 已有的一对且 JSON 内容（`queued` 除外）一致、deb 大小对：不动，所以重复运行没有输出。JSON 内容变了（比如 app.yaml 改了 `launchable`）只重写 JSON；deb 不在或大小不对则先删掉 JSON 再重放整对。
- 候选不再是 pending 且沙箱缺失（已放行、拒绝、被取代、已有沙箱结果）时，对应的一对从队列删除，先删 JSON 再删 deb。只删文件名严格匹配 `^[a-z0-9][a-z0-9-]*_[0-9a-f]{12}\.(json|deb)$` 的项，别的文件不碰；超过 1 小时的自己的临时文件（`.tmp-sbxq-` 前缀）也清掉。
- 符号链接安全：队列目录用 `O_DIRECTORY|O_NOFOLLOW` 打开（是符号链接或不是目录则报错，退出码 1），之后一律用目录 fd、`O_NOFOLLOW`、`O_EXCL` 临时文件和 `rename`；沙箱用户在目录里放的符号链接不会被跟随，要删的话只删链接本身。

应用有有效的 `shots.yaml` 时，JSON 多一个 `shots`（`{"schema": 1, "window": …, "scenes": […]}`，即上面校验后的结果，不含 `promo`）。只要有场景用到 demo 树，队列里还有第三个文件：

- `<应用>_<sha12>.demo.tar`：所用 demo 树（去重、按名字排序）的未压缩 POSIX tar，成员名 `<name>/…`，只有普通文件和目录，成员按路径排序，uid/gid 0、uname/gname 为空、mtime 0、文件 0644、目录 0755，所以同样内容得到逐字节相同的文件。和 deb 一样先写临时文件再 rename（`O_NOFOLLOW`、`O_EXCL`），**先于 JSON** 写入；JSON 里加 `"demo": "<应用>_<sha12>.demo.tar"` 和 `"demo_size": <字节数>`。

已有的 tar 与要写的内容（比较 sha256）一致就不动；内容变了先删 JSON、重写 tar、再写 JSON；不再需要 demo 时 tar 随之删除。清理时文件名正则扩为 `\.(json|deb|demo\.tar)$`，仍然只删严格匹配的名字，JSON 先于 deb 和 tar 删除。

每个新增、更新、删除输出一行（`queued|updated|removed ...`），退出码 0。沙箱主机处理完一个条目后把报告写回 `paths.reports`（第 9 节）；候选一旦有了沙箱结果，下一次 `sandbox-queue` 就把它从队列里清掉。


### 只截图的任务（`shots-request`）

milkyway 里的应用不进沙箱队列，已通过沙箱的候选也不会再入队，所以截图有单独的任务类型。`shots-request APP` 在状态锁内写 `state/shots-requests/<app>.json`（`{"app", "requested", "by"}`，原子写入，`by` 与放行记录用同一个操作者名）；`shots-request --cancel APP` 删除它。`shots-request APP --for-session` 写的是会话请求：多一个 `"session": true` 和 `"expires"`（UTC，当前时间 + 2 小时）；已有普通请求被升级，会话请求不会被降级，再次 `--for-session` 只刷新 `expires`。

`sandbox-queue` 对每个未关闭的请求导出一个 `"mode": "shots"` 的任务（原有任务没有 `mode`，即 check），并带 `shots` 和 demo tar，写法同上：

- 厂商应用（包类型不是 milkyway）：`status` 为 `pending` 或 `approved` 且已下载文件的候选中版本最新的一个；名字 `<应用>_<sha12>`，deb 照常复制，`"source": "deb"`，其余字段（`version`、`sha256`、`size`、`package`、`filename`、`deb`、`launchable`、`install_profile`）同 check 任务。没有这样的候选则输出 `skipped shots APP: …`，请求保留。
- milkyway 应用：`"source": "milkyway"`、`package`（应用的 milkyway 包名）、`version`（缓存的 milkyway 索引，即 `milkyway.json`）、`"sha256": null`，没有 deb 和 `install_profile`；名字 `<应用>_<h12>`，`h12` 为 `sha256("milkyway:" + 包名 + "=" + 版本)` 前 12 位十六进制，符合同一个名字正则。包不在 milkyway 里则输出 `skipped shots APP: package … is not in milkyway`，请求保留。
- 名字冲突：如果同一个厂商候选此刻也需要 check 任务（名字相同），只导出 check 任务，请求保持打开。该 check 任务的 JSON 在应用有 `shots.yaml` 时本来就带 `shots`，所以它产出的 `shots.json` 同样关闭这个请求。shots 任务和 check 任务从不共用名字。
- 会话请求（`session: true`）不会因为 `shots.json` 存在而关闭：只要没过期，即使应用已有截图，也照常导出同一个 shots 任务（同样的 JSON 和 demo tar），`agino_store_sandbox` 的会话守护进程据此找任务。过期（`expires` 已到、缺失或不可读）时请求删除，输出 `expired session shots request APP`，任务随后按“不再需要”清掉。
- 关闭（普通请求）：`<shots_out>/<任务名>/shots.json` 存在（普通文件）时，请求删除，输出 `closed shots request …`，队列里对应的条目随后按“不再需要”清掉。`shots_out`（`[paths] shots_out`，默认 `/srv/store/sandbox/shots-out`，目录对 `agino-store` 组可读）里的项一律用 `O_NOFOLLOW` 打开，符号链接不算；`shots_out` 不存在或读不了时什么都不关闭。

## 11. `status --json`

`agino-store-server status --json [--section NAME ...]` 是管理后台（agino-store-admin）读状态的唯一接口，后台不解析内部文件格式。严格只读：不拿 `state/.lock`（所以和任何写命令并行都行），不写文件，不调用 sudo，不联网。状态 JSON 是原子替换的，每个文件只打开读一次（`O_NOFOLLOW`，必须是普通文件，最大 8 MiB）；文件缺失、读到一半或 JSON 无效时，记入 `errors` 后继续。退出码 0（有 `errors` 也是 0）；`--json` 缺失或段名未知退出码 2。

输出一个 JSON 对象（ensure_ascii，示例见 `docs/status-schema.md`）：`{"schema": 1, "generated": "<UTC>", "sections": {…}, "errors": [{"section", "detail"}]}`。`--section` 可重复，默认全部。已实现：`apps`、`candidates`、`approved`、`vendors`、`catalog`、`sandbox`、`timers`、`shots`、`categories`；`upstreams`、`submissions`、`sessions`、`builds` 是预留名，现在接受但输出里没有这一段。

| 段 | 内容 |
|---|---|
| `apps` | 列表，每项：`id`、`name`、`summary`（`{zh_CN, en_US}`）、`description`（`{zh_CN: [段落…], en_US: […]}`，全文）、`license`、`categories`、`source`、`kind`、`package`、`vendor`、`proprietary`、`developer`（`{zh_CN, en_US}`）、`homepage`、`maintainer`、`launchable`、`has_icon`、`description_source`（缺省 `submitter`）、`description_confirmed`（`{by, at}` 或 null）、`l10n`（布尔）、`shots`（`{state: none\|draft\|approved, scenes, langs, counts: {draft, approved, rejected}}`，来自 `paths.screenshots/<应用>/meta.json`；至少一张 approved 的图就是 approved，否则有 draft 为 draft，否则 none；`scenes`、`langs` 只数没被 rejected 的图）、`approved`（`{version, approved_at, by}` 或 null）、`current_candidate_status`（版本最高的候选的状态）。内容仓库校验失败时这一段为空，每条问题记入 `errors` |
| `candidates` | 全部候选，按 `found` 从新到旧：`id`（`<应用>/<文件名去 .json>`）、`app`、`vendor`、`package`、`version`、`size`、`installed_size`、`found`、`status`、`origin`（`type`，apt 带 `signed_by`）、`source_class`（`official`＝apt/https，`personal`＝manual/built，否则 `unknown`）、`checks`（`deps{ok,detail}`、`inspect{flags}`、`diff{against,scripts_changed,flags_added,summary}`、`sandbox{status,summary,report}`）、`decided`。不含脚本原文和文件清单 |
| `approved` | `{apps: approved.json 原样, log: approvals.log 最后 50 行解析后的对象（坏行跳过）}` |
| `vendors` | 每个厂商：`id`、`name`（厂商包名）、`in_content`、`last_check`、`last_ok`、`last_error`、`seen_version`；内容仓库里的和 `state/vendors/` 里有记录的取并集 |
| `catalog` | `{state: {serial, revision, built_at, inputs_sha256, apps}, published: {serial, revision, created, expires, valid_until, apps, signature_present} 或 null}`；`published` 读 `paths.preview/current/` 的 `manifest.json` 和 `catalog.json`，只看签名文件在不在，不验签 |
| `sandbox` | `{queue: [{job, mode(check\|shots), app, version, size, created}], jobs: [{job, status, result, attempts, reason, report, updated}]（`paths.sandbox_state/*.json`）, reports: 最新 30 份（`paths.reports/` 和 `done/`）[{name, location, app, version, result, finished, at, summary}]}` |
| `shots` | 内容仓库里每个应用一项：`id`、`counts`（`{draft, approved, rejected}`）、`images`（按语言、文件名排序，每张图是 `meta.json` 里的完整记录：`lang`、`file`、`scene`、`width`、`height`、`size`、`sha256`、`source`、`status`、`reviewer`、`reviewed`、`note`、`origin`），管理后台一次调用读全部。目录或 `meta.json` 缺失时 `images` 为空，`meta.json` 损坏记入 `errors`（段名 `shots`） |
| `categories` | `categories.yaml` 的列表：`[{id, name: {zh_CN, en_US}}]`，顺序同文件；内容仓库校验失败时为空列表（问题记入 `errors`，段名 `apps`） |
| `timers` | `agino-store-{track,check,build,sandbox,reports}.{service,timer}` 十个单元的 `systemctl show`（固定参数，不经 shell，每个 5 秒超时）：`unit`、`LoadState`、`ActiveState`、`SubState`、`Result`、`LastTriggerUSec`、`NextElapseUSecRealtime`、`ExecMainStatus`。没有 systemctl 时为空列表并记入 `errors` |

所有来自厂商、候选、沙箱和内容仓库的字符串先过 `pending` 用的控制字符清理（控制字符换成 `?`，包括键名）。文档超过 8 MiB 时，把最长的列表（`apps`、`candidates`、`approved.log`、`vendors`、`sandbox.*`）反复砍掉后一半，被砍的段名写进顶层 `"truncated"`。

`config.py` 新增可选键 `[paths] sandbox_state`（只有 `status` 用）。截图目录相关的键见第 12 节。

## 12. 截图目录（D-203，取代 D-192 的“截图进 store-content”）

截图放在服务端管理的固定目录 `paths.screenshots`（默认 `/srv/store/screenshots`），不在内容仓库里。管理后台（agino-store-admin）只通过 `shots-*` 命令和 `status --json --section shots` 使用它。

```
<root>/<app>/meta.json                       审核状态，原子写入，0644
<root>/<app>/zh_CN/NN-<scene>.png            en_US 同
<root>/<app>/.prev/<UTC 时间戳>/<lang>/<文件>  被替换或删除的图（同一秒内重复时时间戳加 -N），永不销毁
<root>/<app>/.lock                           每个应用一把 flock
```

`meta.json`（`ensure_ascii`，键排序）：

```json
{"schema": 1, "app": "gimp", "updated": "2026-11-03T08:00:00Z", "images": [
  {"lang": "zh_CN", "file": "01-main.png", "scene": "main", "sha256": "…", "size": 123456, "width": 1280, "height": 800,
   "source": "sandbox", "status": "draft", "reviewer": null, "reviewed": null, "note": "", "origin": "gimp_0123456789ab"}]}
```

- `source`：`sandbox`（`shots-import`）、`upload`、`manual`、`vendor`；`status`：`draft`、`approved`、`rejected`，目录只用 approved。`reviewer`（`^[A-Za-z0-9._-]{1,32}$`）和 `reviewed` 在 draft 时为 null；`note`、`origin` 各不超过 200 个字符。
- 规则：只收 PNG；宽度 >= `[shots] min_width`（900），高度 >= 300，宽高都 <= 4096；去掉附加块后文件 <= 1.5 MiB；同一应用同一语言没被 rejected 的图最多 `[shots] max_per_lang`（6）张；文件名 `NN-<scene>.png`（NN 两位，场景 `^[a-z0-9][a-z0-9-]{0,40}$`）在一个语言内唯一，新图的 NN 取已有最大值加 1。同一语言里与没被 rejected 的图 sha256 相同的图不收（`shots-import` 连 rejected 的也算，所以被拒的图不会再次导入）。
- 校验和清理（`pngclean.py`）：签名、块的 CRC、IHDR（颜色类型、位深、不接受未知方法）、宽高上限先于解压检查；zlib 数据按头里推出的精确大小限量解压，大小必须吻合，IEND 后不得有数据。只保留 `IHDR`、`PLTE`、`tRNS`、`IDAT`、`IEND`，其余块（文本、EXIF、色彩配置等）丢弃，不做重新编码。
- 锁：写命令只拿 `<root>/<app>/.lock`（`flock`，不等待，被占用退出码 3），不拿全局的 `state/.lock`，也不碰候选和放行记录；`status` 和 `build` 不加锁（`meta.json` 是原子替换的）。截图目录不存在时写命令报错退出（码 1），不会自己创建。`meta.json` 不合 schema 时写命令拒绝执行（不会覆盖），`status`/`build` 把它当警告。
- 参数全部严格校验，错误退出码 2：应用 id `^[a-z0-9][a-z0-9-]{0,63}$` 且内容仓库里有 `apps/<id>/app.yaml`；语言 `zh_CN`/`en_US`；场景、文件名、审核人同上；备注不超过 200 个字符、无控制字符、合法 UTF-8、不以 `-` 开头（用 `--note=…` 传）。
- `shots-import`：目录名 `<app>_<12 位十六进制>` 且含普通文件 `shots.json` 的最新一个（按 `shots.json` 的 mtime）。每个场景每种语言取 `<n>-<scene>.png`（按 `shots.json` 的 `n`、`id` 核对文件名和声明的 sha256，`O_NOFOLLOW` 读取），重新校验并清理。同场景已有 approved 或非 sandbox 来源的图则跳过；已有 sandbox 草稿则顶替（旧图进 `.prev`）。输出每张图一行（`imported`/`skipped`/`problem`）和 `job …: imported N, skipped N, problems N`；有 problem（坏图、太小、读不到、sha256 不符）时退出码 1，其余的图照常导入。
- `shots-session-import`：交互式会话（agino-store `docs/sandbox.md` 第 9 节）的截图由沙箱主机上的 `agino-sandbox` 写入，里面的内容最终来自 guest，所以比 `shots-ingest` 的暂存文件更不可信：`paths.captures` 经文件描述符打开，会话目录用 `O_DIRECTORY|O_NOFOLLOW`（符号链接、非目录都拒绝）；只认文件名恰为 `NN-window|screen-zh_CN|en_US.png` 的项（列目录得到，同一个 `NN` 必须恰有一个文件），以 `O_NOFOLLOW|O_NONBLOCK` 打开，必须是普通文件且 <= 4 MiB；`captures.json` 只用来确认每张图属于哪个应用（`O_NOFOLLOW`、<= 1 MiB、只认格式正确的项；项的 `n`、`file`、`app` 必须与文件名和 `APP` 一致，读不了或格式不对则一张都不导入），其余字段一律不用（种类和语言来自文件名，宽、高、sha256 全部由解码后的图重新计算）；PNG 头必须是 8 位 RGB、非隔行（守护进程只写这种，也把解码内存限在 3 字节/像素以内），再经 `pngclean` 去掉附加块（宽高 <= 4096，数据长度必须与头一致），然后按 `check_png` 校验（宽 >= `min_width`、高 >= 300、去块后 <= 3 MiB）。体积上限 3 MiB 只对 `source` 为 `manual` 且 `origin` 以 `session:` 开头的图生效，`shots-review --status approved` 的重新校验同样按这个上限。sha256 与任何同语言已有图（含 rejected）相同的跳过；每个坏项只记 `problem` 行，其余照常导入。输出同 `shots-import`，末行 `session ID: imported N, skipped N, problems N`。
- `shots-ingest`：暂存文件名 `^[A-Za-z0-9_-]{8,64}\.png$`，以 `O_NOFOLLOW` 打开，必须是普通文件，<= 4 MiB；成功后才删除暂存文件（失败时留着）。`--status approved` 同时写入审核人和时间；draft 两者为 null。`--replace` 的旧图进 `.prev`，新图沿用其 NN（场景不同则文件名里的场景也换）。
- `shots-review`：approved 要求文件仍然通过全部校验（含 sha256 与记录相符、不含不允许的块）且不超每语言上限；改回 draft 清空审核人和时间；`--note` 缺省时保留原备注。rejected 的图留在磁盘上，不删除。
- `shots-remove`：图移到 `.prev`，从 `meta.json` 去掉；`--reviewer` 只用于校验和输出。
- 回退：把图改回 draft/rejected，或把 `.prev` 里的文件放回去并相应修改 `meta.json`，下一次 `build --if-changed` 生效。

## 13. 应用信息的修改和介绍确认（D-203）

管理后台的"应用信息"标签通过两条命令改 `apps/<id>/app.yaml`，两条都不让后台直接碰内容仓库。设计见 agino-store-admin 的 `docs/design.md` 7.6。

**介绍来源**（`app.yaml` 的 `description_source`，缺省 `submitter`）：`ai`（AI 撰写，放行前必须人工确认）、`confirmed`（人工改过或确认过，可带 `description_confirmed {by, at}`）、`submitter`（提交者写的）、`official`（官方来源的）。`approve` 对 `ai` 的限制见第 5 节。

**`content-edit APP --patch NAME.json --editor NAME`**
- 参数全部严格校验，错误退出码 2：`APP` 同 `shots-*`（内容仓库里有 `apps/<id>/app.yaml`）；`NAME` 为 `^[A-Za-z0-9_-]{8,64}\.json$`；`--editor` 为 `^[A-Za-z0-9._-]{1,32}$`。
- 补丁文件在 `paths.shots_staging`（和截图上传同一个暂存目录），以 `O_NOFOLLOW` 打开，必须是普通文件，<= 64 KiB，UTF-8 JSON 对象，不许重复键和 `NaN`。成功后删除，失败（包括补丁无效）时留着。
- 允许的键，其他一律拒绝（退出码 1，补丁不能带 `description_source`）：`name`、`summary`、`developer`（`{zh_CN, en_US}`，上限 100/300/100 字符，单行）、`description`（`{zh_CN: [段落], en_US: [段落]}`，每种语言 1..50 段，每段 <= 4000 字符）、`categories`（1..2 个，必须在 `categories.yaml` 里，不重复）、`license`（`content.py` 的许可证格式）、`proprietary`（布尔）、`homepage`（https URL，无用户名密码）。本地化键必须同时给出 `zh_CN` 和 `en_US`，且只能有这两个；一个键出现就替换它的值（本地化键里补丁没提到的其他语言保留）。所有文字不得含控制字符（含换行、制表符），也不得含看起来像标记的 `<` 加字母或 `/`（`1 < 2` 没问题）。
- 语义：先校验当前内容仓库（本来就无效则退出码 1，不动文件），再应用补丁。补丁没改变任何值则输出 `no changes for APP`、退出码 0、只删补丁文件。`description` 或 `summary` 的值改变了就把 `description_source` 设为 `confirmed`、`description_confirmed` 设为 `{by: EDITOR, at: 现在(UTC)}`（人改过文字就算审阅过），两个键紧跟在 `description` 后面；只改了别的键则介绍来源不变。
- 写回用 `yaml.safe_dump(sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000000)`（宽度取很大，长段落不会被折行）。现有 store-content 的 `app.yaml` 里只有一处与它不同：`l10n` 的行内列表 `[a]` 会被展开成块列表，含义相同；测试用样例文件验证其余文件逐字节一致。
- 然后用 `content.load` 校验整个内容仓库；失败则恢复原文件，退出码 1，输出错误。
- git（`content_git.py`，在 `paths.content` 里）：开始前要求它是 git 检出且 `apps/<id>/app.yaml` 没有未提交的改动；`git add apps/<id>/app.yaml`，`git -c user.name=agino-store -c user.email=agino-store@localhost commit -m "info(<APP>): <改动的键> by <EDITOR>" -- apps/<id>/app.yaml`（只提交这个文件；关闭钩子和签名）。`[content] push = yes` 时 `git push origin HEAD:main`；被拒绝则做**一次** `git pull --rebase --autostash origin main` 再推；还是失败就撤销这次提交（`git reset --keep`，回到我们提交之前，已拉下来的别人的提交保留），不改别的，退出码 1，补丁留着。`push = no`（默认）只在本地提交，并输出 `warning: not pushed (content.push is off)`。
- 所有 git 命令用固定的参数列表、不经 shell、60 秒超时，环境只保留 `PATH`、`HOME`、`GIT_SSH_COMMAND`（取自 `push_ssh_command`，空则不设）和 `GIT_TERMINAL_PROMPT=0`。
- 锁：全局 `state/.lock`（和 `track`、`build`、其他修改互斥，被占用退出码 3）。
- 回退：在内容仓库里 `git revert` 对应提交，下一次 `build` 恢复。

**`content-confirm APP --editor NAME`**：把 `description_source` 设为 `confirmed`，`description_confirmed` 设为 `{by, at}`（来源原本是 `ai`、`submitter` 或 `official` 都行），按上面的 git 流程提交 `info(<APP>): confirm description by <EDITOR>`。已经是 `confirmed` 则输出 `description of APP is already confirmed`，退出码 0，不提交。同样拿全局锁。
