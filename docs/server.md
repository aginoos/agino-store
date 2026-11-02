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
tests/server/    unittest；fixture 在测试里现场生成（gpg 临时密钥、dpkg-deb 打的小包、本地 http 服务）
```

依赖（Debian 包）：`python3`（3.13）、`python3-yaml`、`gpgv`、`gpg`（只在测试里生成密钥）、`dpkg`、`dose-distcheck`、`openssl`（签名，与 make-catalog 相同）。

## 2. 配置 `/etc/agino-store-server/server.conf`

INI（configparser），全部是路径和地址，没有机密（私钥只给路径）：

```ini
[paths]
content = /srv/store/content          # store-content 的检出
state = /srv/store/state
downloads = /srv/store/downloads      # deb：<sha256>/<原文件名>
preview = /srv/store/preview          # 生成的目录：catalog-<serial>/ 和 current -> catalog-<serial>
reports = /srv/store/reports          # 从沙箱主机拉回的报告
metrics = /var/lib/prometheus/node-exporter

[milkyway]
url = https://repo.aginoos.com        # 取 dists/milkyway/<component>/binary-{amd64,all}/Packages.gz
suite = milkyway
components = main contrib non-free
arch = amd64
keyring = /usr/share/keyrings/agino-archive-keyring.gpg   # 可选；设了就取 InRelease 并验签
fingerprints = <主密钥指纹，空格分隔>                      # 设了 keyring 就必须设

[catalog]
key = /etc/agino-store-server/keys/preview.pem   # Ed25519 私钥（PEM）
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
apps/<id>/screenshots/zh_CN/*.png, en_US/*.png     （可选）
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
- 截图的宽高由 build 从 PNG 头读出；PoC 不生成缩略图（`thumb` 与 `image` 相同）。
- 未知字段报错（防止拼错的字段被静默忽略）。

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
| `approve APP[@VERSION] \| all [--no-sandbox] [--include-flagged] [--note 文字]` | 放行。依赖失败的不能放行；沙箱不是 pass 的要 `--no-sandbox`（记入 log）。`all` 跳过不满足条件的并列出，也跳过有 inspect flags、`diff.scripts_changed` 或 `diff.flags_added` 的候选，除非给 `--include-flagged`；单个应用照常放行，但会先打印它放行的 flags 和改动的脚本 | cli |
| `revoke APP[@VERSION] [--note]` | 撤销放行：从 `approved.json` 删除该应用，对应候选记为 `revoked`，写入 `approvals.log`；下一次 `build` 不再包含它 | cli |
| `reject APP@VERSION [--note]` | 拒绝 | cli |
| `reports-ingest` | 导入 `paths.reports` 下的新报告 | `reports.ingest(cfg, st) -> list[dict]` |
| `build [--if-changed]` | 生成并签名目录；`--if-changed`：输入没变且距上次不到 `renew_after_hours` 就什么都不做（输入没变但该续期了就只换 serial 和有效期） | `catalog.build(cfg, st, content, if_changed=False) -> dict` |
| `metrics` | 写 `agino_store_server.prom` | cli |

`allow_insecure = true` 时命令行直接退出（码 2），除非环境变量 `AGINO_STORE_SERVER_TESTS=1`（测试设置它）。`pending` 和各命令输出里，厂商或候选提供的字符串会去掉控制字符（制表符、换行除外）。

退出码：0 成功；1 有问题需要人看（检查失败、跟踪出错）；2 用法或配置错误；3 锁被占用。

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
- **inspect**：`dpkg-deb -e` 取 control 目录；`dpkg-deb -c` 取文件清单。标记（`flags`）：setuid/setgid 文件；`etc/apt/` 下的文件；systemd 单元（`lib/systemd/system`、`usr/lib/systemd/system`、`etc/systemd/system`）；`etc/cron.*`；`etc/sudoers.d`；`etc/profile.d`；udev 规则；maintainer 脚本里出现 `sources.list`、`apt-key`、`trusted.gpg`、`chmod 777`、`useradd`、`systemctl enable` 等关键词（只标出，不阻止）。
- **diff**：和该应用当前放行的候选比较：哪些 maintainer 脚本变了（统一 diff 存进 inspect 目录的 `scripts.diff`）、flags 增减、Depends/Pre-Depends/Conflicts/Breaks/Provides/Replaces 的变化。没有放行过的版本，`against` 为 null，所有 flags 都算新增。

## 8. 生成目录

- 放行记录的校验：`kind: deb` 的应用，候选必须是 `approved` 状态且依赖检查 ok；build 会流式重算下载目录里 deb 的 sha256，和放行时记录的值比对，不符的应用带原因跳过（不进目录）。`preview/debs/<sha>/<文件名>` 已存在时，只有它与源文件是同一个 inode 或哈希相同才保留，否则替换。
- 输入：内容仓库（校验通过）、`approved.json`、`milkyway.json`。`inputs_sha256` 是这三者规范化后的哈希（内容仓库按文件路径排序后逐个哈希，含图标和截图）。
- `kind: deb` 的应用没有放行版本时不进目录；`kind: milkyway` 的包在 `milkyway.json` 里没有时不进目录（`check` 会报出来）。
- `package.deb`：`version`、`url`（`deb_base_url + <sha256>/<文件名>`）、`vendor_url`、`sha256`、`size`、`installed_size`（字节）。deb 文件硬链接到 `preview/debs/<sha256>/<文件名>`。
- `updated`：deb 类用放行日期，milkyway 类用 `first_seen`。`releases`：当前版本 + `release_notes` 里有的旧版本，按版本降序，最多 5 条。
- `serial`：UTC 日期 `YYYYMMDD` × 100 + 当天序号（01–99），必须大于 `catalog.json` 里的上一个；`revision`：`r<serial>-<inputs_sha256 前 12 位>`。
- 写到 `preview/catalog-<serial>/`，校验通过后原子替换 `preview/current` 符号链接；只保留最近 `keep_catalogs` 份（按明确的目录名删除）。生成失败不动 `current`。如果换了 `current` 之后、保存状态之前崩溃，留下的 `catalog-<serial>` 不会卡住下一次构建：serial 已存在就顺延到下一个。`preview/debs/<sha>/` 中不被任何保留的目录引用、且超过 `valid_days + 7` 天的会按确切路径删除。签名要求私钥是当前用户拥有的普通文件，且 `mode & 0o077 == 0`；openssl 失败只报 "signing failed"，不带私钥路径。目录里单个图片资源不得超过 10 MiB。

## 9. 沙箱报告

沙箱主机写、repo 拉回的报告（`paths.reports/*.json`），必须签名：每个 `<name>.json` 旁边要有 `<name>.json.sig`，内容是一行 base64，为对 JSON 原始字节的 Ed25519 签名。验证用的公钥放在 `[reports] keys` 目录（默认 `/etc/agino-store-server/sandbox-keys`）下的 `*.pub` 文件里，每个文件一行，原始 32 字节公钥的 base64；验证命令是 `openssl pkeyutl -verify -rawin -pubin -keyform DER -inkey <DER 公钥>`，DER 为 `302a300506032b6570032100` + 原始公钥。没有签名、签名不对、或没有配置公钥的报告一律移到 `reports/rejected/`；验签通过之前不解析内容。只当数据解析：

```json
{"schema": 1, "app": "chrome", "version": "156.0.8078.79-1", "sha256": "…",
 "status": "pass", "started": "…", "finished": "…", "host": "store",
 "checks": {"installed": true, "third_party_sources": [], "desktop_file": "google-chrome.desktop",
            "window_seen": true, "new_errors": 0, "removed_clean": true},
 "summary": "…"}
```

`reports.ingest(cfg, st, keys_dir)` 按 `app` + `version` + `sha256` 找候选；找不到或字段不对的报告（连同 `.sig`）移到 `reports/rejected/`，处理过的移到 `reports/done/`。

符号链接安全：`reports/`、`done/`、`rejected/` 都用 `O_DIRECTORY|O_NOFOLLOW` 打开，用目录 fd 列目录、`lstat` 式检查、读文件和 `rename`；`done/` 或 `rejected/` 是符号链接或不是目录时 `reports-ingest` 报错退出（码 1），什么都不移动。
