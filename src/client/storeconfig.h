// SPDX-License-Identifier: GPL-3.0-or-later
// Reads /etc/agino-store/store.conf (docs/design.md section 3). The client only needs the
// catalog URL (icon and screenshot addresses), the key directories (it verifies the cache
// itself) and the protected package list.
#pragma once

#include <QString>
#include <QStringList>

struct StoreConfig {
    QString catalogUrl;          // always ends with '/'
    QStringList keyDirs;
    QStringList protectedPackages;
    bool allowInsecureUrls = false;   // test-only: http:// and file:// asset URLs

    // Missing file or keys fall back to the documented defaults.
    static StoreConfig load();
    // Verified catalog cache written by the helper.
    static QString catalogDir();
};
