# agino-store

agino 的应用商店：一个 deb 包 `agino-store`，内含客户端（Qt6/QML + DTK，用户权限运行）和 root 辅助程序 `agino-store-helper`（D-Bus 系统服务，按需激活）。客户端不添加任何第三方软件源；所有安装和卸载经 helper 完成，helper 只装签名目录里的应用。

设计和组件间协议见 [docs/design.md](docs/design.md)；决策见 agino infra `docs/decisions.md` D-192。

## 构建

构建依赖：cmake、qt6-base-dev、qt6-declarative-dev、qt6-tools-dev、qt6-l10n-tools、libdtk6core-dev、libdtk6gui-dev、libdtk6declarative-dev、libpackagekitqt6-dev、libssl-dev。

```
cmake -S . -B build -DCMAKE_INSTALL_PREFIX=/usr
cmake --build build
ctest --test-dir build
sudo cmake --install build
```

## 许可

GPL-3.0-or-later，见 COPYING。
