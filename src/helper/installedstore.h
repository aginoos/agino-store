// SPDX-License-Identifier: GPL-3.0-or-later
// /var/lib/agino-store/installed.json: the apps the store installed (docs/design.md section 7.5).
#pragma once

#include <QHash>
#include <QString>

namespace AginoStore {

class InstalledStore
{
public:
    struct Entry {
        QString package;
        QString kind;        // "milkyway" or "deb"
    };

    InstalledStore();

    const QHash<QString, Entry> &entries() const { return m_entries; }
    bool record(const QString &appId, const QString &package, const QString &kind);
    bool erase(const QString &appId);

private:
    void load();
    bool save();

    QHash<QString, Entry> m_entries;
};

} // namespace AginoStore
