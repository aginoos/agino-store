// SPDX-License-Identifier: GPL-3.0-or-later
#include "installedstore.h"

#include "dpkgutil.h"
#include "fsutil.h"
#include "paths.h"

#include <QJsonDocument>
#include <QJsonObject>
#include <QDebug>

namespace AginoStore {

InstalledStore::InstalledStore()
{
    load();
}

void InstalledStore::load()
{
    m_entries.clear();
    QByteArray data;
    QString error;
    if (!readFileLimited(installedFilePath(), 4 * 1024 * 1024, &data, &error))
        return;   // missing on first run
    const QJsonDocument doc = QJsonDocument::fromJson(data);
    const QJsonObject apps = doc.object().value(QStringLiteral("apps")).toObject();
    for (auto it = apps.begin(); it != apps.end(); ++it) {
        const QJsonObject o = it.value().toObject();
        const QString package = o.value(QStringLiteral("package")).toString();
        const QString kind = o.value(QStringLiteral("kind")).toString();
        if (!isValidAppId(it.key()) || !isValidPackageName(package))
            continue;
        m_entries.insert(it.key(), Entry{package, kind == QLatin1String("deb") ? kind : QStringLiteral("milkyway")});
    }
}

bool InstalledStore::save()
{
    QJsonObject apps;
    for (auto it = m_entries.cbegin(); it != m_entries.cend(); ++it) {
        apps.insert(it.key(), QJsonObject{{QStringLiteral("package"), it.value().package},
                                          {QStringLiteral("kind"), it.value().kind}});
    }
    const QJsonObject root{{QStringLiteral("schema"), 1}, {QStringLiteral("apps"), apps}};
    QString error;
    if (!ensureDirectory(stateDir(), perm755(), &error)
        || !writeFileAtomic(installedFilePath(), QJsonDocument(root).toJson(QJsonDocument::Indented),
                            perm644(), &error)) {
        qWarning().noquote() << "cannot save installed.json:" << error;
        return false;
    }
    return true;
}

bool InstalledStore::record(const QString &appId, const QString &package, const QString &kind)
{
    m_entries.insert(appId, Entry{package, kind});
    return save();
}

bool InstalledStore::erase(const QString &appId)
{
    if (m_entries.remove(appId) == 0)
        return true;
    return save();
}

} // namespace AginoStore
