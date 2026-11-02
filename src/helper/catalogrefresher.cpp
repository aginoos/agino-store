// SPDX-License-Identifier: GPL-3.0-or-later
#include "catalogrefresher.h"

#include "fsutil.h"
#include "paths.h"

#include <QDateTime>
#include <QDebug>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTimer>
#include <QUrl>

namespace AginoStore {

namespace {
constexpr qint64 kMaxManifestBytes = 4 * 1024 * 1024;
constexpr qint64 kMaxSignatureBytes = 4096;
constexpr qint64 kMaxFileBytes = 64 * 1024 * 1024;
constexpr int kMaxFiles = 10000;
constexpr int kTransferTimeoutMs = 30000;
const char kStagingPrefix[] = "catalog.new-";
} // namespace

CatalogRefresher::CatalogRefresher(const HelperConfig &config, CatalogCache *cache,
                                   QNetworkAccessManager *nam, QObject *parent)
    : QObject(parent)
    , m_config(config)
    , m_cache(cache)
    , m_nam(nam)
{
}

void CatalogRefresher::fail(const QString &code, const QString &message)
{
    qWarning().noquote() << "catalog refresh failed:" << code << message;
    cleanupStaging();
    m_running = false;
    Q_EMIT finished(false, code, message, false);
}

void CatalogRefresher::cleanupStaging()
{
    m_staging.reset();
    m_pending.clear();
    m_manifestBytes.clear();
    m_signature.clear();
}

void CatalogRefresher::fetch(const QString &relativePath, qint64 maxBytes, FetchDone done)
{
    const QUrl url(m_config.catalogUrl + relativePath);
    if (!url.isValid()) {
        QTimer::singleShot(0, this, [done]() { done(false, QByteArray(), QStringLiteral("invalid URL")); });
        return;
    }
    if (url.isLocalFile()) {
        QByteArray data;
        QString error;
        const QString path = url.toLocalFile();
        const bool ok = QFileInfo(path).isFile() && readFileLimited(path, maxBytes, &data, &error);
        if (!ok && error.isEmpty())
            error = QStringLiteral("%1 is not a regular file").arg(path);
        QTimer::singleShot(0, this, [done, ok, data, error]() { done(ok, data, error); });
        return;
    }
    if (url.scheme() != QLatin1String("https") && url.scheme() != QLatin1String("http")) {
        QTimer::singleShot(0, this, [done]() { done(false, QByteArray(), QStringLiteral("unsupported URL scheme")); });
        return;
    }

    QNetworkRequest request(url);
    request.setTransferTimeout(kTransferTimeoutMs);
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::NoLessSafeRedirectPolicy);
    QNetworkReply *reply = m_nam->get(request);
    m_reply = reply;
    auto buffer = std::make_shared<QByteArray>();
    auto tooBig = std::make_shared<bool>(false);
    connect(reply, &QNetworkReply::readyRead, this, [reply, buffer, tooBig, maxBytes]() {
        buffer->append(reply->readAll());
        if (buffer->size() > maxBytes) {
            *tooBig = true;
            reply->abort();
        }
    });
    connect(reply, &QNetworkReply::finished, this, [reply, buffer, tooBig, done]() {
        reply->deleteLater();
        if (*tooBig) {
            done(false, QByteArray(), QStringLiteral("response larger than allowed"));
        } else if (reply->error() != QNetworkReply::NoError) {
            done(false, QByteArray(), reply->errorString());
        } else {
            buffer->append(reply->readAll());
            if (buffer->size() > maxBytes)
                done(false, QByteArray(), QStringLiteral("response larger than allowed"));
            else
                done(true, *buffer, QString());
        }
    });
}

void CatalogRefresher::start()
{
    if (m_running)
        return;
    m_running = true;
    if (m_config.catalogUrl.isEmpty()) {
        QTimer::singleShot(0, this, [this]() { fail(QStringLiteral("catalog-unavailable"), QStringLiteral("no catalog URL configured")); });
        return;
    }
    fetch(QStringLiteral("manifest.json"), kMaxManifestBytes,
          [this](bool ok, const QByteArray &data, const QString &error) {
              if (!ok) {
                  fail(QStringLiteral("catalog-unavailable"), QStringLiteral("manifest.json: ") + error);
                  return;
              }
              m_manifestBytes = data;
              fetch(QStringLiteral("manifest.json.sig"), kMaxSignatureBytes,
                    [this](bool sigOk, const QByteArray &sig, const QString &sigError) {
                        if (!sigOk) {
                            fail(QStringLiteral("catalog-unavailable"), QStringLiteral("manifest.json.sig: ") + sigError);
                            return;
                        }
                        m_signature = sig;
                        onManifestAndSignature();
                    });
          });
}

void CatalogRefresher::onManifestAndSignature()
{
    const QList<QByteArray> keys = loadTrustedKeys(m_config.keyDirs);
    if (keys.isEmpty()) {
        fail(QStringLiteral("catalog-unverified"), QStringLiteral("no trusted public keys"));
        return;
    }
    const CatalogSnapshot &cached = m_cache->snapshot();
    const VerifyResult verified = verifyManifest(m_manifestBytes, m_signature, keys, m_cache->serial(),
                                                 QDateTime::currentDateTimeUtc());
    if (!verified.ok) {
        fail(QStringLiteral("catalog-unverified"),
             verified.expired ? QStringLiteral("the catalog manifest has expired") : verified.error);
        return;
    }
    m_manifest = verified.manifest;

    // Nothing new: same bytes as the verified cache.
    if (cached.valid && cached.manifestBytes == m_manifestBytes) {
        cleanupStaging();
        m_running = false;
        Q_EMIT finished(true, QString(), QString(), false);
        return;
    }

    if (m_manifest.files.size() > kMaxFiles || !m_manifest.entry(QStringLiteral("catalog.json"))) {
        fail(QStringLiteral("catalog-unverified"), QStringLiteral("manifest lists no catalog.json or too many files"));
        return;
    }
    m_pending.clear();
    QStringList seen;
    for (const ManifestEntry &entry : m_manifest.files) {
        if (!isSafeRelativePath(entry.path) || entry.size < 0 || entry.size > kMaxFileBytes
            || seen.contains(entry.path)
            || entry.path == QLatin1String("manifest.json") || entry.path == QLatin1String("manifest.json.sig")) {
            fail(QStringLiteral("catalog-unverified"), QStringLiteral("unacceptable manifest entry: ") + entry.path);
            return;
        }
        seen.append(entry.path);
        if (!entry.path.startsWith(QLatin1String("assets/")))
            m_pending.append(entry);
    }

    QString error;
    if (!ensureDirectory(cacheRoot(), perm755(), &error)) {
        fail(QStringLiteral("catalog-unavailable"), error);
        return;
    }
    // Remove staging directories left behind by an interrupted refresh.
    const QStringList stale = QDir(cacheRoot()).entryList({QString::fromLatin1(kStagingPrefix) + QLatin1Char('*')},
                                                          QDir::Dirs | QDir::NoDotAndDotDot);
    for (const QString &name : stale)
        QDir(cacheRoot() + QLatin1Char('/') + name).removeRecursively();

    m_staging = std::make_unique<QTemporaryDir>(cacheRoot() + QLatin1Char('/') + QString::fromLatin1(kStagingPrefix)
                                                + QStringLiteral("XXXXXX"));
    if (!m_staging->isValid()) {
        fail(QStringLiteral("catalog-unavailable"), QStringLiteral("cannot create a staging directory"));
        return;
    }
    QFile::setPermissions(m_staging->path(), perm755());
    m_next = 0;
    fetchNextFile();
}

void CatalogRefresher::fetchNextFile()
{
    if (m_next >= m_pending.size()) {
        commit();
        return;
    }
    const ManifestEntry entry = m_pending.at(m_next);
    fetch(entry.path, entry.size, [this, entry](bool ok, const QByteArray &data, const QString &error) {
        if (!ok) {
            fail(QStringLiteral("catalog-unavailable"), entry.path + QStringLiteral(": ") + error);
            return;
        }
        if (!matchesEntry(data, entry)) {
            fail(QStringLiteral("catalog-unverified"), entry.path + QStringLiteral(": size or sha256 mismatch"));
            return;
        }
        // The path was validated by isSafeRelativePath, so it cannot leave the staging directory.
        const QString dest = m_staging->path() + QLatin1Char('/') + entry.path;
        QString writeError;
        if (!ensureDirectory(QFileInfo(dest).absolutePath(), perm755(), &writeError)
            || !writeFileAtomic(dest, data, perm644(), &writeError)) {
            fail(QStringLiteral("catalog-unavailable"), writeError);
            return;
        }
        ++m_next;
        fetchNextFile();
    });
}

void CatalogRefresher::commit()
{
    const QString base = m_staging->path();
    QString error;
    if (!writeFileAtomic(base + QStringLiteral("/manifest.json"), m_manifestBytes, perm644(), &error)
        || !writeFileAtomic(base + QStringLiteral("/manifest.json.sig"), m_signature, perm644(), &error)
        || !replaceDirectory(base, catalogDir(), &error)) {
        fail(QStringLiteral("catalog-unavailable"), error);
        return;
    }
    // After the swap the staging path holds the old catalog (or nothing); QTemporaryDir removes it.
    m_staging.reset();
    m_cache->reload();
    cleanupStaging();
    m_running = false;
    Q_EMIT finished(true, QString(), QString(), true);
}

} // namespace AginoStore
