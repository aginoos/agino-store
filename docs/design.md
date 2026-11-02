# agino-store 设计文档

版本：0.1.0（2026-11-02）。决策来源：agino infra `docs/decisions.md` D-192；总体设计 infra `docs/app-store.md`；界面稿 infra `docs/design/12-app-store-client.html`。本文是实现和测试的依据；实现中发现与本文不符时，先改本文再改代码。

## 1. 目标与范围

agino 的应用商店客户端和特权辅助程序：

- `agino-store`：客户端（Qt6/QML + DTK），用户权限运行。浏览、搜索、详情、安装、更新、卸载、任务、设置。
- `agino-store-helper`：root 辅助程序，D-Bus 系统服务，按需激活。唯一的特权入口：拉取并验证目录、下载厂商 deb、校验、调用 PackageKit 安装和卸载。

第一版（0.1.0）是可运行骨架：客户端做出界面稿的全部页面，读本地已验证的目录；helper 实现下面的 D-Bus 接口、目录验签、PackageKit 安装卸载 milkyway 包和目录里的 deb；polkit 规则；能打成 deb。服务端流水线和真实目录在 infra 里另做。

不做：账号、评分、付费、遥测；第三方 apt 源（客户端永远不写 `/etc/apt/`）。

## 2. 组件和文件

```
src/common/   libagino-store-common（静态库）：目录数据结构与解析、清单验签、sha256、语言回退
src/helper/   agino-store-helper：D-Bus 服务、polkit 检查、任务队列、下载、PackageKit、安装后检查
src/client/   agino-store：QML 界面、C++ 模型、D-Bus 客户端、设置（DConfig）、桌面图标
data/         D-Bus 接口 XML 和服务文件、polkit 动作和规则、systemd 单元、desktop 文件、图标、DConfig 元数据、默认配置
tests/        QtTest 单元测试；tests/data/ 测试目录和测试密钥（只用于测试，不安装）
tools/        make-catalog.py：从目录源数据生成清单并签名（服务端和测试共用）
translations/ Qt 翻译（源字符串英文，第一版带 zh_CN）
```

安装后的路径：

| 路径 | 内容 | 属于 |
|---|---|---|
| `/usr/bin/agino-store` | 客户端 | agino-store |
| `/usr/libexec/agino-store-helper` | helper | agino-store-helper |
| `/usr/share/dbus-1/system.d/org.aginoos.Store1.conf` | 总线策略：root 拥有名字，所有人可调用 | agino-store-helper |
| `/usr/share/dbus-1/system-services/org.aginoos.Store1.service` | D-Bus 激活（`SystemdService=agino-store-helper.service`） | agino-store-helper |
| `/usr/lib/systemd/system/agino-store-helper.service` | `Type=dbus`，`BusName=org.aginoos.Store1` | agino-store-helper |
| `/usr/share/polkit-1/actions/org.aginoos.store.policy` | 动作 `org.aginoos.store.manage` | agino-store-helper |
| `/usr/share/polkit-1/rules.d/50-agino-store.rules` | sudo 组本地活动会话免密 | agino-store-helper |
| `/etc/agino-store/store.conf` | 配置（conffile） | agino-store-helper |
| `/usr/share/agino-store/keys/*.pub` | 受信任的目录公钥 | agino-store-helper |
| `/var/cache/agino-store/catalog/` | 已验证的目录（0755，文件 0644，客户端可读） | helper 运行时 |
| `/var/cache/agino-store/debs/` | 下载的 deb（0700） | helper 运行时 |
| `/var/lib/agino-store/installed.json` | 商店装过的应用 | helper 运行时 |
| `/usr/share/applications/agino-store.desktop` | 启动器 | agino-store |
| `/usr/share/dsg/configs/org.aginoos.store/org.aginoos.store.json` | 设置的 DConfig 元数据 | agino-store |

## 3. 配置 `/etc/agino-store/store.conf`

```ini
[Catalog]
# Base URL of the signed catalog; file:// works for testing.
Url=https://store.aginoos.com/v1/
# Directories with trusted public keys (*.pub).
KeyDirs=/usr/share/agino-store/keys

[Packages]
# Never removed through the store.
Protected=agino-desktop-dde;agino-desktop-base;dde-session-shell;dde-shell;dde-control-center;agino-store;agino-store-helper
```

客户端和 helper 都读这个文件（客户端只读 `Url` 用来拼图标和截图的地址）。

## 4. 目录格式（schema 1）

目录根（`Url`）下的文件：

```
manifest.json        清单：序号、有效期、全部其他文件的 sha256 和大小
manifest.json.sig    manifest.json 原始字节的 Ed25519 签名，base64，一行
catalog.json         索引（轻字段）
apps/<id>.json       详情
stats.json           可选：下载量
assets/<sha256>.<ext> 图标和截图，按内容寻址
```

`manifest.json`：

```json
{
  "schema": 1,
  "serial": 2026110203,
  "revision": "r20261102.3",
  "created": "2026-11-02T08:00:00Z",
  "expires": "2026-11-16T08:00:00Z",
  "files": [ { "path": "catalog.json", "sha256": "…", "size": 12345 } ]
}
```

- `serial` 是单调递增的整数。客户端和 helper 拒绝比已缓存的更小的 `serial`（防回滚），拒绝过期的清单（`expires` 早于当前时间；离线时已缓存的过期目录仍可只读显示）。
- `files` 列出除清单和签名以外的全部文件，包括 assets。路径只能是相对路径，不能含 `..`、以 `/` 开头或包含反斜杠。

`catalog.json`：

```json
{
  "schema": 1,
  "categories": [ { "id": "office", "name": { "zh_CN": "办公", "en_US": "Office" }, "order": 1 } ],
  "featured": [ { "id": "essentials", "title": { "zh_CN": "装机必备", "en_US": "Essentials" },
                  "subtitle": { … }, "apps": ["wps", "wechat"], "banner": false } ],
  "apps": [ {
    "id": "wps",
    "name": { "zh_CN": "WPS Office", "en_US": "WPS Office" },
    "summary": { "zh_CN": "…", "en_US": "…" },
    "icon": "assets/<sha256>.svg",
    "categories": ["office"],
    "source": "official",
    "license": "LicenseRef-proprietary",
    "proprietary": true,
    "updated": "2026-10-28",
    "package": {
      "kind": "deb",
      "name": "wps-office",
      "deb": { "version": "12.1.2.23578", "url": "https://…/wps-office_12.1.2.23578_amd64.deb",
               "vendor_url": "https://…", "sha256": "…", "size": 445000000, "installed_size": 1500000000 }
    },
    "detail": "apps/wps.json"
  } ]
}
```

- `source`：`system`（系统仓库）、`official`（官方提供）、`personal`（个人维护）。
- `package.kind`：`milkyway`（只有 `name`，从系统软件源装）或 `deb`（必须有 `deb`）。`personal` 的应用必须有 `maintainer`（在详情里）。
- `featured` 里 `banner: true` 的条目进首页横幅，第一个 `banner: false` 的是"装机必备"。

`apps/<id>.json`：

```json
{
  "id": "wps",
  "description": { "zh_CN": ["段落", "段落"], "en_US": ["…"] },
  "developer": { "zh_CN": "金山办公", "en_US": "Kingsoft Office" },
  "homepage": "https://www.wps.cn",
  "maintainer": null,
  "screenshots": { "zh_CN": [ { "image": "assets/….png", "thumb": "assets/….webp", "width": 1920, "height": 1080 } ],
                   "en_US": [ … ] },
  "releases": [ { "version": "12.1.2.23578", "date": "2026-10-28", "notes": { "zh_CN": ["…"], "en_US": ["…"] } } ],
  "install_profile": {
    "debconf": [ "code code/add-microsoft-repo boolean false" ],
    "default_files": [ { "path": "/etc/default/google-chrome", "content": "repo_add_once=\"false\"\nrepo_reenable_on_distupgrade=\"false\"\n" } ]
  }
}
```

- `install_profile.debconf` 每行是 `debconf-set-selections` 的一行：`<owner> <question> <type> <value>`，只允许 `boolean`、`string`、`select` 三种类型，字段里不能有换行。
- `default_files.path` 必须匹配 `^/etc/default/[a-z0-9][a-z0-9.+-]*$`；内容不超过 4 KiB。

`stats.json`：`{ "downloads": { "wps": 123456 } }`，用于"最热"排序。

### 4.1 语言

- 文本（名称、简介、说明、更新说明）：按 `QLocale::system().name()` 找完全相同的键（`zh_CN`），再找同语言的任一键（`zh_*`），再 `en_US`，再第一个。
- 截图：系统语言以 `zh` 开头（含 `zh_TW`、`zh_HK`）用 `zh_CN`，其他一律 `en_US`；该键为空时用另一套。

### 4.2 验签

- 公钥文件 `*.pub`：一行 base64，是 32 字节的 Ed25519 原始公钥。
- 签名：`manifest.json.sig` 一行 base64，是对 `manifest.json` 原始字节的 64 字节 Ed25519 签名。任一受信任公钥验证通过即可。
- 实现用 OpenSSL 3：`EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519, …)` + `EVP_DigestVerify`。
- 验签通过后，逐个校验清单里列出的文件（大小和 sha256）。helper 在写入缓存前校验全部非 assets 文件；assets 由客户端按需下载后校验。

## 5. D-Bus 接口

系统总线，名字 `org.aginoos.Store1`，对象 `/org/aginoos/Store1`，接口 `org.aginoos.Store1`。XML 在 `data/org.aginoos.Store1.xml`。

| 成员 | 签名 | 授权 | 说明 |
|---|---|---|---|
| `Refresh()` | `→ (x serial, s revision)` | 无 | 从 `Url` 取清单和签名，验签、校验、原子替换缓存；目录没变就直接返回 |
| `GetState()` | `→ s json` | 无 | `{"serial":…,"revision":…,"verified":true,"installed":{"wps":{"package":"wps-office","version":"…"}},"updates":[{"id":"wps","installed":"…","available":"…"}],"jobs":[…]}` |
| `Simulate(s appId, s action)` | `→ s json` | 无 | action 为 `install`/`update`/`remove`；`{"install":[{"name":…,"version":…}],"remove":[…],"download_bytes":…,"installed_delta":…}` |
| `Install(s appId, a{sv} options)` | `→ u jobId` | manage | options：`keep_package`(b，默认 false) |
| `Update(s appId, a{sv} options)` | `→ u jobId` | manage | 同上 |
| `Remove(s appId, a{sv} options)` | `→ u jobId` | manage | 只卸载目录里有的应用对应的包；`Protected` 里的拒绝 |
| `Cancel(u jobId)` | | manage | 只能取消还在等待或下载中的任务 |
| `JobProgress` 信号 | `(u jobId, s appId, s stage, u percent, t done, t total)` | | stage：`waiting`、`downloading`、`verifying`、`installing`、`removing` |
| `JobFinished` 信号 | `(u jobId, s appId, b ok, s errorCode, s message)` | | |
| `CatalogChanged` 信号 | `(x serial, s revision)` | | |
| `StateChanged` 信号 | `()` | | 安装、卸载、更新完成后 |

- 错误码：`not-authorized`、`catalog-unavailable`、`catalog-unverified`、`unknown-app`、`protected-package`、`busy`、`download-failed`、`hash-mismatch`、`install-profile-invalid`、`dependency-unresolvable`、`packagekit-error`、`cancelled`。方法调用失败时以 D-Bus 错误 `org.aginoos.Store1.Error.<驼峰码>` 返回；任务失败时由 `JobFinished` 带回错误码。
- 任务串行执行（PackageKit 本身也串行）；同一个应用已有任务时返回 `busy`。
- helper 没有任务且 5 分钟内没有调用时退出。

## 6. 授权

- polkit 动作 `org.aginoos.store.manage`：`allow_any=no`、`allow_inactive=no`、`allow_active=auth_admin_keep`。
- 规则 `50-agino-store.rules`：`action.id == "org.aginoos.store.manage" && subject.local && subject.active && subject.isInGroup("sudo")` 返回 `polkit.Result.YES`，其他不处理。
- helper 通过 D-Bus 调用 `org.freedesktop.PolicyKit1.Authority.CheckAuthorization`（主体 `system-bus-name`，调用者的唯一名，`AllowUserInteraction`），不链接 polkit-qt。
- helper 自己是 root，调用 PackageKit 不再经过 PackageKit 的 polkit 动作；不放行 PackageKit 自己的通用动作。

## 7. 安装、更新、卸载（helper）

1. 在已验证的缓存目录里按 ID 找应用；找不到返回 `unknown-app`。
2. `milkyway`：PackageKit `Resolve` 包名（过滤 `NotInstalled` 或 `Installed`），`InstallPackages`（`OnlyTrusted`）/`UpdatePackages`。
3. `deb`：
   1. 下载 `deb.url` 到 `/var/cache/agino-store/debs/<sha256>.deb`（先写 `.part`），大小超过 `deb.size` 立即中止；
   2. 校验大小和 sha256，不符返回 `hash-mismatch` 并删除文件；
   3. 校验并执行 `install_profile`：`debconf-set-selections`（stdin 传入）和写 `/etc/default/` 文件（0644）；
   4. 记下 `/etc/apt/sources.list.d/`、`/etc/apt/trusted.gpg.d/`、`/usr/share/keyrings/` 的文件列表；
   5. PackageKit `InstallFiles`（不设 `OnlyTrusted`）；
   6. 再看这三个目录：新出现且 `dpkg -S` 查不到属主的文件删除，写日志；
   7. 不保留安装包时删除下载的 deb。
4. 卸载：包名在 `Protected` 里返回 `protected-package`；PackageKit `RemovePackages(allow_deps=false, autoremove=true)`。
5. 成功后更新 `/var/lib/agino-store/installed.json`（原子写），发 `StateChanged`。
6. 可更新判断：已装版本（`dpkg-query -W`）与目录版本用 `dpkg --compare-versions` 比较；`milkyway` 的应用不进可更新列表（随系统更新）。

## 8. 客户端

- 入口 `main.cpp`：`QGuiApplication` + `QQmlApplicationEngine`，加载 `qrc:/qml/main.qml`；窗口用 DTK 的 `D.ApplicationWindow` 和 `D.TitleBar`，标题栏菜单：设置、`D.ThemeMenu`、分隔线、`D.AboutAction`、`D.QuitAction`。
- C++ 暴露给 QML（`QML_ELEMENT`/单例）：
  - `Store`（单例）：读 `/var/cache/agino-store/catalog/`（自己再验一遍签名和文件哈希），语言回退，D-Bus 调用和信号，任务列表，可更新列表，资源下载（`~/.cache/agino-store/assets/`，按清单校验 sha256）。
  - `AppListModel`：按分类、搜索词、来源和专有过滤（设置里的两个开关），排序 `hot`（stats.json 的下载量）/`new`（`updated` 日期）。
  - `Settings`（单例）：DConfig `org.aginoos.store`。
  - `DesktopIcons`：安装成功后把应用的 desktop 文件复制到 `xdg-user-dir DESKTOP`（设置开着且是新装时）；卸载时删除商店创建的那个；记录在 `~/.local/share/agino-store/desktop-icons.json`。
- 页面（与界面稿一致，解释性文字能不放就不放）：首页（横幅、装机必备、全部分类两两并排、每类 5 个、最热/最新切换）、分类、搜索、详情、已安装（可更新/已安装两个标签、全部更新、系统更新按钮）、下载任务、设置。
- 安装和卸载前先调用 `Simulate`，确认框只列事实：下载大小、安装后占用或释放空间、连带安装或移除的组件、来源。
- "系统更新"按钮打开控制中心的更新页：`dbus-send --session --dest=org.deepin.dde.ControlCenter1 /org/deepin/dde/ControlCenter1 org.deepin.dde.ControlCenter1.ShowPage string:update`（实现时核对接口名）。
- 第一版启动时调用一次 `Refresh`；每天自动检查放到后续版本（用户级 systemd 定时器，遵守"计量网络不自动下载"）。

DConfig 键（`org.aginoos.store`）：

| 键 | 类型 | 默认 |
|---|---|---|
| `autoCheckUpdates` | bool | true |
| `noDownloadOnMetered` | bool | true |
| `notifyUpdates` | bool | true |
| `autoInstallUpdates` | bool | false（第二期，界面上不可改） |
| `createDesktopIcon` | bool | true |
| `deletePackagesAfterInstall` | bool | true |
| `showPersonal` | bool | true |
| `showProprietary` | bool | true |

## 9. 测试

- 单元测试（QtTest）：目录解析和拒绝非法输入（路径、类型、缺字段）、语言回退、截图语言、Ed25519 验签（好签名、改一字节、错公钥）、清单序号和过期、文件哈希校验、`install_profile` 校验。
- `tests/data/keys/test-ed25519.{pem,pub}` 只用于测试，不安装，任何包都不信任它。
- 集成测试在 agino 虚拟机里做（主 agent）：配置 `Url=file:///…/tests/data/catalog/`，并把测试公钥放进 `KeyDirs`，安装、卸载一个 milkyway 包和一个本地 deb。
