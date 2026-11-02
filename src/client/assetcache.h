// SPDX-License-Identifier: GPL-3.0-or-later
// Icons and screenshots: fetched on demand from the catalog URL (http(s) or file://), checked
// against the manifest sha256 and kept in ~/.cache/agino-store/assets/.
#pragma once

#include "manifest.h"

#include <QHash>
#include <QObject>
#include <QQueue>
#include <QSet>
#include <QString>
#include <QUrl>

class QNetworkAccessManager;
class QNetworkReply;

class AssetCache : public QObject
{
    Q_OBJECT
public:
    AssetCache(const QUrl &baseUrl, bool allowInsecure, QObject *parent = nullptr);

    void setManifest(const AginoStore::Manifest &manifest);
    // Local file for a catalog-relative path, or an empty string while it is still missing
    // (the download starts and ready() follows).
    QString localPath(const QString &relPath);
    qint64 sizeOnDisk() const;
    void clear();

signals:
    void ready(const QString &relPath);

private:
    void startNext();
    void finish(QNetworkReply *reply, const QString &relPath, const AginoStore::ManifestEntry &entry);
    QString fileFor(const QString &relPath) const;

    QUrl m_base;
    bool m_allowInsecure;
    QString m_dir;
    QNetworkAccessManager *m_net = nullptr;
    AginoStore::Manifest m_manifest;
    QSet<QString> m_verified;   // verified on disk this session
    QSet<QString> m_inFlight;
    QSet<QString> m_failed;
    QQueue<QString> m_queue;
    int m_running = 0;
};
