// SPDX-License-Identifier: GPL-3.0-or-later
#include "manifest.h"

#include "catalog.h"

#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QIODevice>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QRegularExpression>
#include <QSet>

#include <openssl/evp.h>

#include <cmath>

namespace AginoStore {

namespace {

constexpr int kEd25519KeySize = 32;
constexpr int kEd25519SigSize = 64;

void setError(QString *error, const QString &text)
{
    if (error)
        *error = text;
}

bool isHex64(const QString &s)
{
    static const QRegularExpression re(QStringLiteral("\\A[0-9a-f]{64}\\z"));
    return re.match(s).hasMatch();
}

// JSON number that is a whole, non-negative value representable exactly in a double.
bool readInteger(const QJsonValue &v, qint64 *out)
{
    if (!v.isDouble())
        return false;
    const double d = v.toDouble();
    if (!std::isfinite(d) || d < 0 || d > 9007199254740992.0 || d != std::floor(d))
        return false;
    *out = static_cast<qint64>(d);
    return true;
}

// Strict base64: returns an empty array on any decoding error.
QByteArray decodeBase64(const QByteArray &text)
{
    const auto res = QByteArray::fromBase64Encoding(
        text.trimmed(), QByteArray::Base64Encoding | QByteArray::AbortOnBase64DecodingErrors);
    if (!res)
        return {};
    return res.decoded;
}

} // namespace

std::optional<ManifestEntry> Manifest::entry(const QString &path) const
{
    for (const ManifestEntry &e : files) {
        if (e.path == path)
            return e;
    }
    return std::nullopt;
}

QString sha256Hex(const QByteArray &data)
{
    return QString::fromLatin1(QCryptographicHash::hash(data, QCryptographicHash::Sha256).toHex());
}

QString sha256Hex(QIODevice &device)
{
    if (!device.isOpen() && !device.open(QIODevice::ReadOnly))
        return {};
    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&device))
        return {};
    return QString::fromLatin1(hash.result().toHex());
}

bool verifyEd25519(const QByteArray &message, const QByteArray &signature, const QByteArray &publicKey)
{
    if (publicKey.size() != kEd25519KeySize || signature.size() != kEd25519SigSize)
        return false;

    EVP_PKEY *pkey = EVP_PKEY_new_raw_public_key(
        EVP_PKEY_ED25519, nullptr, reinterpret_cast<const unsigned char *>(publicKey.constData()),
        static_cast<size_t>(publicKey.size()));
    if (!pkey)
        return false;

    bool ok = false;
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    if (ctx) {
        if (EVP_DigestVerifyInit(ctx, nullptr, nullptr, nullptr, pkey) == 1) {
            ok = EVP_DigestVerify(ctx, reinterpret_cast<const unsigned char *>(signature.constData()),
                                  static_cast<size_t>(signature.size()),
                                  reinterpret_cast<const unsigned char *>(message.constData()),
                                  static_cast<size_t>(message.size())) == 1;
        }
        EVP_MD_CTX_free(ctx);
    }
    EVP_PKEY_free(pkey);
    return ok;
}

QList<QByteArray> loadTrustedKeys(const QStringList &dirs)
{
    QList<QByteArray> keys;
    for (const QString &dirPath : dirs) {
        const QDir dir(dirPath);
        const QStringList names = dir.entryList({QStringLiteral("*.pub")}, QDir::Files, QDir::Name);
        for (const QString &name : names) {
            QFile file(dir.filePath(name));
            if (!file.open(QIODevice::ReadOnly))
                continue;
            const QByteArray raw = decodeBase64(file.read(4096));
            if (raw.size() == kEd25519KeySize && !keys.contains(raw))
                keys.append(raw);
        }
    }
    return keys;
}

std::optional<Manifest> parseManifest(const QByteArray &json, QString *error)
{
    QJsonParseError perr;
    const QJsonDocument doc = QJsonDocument::fromJson(json, &perr);
    if (perr.error != QJsonParseError::NoError || !doc.isObject()) {
        setError(error, QStringLiteral("manifest is not a valid JSON object"));
        return std::nullopt;
    }
    const QJsonObject obj = doc.object();

    Manifest m;
    qint64 schema = 0;
    if (!readInteger(obj.value(QStringLiteral("schema")), &schema) || schema != 1) {
        setError(error, QStringLiteral("unsupported manifest schema"));
        return std::nullopt;
    }
    m.schema = static_cast<int>(schema);

    if (!readInteger(obj.value(QStringLiteral("serial")), &m.serial) || m.serial <= 0) {
        setError(error, QStringLiteral("manifest serial missing or invalid"));
        return std::nullopt;
    }
    if (!obj.value(QStringLiteral("revision")).isString()) {
        setError(error, QStringLiteral("manifest revision missing"));
        return std::nullopt;
    }
    m.revision = obj.value(QStringLiteral("revision")).toString();

    m.created = QDateTime::fromString(obj.value(QStringLiteral("created")).toString(), Qt::ISODate);
    m.expires = QDateTime::fromString(obj.value(QStringLiteral("expires")).toString(), Qt::ISODate);
    if (!m.created.isValid() || !m.expires.isValid()) {
        setError(error, QStringLiteral("manifest created/expires missing or invalid"));
        return std::nullopt;
    }

    if (!obj.value(QStringLiteral("files")).isArray()) {
        setError(error, QStringLiteral("manifest files missing"));
        return std::nullopt;
    }
    QSet<QString> seen;
    const QJsonArray files = obj.value(QStringLiteral("files")).toArray();
    for (const QJsonValue &fv : files) {
        if (!fv.isObject()) {
            setError(error, QStringLiteral("manifest file entry is not an object"));
            return std::nullopt;
        }
        const QJsonObject fo = fv.toObject();
        ManifestEntry e;
        if (!fo.value(QStringLiteral("path")).isString()) {
            setError(error, QStringLiteral("manifest file entry has no path"));
            return std::nullopt;
        }
        e.path = fo.value(QStringLiteral("path")).toString();
        if (!isSafeRelativePath(e.path)) {
            setError(error, QStringLiteral("unsafe path in manifest: %1").arg(e.path));
            return std::nullopt;
        }
        if (!fo.value(QStringLiteral("sha256")).isString()
            || !isHex64(fo.value(QStringLiteral("sha256")).toString())) {
            setError(error, QStringLiteral("invalid sha256 for %1").arg(e.path));
            return std::nullopt;
        }
        e.sha256 = fo.value(QStringLiteral("sha256")).toString();
        if (!readInteger(fo.value(QStringLiteral("size")), &e.size)) {
            setError(error, QStringLiteral("invalid size for %1").arg(e.path));
            return std::nullopt;
        }
        if (seen.contains(e.path)) {
            setError(error, QStringLiteral("duplicate path in manifest: %1").arg(e.path));
            return std::nullopt;
        }
        seen.insert(e.path);
        m.files.append(e);
    }
    return m;
}

VerifyResult verifyManifest(const QByteArray &manifestJson, const QByteArray &signatureBase64,
                            const QList<QByteArray> &trustedKeys, qint64 minSerial,
                            const QDateTime &now)
{
    VerifyResult result;

    const QByteArray sig = decodeBase64(signatureBase64);
    if (sig.size() != kEd25519SigSize) {
        result.error = QStringLiteral("signature is not a 64-byte base64 value");
        return result;
    }
    bool signedOk = false;
    for (const QByteArray &key : trustedKeys) {
        if (verifyEd25519(manifestJson, sig, key)) {
            signedOk = true;
            break;
        }
    }
    if (!signedOk) {
        result.error = QStringLiteral("signature verification failed");
        return result;
    }

    QString perr;
    const std::optional<Manifest> m = parseManifest(manifestJson, &perr);
    if (!m) {
        result.error = perr;
        return result;
    }
    result.manifest = *m;

    if (m->created > now.addDays(1)) {
        result.error = QStringLiteral("manifest created is in the future");
        return result;
    }
    if (m->serial < minSerial) {
        result.error = QStringLiteral("manifest serial %1 is older than cached %2")
                           .arg(m->serial)
                           .arg(minSerial);
        return result;
    }
    if (m->expires <= now) {
        // Signature is fine: callers may still show a cached expired catalog read-only.
        result.expired = true;
        result.error = QStringLiteral("manifest expired");
        return result;
    }
    result.ok = true;
    return result;
}

bool matchesEntry(const QByteArray &data, const ManifestEntry &entry)
{
    return data.size() == entry.size && sha256Hex(data) == entry.sha256;
}

} // namespace AginoStore
