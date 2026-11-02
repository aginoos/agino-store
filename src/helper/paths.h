// SPDX-License-Identifier: GPL-3.0-or-later
// Fixed filesystem locations used by the helper (docs/design.md section 2).
#pragma once

#include <QString>

namespace AginoStore {

inline QString configFilePath() { return QStringLiteral("/etc/agino-store/store.conf"); }
inline QString cacheRoot() { return QStringLiteral("/var/cache/agino-store"); }
inline QString catalogDir() { return cacheRoot() + QStringLiteral("/catalog"); }
inline QString debsDir() { return cacheRoot() + QStringLiteral("/debs"); }
inline QString stateDir() { return QStringLiteral("/var/lib/agino-store"); }
inline QString installedFilePath() { return stateDir() + QStringLiteral("/installed.json"); }

// Absolute program paths: QProcess would resolve bare names with the PATH of this process.
inline QString dpkgPath() { return QStringLiteral("/usr/bin/dpkg"); }
inline QString dpkgQueryPath() { return QStringLiteral("/usr/bin/dpkg-query"); }
inline QString debconfSetSelectionsPath() { return QStringLiteral("/usr/bin/debconf-set-selections"); }

} // namespace AginoStore
