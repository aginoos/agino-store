// SPDX-License-Identifier: GPL-3.0-or-later
// Refresh of the verified catalog cache (docs/design.md sections 4.2 and 5, Refresh).
#pragma once

#include "catalogcache.h"
#include "config.h"
#include "manifest.h"

#include <QObject>
#include <QPointer>
#include <QTemporaryDir>

#include <functional>
#include <memory>

class QNetworkAccessManager;
class QNetworkReply;

namespace AginoStore {

class CatalogRefresher : public QObject
{
    Q_OBJECT
public:
    CatalogRefresher(const HelperConfig &config, CatalogCache *cache, QNetworkAccessManager *nam,
                     QObject *parent = nullptr);

    bool isRunning() const { return m_running; }
    // No-op while a refresh is already running (the finished signal covers both callers).
    void start();

Q_SIGNALS:
    // errorCode is one of the design.md error codes when ok is false. changed is true when the
    // cache was replaced.
    void finished(bool ok, const QString &errorCode, const QString &message, bool changed);

private:
    using FetchDone = std::function<void(bool ok, const QByteArray &data, const QString &error)>;

    void fetch(const QString &relativePath, qint64 maxBytes, FetchDone done);
    void fail(const QString &code, const QString &message);
    void onManifestAndSignature();
    void fetchNextFile();
    void commit();
    void cleanupStaging();

    HelperConfig m_config;
    CatalogCache *m_cache;
    QNetworkAccessManager *m_nam;
    bool m_running = false;

    QByteArray m_manifestBytes;
    QByteArray m_signature;
    Manifest m_manifest;
    QList<ManifestEntry> m_pending;
    int m_next = 0;
    std::unique_ptr<QTemporaryDir> m_staging;
    QPointer<QNetworkReply> m_reply;
};

} // namespace AginoStore
