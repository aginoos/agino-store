// SPDX-License-Identifier: GPL-3.0-or-later
// /etc/agino-store/store.conf (docs/design.md section 3).
#pragma once

#include <QString>
#include <QStringList>

namespace AginoStore {

struct HelperConfig {
    QString catalogUrl;             // always ends with '/' when non-empty
    QStringList keyDirs;
    QStringList protectedPackages;
    bool allowInsecureUrls = false; // test-only: also accept http:// and file:// URLs

    // https always; http and file only with allowInsecureUrls.
    bool isAllowedScheme(const QString &scheme) const
    {
        return scheme == QLatin1String("https")
            || (allowInsecureUrls && (scheme == QLatin1String("http") || scheme == QLatin1String("file")));
    }

    bool isProtected(const QString &package) const { return protectedPackages.contains(package); }
    static HelperConfig load(const QString &path);
};

} // namespace AginoStore
