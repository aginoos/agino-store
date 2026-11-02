// SPDX-License-Identifier: GPL-3.0-or-later
#include "assetcache.h"

#include "catalog.h"

#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QFileInfo>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QSaveFile>
#include <QStandardPaths>

using namespace AginoStore;

namespace {
constexpr int kMaxParallel = 4;
}

AssetCache::AssetCache(const QUrl &baseUrl, QObject *parent)
    : QObject(parent)
    , m_base(baseUrl)
    , m_dir(QStandardPaths::writableLocation(QStandardPaths::GenericCacheLocation)
            + QStringLiteral("/agino-store/assets"))
    , m_net(new QNetworkAccessManager(this))
{
    QDir().mkpath(m_dir);
}

void AssetCache::setManifest(const Manifest &manifest)
{
    m_manifest = manifest;
    m_failed.clear();   // a new catalog may fix what failed before
}

QString AssetCache::fileFor(const QString &relPath) const
{
    // Assets are content addressed, so the file name alone is unique.
    return m_dir + QLatin1Char('/') + QFileInfo(relPath).fileName();
}

QString AssetCache::localPath(const QString &relPath)
{
    if (relPath.isEmpty() || !isSafeRelativePath(relPath))
        return {};
    const std::optional<ManifestEntry> entry = m_manifest.entry(relPath);
    if (!entry)
        return {};

    const QString file = fileFor(relPath);
    if (m_verified.contains(relPath))
        return file;

    QFile cached(file);
    if (cached.exists()) {
        if (cached.open(QIODevice::ReadOnly) && matchesEntry(cached.readAll(), *entry)) {
            m_verified.insert(relPath);
            return file;
        }
        cached.close();
        QFile::remove(file);
    }

    if (!m_inFlight.contains(relPath) && !m_failed.contains(relPath)) {
        m_inFlight.insert(relPath);
        m_queue.enqueue(relPath);
        startNext();
    }
    return {};
}

void AssetCache::startNext()
{
    while (m_running < kMaxParallel && !m_queue.isEmpty()) {
        const QString relPath = m_queue.dequeue();
        const std::optional<ManifestEntry> entry = m_manifest.entry(relPath);
        if (!entry) {
            m_inFlight.remove(relPath);
            continue;
        }
        QNetworkRequest request(m_base.resolved(QUrl(relPath)));
        request.setAttribute(QNetworkRequest::RedirectPolicyAttribute,
                             QNetworkRequest::NoLessSafeRedirectPolicy);
        QNetworkReply *reply = m_net->get(request);
        ++m_running;
        const ManifestEntry wanted = *entry;
        // A server that sends more than the manifest promised is cut off.
        connect(reply, &QNetworkReply::downloadProgress, reply, [reply, wanted](qint64 received, qint64) {
            if (received > wanted.size)
                reply->abort();
        });
        connect(reply, &QNetworkReply::finished, this, [this, reply, relPath, wanted]() {
            finish(reply, relPath, wanted);
        });
    }
}

void AssetCache::finish(QNetworkReply *reply, const QString &relPath, const ManifestEntry &entry)
{
    reply->deleteLater();
    --m_running;
    m_inFlight.remove(relPath);

    bool ok = false;
    if (reply->error() == QNetworkReply::NoError) {
        const QByteArray data = reply->readAll();
        if (matchesEntry(data, entry)) {
            QSaveFile out(fileFor(relPath));
            if (out.open(QIODevice::WriteOnly) && out.write(data) == data.size() && out.commit())
                ok = true;
        }
    }
    if (ok) {
        m_verified.insert(relPath);
        emit ready(relPath);
    } else {
        m_failed.insert(relPath);
    }
    startNext();
}

qint64 AssetCache::sizeOnDisk() const
{
    qint64 total = 0;
    QDirIterator it(m_dir, QDir::Files);
    while (it.hasNext()) {
        it.next();
        total += it.fileInfo().size();
    }
    return total;
}

void AssetCache::clear()
{
    QDirIterator it(m_dir, QDir::Files);
    while (it.hasNext())
        QFile::remove(it.next());
    m_verified.clear();
    m_failed.clear();
}
