// SPDX-License-Identifier: GPL-3.0-or-later
// Input validation and dpkg queries (docs/design.md section 7). Every name that reaches an
// external command is validated first.
#pragma once

#include <QHash>
#include <QString>
#include <QStringList>

namespace AginoStore {

bool isValidAppId(const QString &id);
bool isValidPackageName(const QString &name);
bool isValidVersion(const QString &version);

// package name -> installed version, only for packages whose dpkg status is "installed".
QHash<QString, QString> installedVersions(const QStringList &packageNames);

// True when `a` is strictly newer than `b` (dpkg --compare-versions a gt b).
bool dpkgVersionGreater(const QString &a, const QString &b);

// True when some installed package owns the path (dpkg-query -S).
bool dpkgOwnsPath(const QString &path);

} // namespace AginoStore
