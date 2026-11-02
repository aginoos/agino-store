// SPDX-License-Identifier: GPL-3.0-or-later
// The verified catalog cache in /var/cache/agino-store/catalog/ (docs/design.md section 4).
// Everything is re-verified on load: signature, then the hash of every file we read.
#pragma once

#include "catalog.h"
#include "config.h"
#include "manifest.h"

#include <QByteArray>
#include <QString>
#include <optional>

namespace AginoStore {

struct CatalogSnapshot {
    bool valid = false;        // signature, manifest and catalog.json verified
    bool expired = false;      // signature fine but past "expires": read-only use
    QString error;
    Manifest manifest;
    QByteArray manifestBytes;
    Catalog catalog;
};

class CatalogCache
{
public:
    explicit CatalogCache(const HelperConfig &config);

    void reload();
    const CatalogSnapshot &snapshot() const { return m_snapshot; }

    // Serial of the cached manifest (0 when there is none); used as the rollback floor.
    qint64 serial() const { return m_snapshot.manifest.serial; }
    // max(persisted highest accepted serial, cached serial): no manifest below it is accepted.
    qint64 serialFloor() const;
    // Persists serial as the new floor when it is higher than the stored one.
    bool recordSerial(qint64 serial, QString *error);

    // Detail file of an app, verified against the manifest.
    std::optional<AppDetail> loadDetail(const AppSummary &app, QString *error) const;

private:
    HelperConfig m_config;
    CatalogSnapshot m_snapshot;
};

} // namespace AginoStore
