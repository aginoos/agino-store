// SPDX-License-Identifier: GPL-3.0-or-later
#include "config.h"

#include <QSettings>
#include <QVariant>

namespace AginoStore {

namespace {

// Values are ';' separated. QSettings may already have split on ',' (INI list syntax), so
// accept both a string and a string list and split every item on ';'.
QStringList splitList(const QVariant &value)
{
    QStringList out;
    const QStringList items = value.toStringList();
    for (const QString &item : items) {
        const QStringList parts = item.split(QLatin1Char(';'), Qt::SkipEmptyParts);
        for (const QString &part : parts) {
            const QString trimmed = part.trimmed();
            if (!trimmed.isEmpty())
                out.append(trimmed);
        }
    }
    return out;
}

} // namespace

HelperConfig HelperConfig::load(const QString &path)
{
    HelperConfig config;
    config.keyDirs = {QStringLiteral("/usr/share/agino-store/keys")};

    QSettings settings(path, QSettings::IniFormat);
    // TODO(verify): QSettings keeps ';' in the middle of a value as part of the value (only a
    // line-leading ';' or '#' starts a comment); confirm with the shipped store.conf.
    settings.beginGroup(QStringLiteral("Catalog"));
    QString url = settings.value(QStringLiteral("Url")).toString().trimmed();
    if (!url.isEmpty() && !url.endsWith(QLatin1Char('/')))
        url += QLatin1Char('/');
    config.catalogUrl = url;
    const QStringList dirs = splitList(settings.value(QStringLiteral("KeyDirs")));
    if (!dirs.isEmpty())
        config.keyDirs = dirs;
    settings.endGroup();

    settings.beginGroup(QStringLiteral("Packages"));
    config.protectedPackages = splitList(settings.value(QStringLiteral("Protected")));
    settings.endGroup();
    return config;
}

} // namespace AginoStore
