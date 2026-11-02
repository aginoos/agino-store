// SPDX-License-Identifier: GPL-3.0-or-later
#include "storeconfig.h"

#include <QFile>
#include <QTextStream>

namespace {

QString configPath()
{
    // AGINO_STORE_CONF lets the VM tests point the client at a test catalog.
    const QByteArray env = qgetenv("AGINO_STORE_CONF");
    return env.isEmpty() ? QStringLiteral("/etc/agino-store/store.conf") : QString::fromLocal8Bit(env);
}

QStringList splitList(const QString &value)
{
    QStringList out;
    const QStringList parts = value.split(QLatin1Char(';'), Qt::SkipEmptyParts);
    for (const QString &part : parts) {
        const QString trimmed = part.trimmed();
        if (!trimmed.isEmpty())
            out.append(trimmed);
    }
    return out;
}

} // namespace

StoreConfig StoreConfig::load()
{
    StoreConfig config;
    config.catalogUrl = QStringLiteral("https://store.aginoos.com/v1/");
    config.keyDirs = {QStringLiteral("/usr/share/agino-store/keys")};

    QFile file(configPath());
    if (file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        QTextStream in(&file);
        QString section;
        while (!in.atEnd()) {
            const QString line = in.readLine().trimmed();
            if (line.isEmpty() || line.startsWith(QLatin1Char('#')) || line.startsWith(QLatin1Char(';')))
                continue;
            if (line.startsWith(QLatin1Char('[')) && line.endsWith(QLatin1Char(']'))) {
                section = line.mid(1, line.size() - 2);
                continue;
            }
            const int eq = line.indexOf(QLatin1Char('='));
            if (eq <= 0)
                continue;
            const QString key = line.left(eq).trimmed();
            const QString value = line.mid(eq + 1).trimmed();
            if (section == QLatin1String("Catalog") && key == QLatin1String("Url") && !value.isEmpty())
                config.catalogUrl = value;
            else if (section == QLatin1String("Catalog") && key == QLatin1String("AllowInsecureUrls"))
                config.allowInsecureUrls = value.compare(QLatin1String("true"), Qt::CaseInsensitive) == 0
                    || value == QLatin1String("1");
            else if (section == QLatin1String("Catalog") && key == QLatin1String("KeyDirs"))
                config.keyDirs = splitList(value);
            else if (section == QLatin1String("Packages") && key == QLatin1String("Protected"))
                config.protectedPackages = splitList(value);
        }
    }
    if (!config.catalogUrl.endsWith(QLatin1Char('/')))
        config.catalogUrl += QLatin1Char('/');
    return config;
}

QString StoreConfig::catalogDir()
{
    const QByteArray env = qgetenv("AGINO_STORE_CATALOG_DIR");
    return env.isEmpty() ? QStringLiteral("/var/cache/agino-store/catalog") : QString::fromLocal8Bit(env);
}
