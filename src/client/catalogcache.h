// SPDX-License-Identifier: GPL-3.0-or-later
// Reads the helper's catalog cache and verifies it again (signature, serial, file hashes).
#pragma once

#include "catalog.h"
#include "manifest.h"

#include <QDateTime>
#include <QHash>
#include <QString>
#include <QStringList>

class CatalogCache
{
public:
    enum class Status { Ok, Expired, Unavailable };

    struct Loaded {
        Status status = Status::Unavailable;
        QString error;
        AginoStore::Manifest manifest;
        AginoStore::Catalog catalog;
        QHash<QString, qint64> stats;   // app id -> downloads
    };

    CatalogCache(QString dir, QStringList keyDirs);

    // minSerial: refuse anything older than what is already shown (rollback protection).
    Loaded load(qint64 minSerial) const;
    // Reads a file listed in the manifest and checks size and sha256; empty on any problem.
    QByteArray readVerified(const AginoStore::Manifest &manifest, const QString &path) const;
    QDateTime manifestModified() const;

private:
    QString m_dir;
    QStringList m_keyDirs;
};
