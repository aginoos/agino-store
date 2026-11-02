// SPDX-License-Identifier: GPL-3.0-or-later
#include "catalogcache.h"

#include "fsutil.h"
#include "paths.h"

#include <QDateTime>

namespace AginoStore {

namespace {
constexpr qint64 kMaxMetaBytes = 64 * 1024 * 1024;
}

CatalogCache::CatalogCache(const HelperConfig &config)
    : m_config(config)
{
    reload();
}

void CatalogCache::reload()
{
    CatalogSnapshot snap;
    const QString dir = catalogDir();
    QByteArray sig;
    QString error;
    if (!readFileLimited(dir + QStringLiteral("/manifest.json"), kMaxMetaBytes, &snap.manifestBytes, &error)
        || !readFileLimited(dir + QStringLiteral("/manifest.json.sig"), 4096, &sig, &error)) {
        snap.error = error;
        m_snapshot = snap;
        return;
    }
    const QList<QByteArray> keys = loadTrustedKeys(m_config.keyDirs);
    // minSerial 0: the rollback floor is the serial of this very cache.
    const VerifyResult verified =
        verifyManifest(snap.manifestBytes, sig, keys, 0, QDateTime::currentDateTimeUtc());
    if (!verified.ok && !verified.expired) {
        snap.error = verified.error;
        m_snapshot = snap;
        return;
    }
    snap.manifest = verified.manifest;
    snap.expired = verified.expired;

    const std::optional<ManifestEntry> entry = snap.manifest.entry(QStringLiteral("catalog.json"));
    QByteArray catalogBytes;
    if (!entry || !readFileLimited(dir + QStringLiteral("/catalog.json"), kMaxMetaBytes, &catalogBytes, &error)
        || !matchesEntry(catalogBytes, *entry)) {
        snap.error = error.isEmpty() ? QStringLiteral("catalog.json missing or does not match the manifest") : error;
        m_snapshot = snap;
        return;
    }
    const std::optional<Catalog> parsed = parseCatalog(catalogBytes, &error);
    if (!parsed) {
        snap.error = error;
        m_snapshot = snap;
        return;
    }
    snap.catalog = *parsed;
    snap.valid = true;
    m_snapshot = snap;
}

std::optional<AppDetail> CatalogCache::loadDetail(const AppSummary &app, QString *error) const
{
    if (!m_snapshot.valid) {
        if (error)
            *error = m_snapshot.error;
        return std::nullopt;
    }
    if (!isSafeRelativePath(app.detail)) {
        if (error)
            *error = QStringLiteral("unsafe detail path");
        return std::nullopt;
    }
    const std::optional<ManifestEntry> entry = m_snapshot.manifest.entry(app.detail);
    QByteArray bytes;
    QString readError;
    if (!entry || !readFileLimited(catalogDir() + QLatin1Char('/') + app.detail, kMaxMetaBytes, &bytes, &readError)
        || !matchesEntry(bytes, *entry)) {
        if (error)
            *error = readError.isEmpty() ? QStringLiteral("detail file missing or does not match the manifest") : readError;
        return std::nullopt;
    }
    QString parseError;
    std::optional<AppDetail> detail = parseAppDetail(bytes, &parseError);
    if (!detail || detail->id != app.id) {
        if (error)
            *error = parseError.isEmpty() ? QStringLiteral("detail id mismatch") : parseError;
        return std::nullopt;
    }
    return detail;
}

} // namespace AginoStore
