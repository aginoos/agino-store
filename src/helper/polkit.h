// SPDX-License-Identifier: GPL-3.0-or-later
// Asynchronous org.freedesktop.PolicyKit1.Authority.CheckAuthorization for a D-Bus caller
// (docs/design.md section 6). No polkit-qt.
#pragma once

#include <QObject>
#include <QString>

#include <functional>

namespace AginoStore {

inline constexpr char kActionManage[] = "org.aginoos.store.manage";

enum class AuthResult { Authorized, Denied, Error };

// `done` runs on the event loop; `parent` owns the pending call.
void checkAuthorization(QObject *parent, const QString &busName, const QString &actionId,
                        bool allowInteraction, std::function<void(AuthResult)> done);

} // namespace AginoStore
