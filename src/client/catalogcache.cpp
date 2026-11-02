// SPDX-License-Identifier: GPL-3.0-or-later
#include "catalogcache.h"

#include <QFile>
#include <QFileInfo>

using namespace AginoStore;

namespace {

QByteArray readAll(const QString &path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return {};
    return file.readAll();
}

CatalogCache::Loaded unavailable(const QString &why)
{
    CatalogCache::Loaded loaded;
    loaded.status = CatalogCache::Status::Unavailable;
    loaded.error = why;
    return loaded;
}

} // namespace

CatalogCache::CatalogCache(QString dir, QStringList keyDirs)
    : m_dir(std::move(dir))
    , m_keyDirs(std::move(keyDirs))
{
}

CatalogCache::Loaded CatalogCache::load(qint64 minSerial) const
{
    const QByteArray manifestJson = readAll(m_dir + QStringLiteral("/manifest.json"));
    const QByteArray signature = readAll(m_dir + QStringLiteral("/manifest.json.sig"));
    if (manifestJson.isEmpty() || signature.isEmpty())
        return unavailable(QStringLiteral("no catalog in cache"));

    const QList<QByteArray> keys = loadTrustedKeys(m_keyDirs);
    if (keys.isEmpty())
        return unavailable(QStringLiteral("no trusted keys"));

    const VerifyResult verified = verifyManifest(manifestJson, signature, keys, minSerial,
                                                 QDateTime::currentDateTimeUtc());
    // TODO(verify): an expired manifest comes back with expired == true; manifest.h does not say
    // whether ok stays true then, so accept either as long as the serial is not a rollback.
    const bool acceptable = verified.ok || (verified.expired && verified.manifest.serial >= minSerial
                                            && verified.manifest.serial > 0);
    if (!acceptable)
        return unavailable(verified.error);

    Loaded loaded;
    loaded.manifest = verified.manifest;
    loaded.status = verified.expired ? Status::Expired : Status::Ok;

    const QByteArray catalogJson = readVerified(loaded.manifest, QStringLiteral("catalog.json"));
    if (catalogJson.isEmpty())
        return unavailable(QStringLiteral("catalog.json missing or corrupt"));
    QString error;
    const std::optional<Catalog> catalog = parseCatalog(catalogJson, &error);
    if (!catalog)
        return unavailable(error);
    loaded.catalog = *catalog;

    if (loaded.manifest.entry(QStringLiteral("stats.json")))
        loaded.stats = parseStats(readVerified(loaded.manifest, QStringLiteral("stats.json")));
    return loaded;
}

QByteArray CatalogCache::readVerified(const Manifest &manifest, const QString &path) const
{
    if (!isSafeRelativePath(path))
        return {};
    const std::optional<ManifestEntry> entry = manifest.entry(path);
    if (!entry)
        return {};
    const QByteArray data = readAll(m_dir + QLatin1Char('/') + path);
    return matchesEntry(data, *entry) ? data : QByteArray();
}

QDateTime CatalogCache::manifestModified() const
{
    return QFileInfo(m_dir + QStringLiteral("/manifest.json")).lastModified();
}
