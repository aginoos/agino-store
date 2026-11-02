// SPDX-License-Identifier: GPL-3.0-or-later
// Signed manifest: Ed25519 verification, serial/expiry checks, file hashes (docs/design.md 4, 4.2).
#pragma once

#include <QByteArray>
#include <QDateTime>
#include <QList>
#include <QString>
#include <QStringList>
#include <optional>

class QIODevice;

namespace AginoStore {

struct ManifestEntry {
    QString path;
    QString sha256;          // lower-case hex
    qint64 size = 0;
};

struct Manifest {
    int schema = 0;
    qint64 serial = 0;
    QString revision;
    QDateTime created;
    QDateTime expires;
    QList<ManifestEntry> files;
    std::optional<ManifestEntry> entry(const QString &path) const;
};

QString sha256Hex(const QByteArray &data);
// Streams the device; empty string on read error.
QString sha256Hex(QIODevice &device);

// Raw 32-byte public key / 64-byte signature.
bool verifyEd25519(const QByteArray &message, const QByteArray &signature, const QByteArray &publicKey);

// Reads every *.pub in the directories (one base64 line, 32 raw bytes); invalid files are skipped.
QList<QByteArray> loadTrustedKeys(const QStringList &dirs);

std::optional<Manifest> parseManifest(const QByteArray &json, QString *error);

struct VerifyResult {
    bool ok = false;
    bool expired = false;    // signature fine but past "expires"
    QString error;
    Manifest manifest;
};

// Checks the base64 signature against any trusted key, parses the manifest, rejects
// serial < minSerial, and flags expiry against now. Does not look at the listed files.
VerifyResult verifyManifest(const QByteArray &manifestJson, const QByteArray &signatureBase64,
                            const QList<QByteArray> &trustedKeys, qint64 minSerial,
                            const QDateTime &now);

// True when data matches the entry's size and sha256.
bool matchesEntry(const QByteArray &data, const ManifestEntry &entry);

} // namespace AginoStore
