// SPDX-License-Identifier: GPL-3.0-or-later
#include "manifest.h"

#include "catalog.h"

#include <QBuffer>
#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTemporaryDir>
#include <QtTest>

using namespace AginoStore;

namespace {

const QString kDataDir = QStringLiteral(TEST_DATA_DIR);
const QString kCatalogDir = kDataDir + QStringLiteral("/catalog");

QByteArray readFile(const QString &path)
{
    QFile f(path);
    return f.open(QIODevice::ReadOnly) ? f.readAll() : QByteArray();
}

QByteArray testKey()
{
    return QByteArray::fromBase64(readFile(kDataDir + QStringLiteral("/keys/test-ed25519.pub")).trimmed());
}

QByteArray manifestBytes() { return readFile(kCatalogDir + QStringLiteral("/manifest.json")); }
QByteArray signatureBytes() { return readFile(kCatalogDir + QStringLiteral("/manifest.json.sig")); }

const QDateTime kNow = QDateTime::fromString(QStringLiteral("2026-11-02T12:00:00Z"), Qt::ISODate);

QByteArray manifestWithFile(const QJsonObject &file)
{
    QJsonObject root;
    root.insert(QStringLiteral("schema"), 1);
    root.insert(QStringLiteral("serial"), 7);
    root.insert(QStringLiteral("revision"), QStringLiteral("r7"));
    root.insert(QStringLiteral("created"), QStringLiteral("2026-11-02T00:00:00Z"));
    root.insert(QStringLiteral("expires"), QStringLiteral("2026-11-16T00:00:00Z"));
    root.insert(QStringLiteral("files"), QJsonArray{file});
    return QJsonDocument(root).toJson(QJsonDocument::Compact);
}

QJsonObject goodFile()
{
    return QJsonObject{{QStringLiteral("path"), QStringLiteral("catalog.json")},
                       {QStringLiteral("sha256"), QString(64, QLatin1Char('a'))},
                       {QStringLiteral("size"), 10}};
}

} // namespace

class TestManifest : public QObject
{
    Q_OBJECT
private slots:
    void sha256Known();
    void sha256Device();
    void keyLoading();
    void verifyGoodSignature();
    void verifyTamperedMessage();
    void verifyTamperedSignature();
    void verifyWrongKey();
    void verifyBadSizes();
    void parseSampleManifest();
    void parseRejections_data();
    void parseRejections();
    void verifyManifestAccepts();
    void verifyManifestBadSignatureEncoding();
    void verifyManifestTamperedBytes();
    void verifyManifestUntrustedKey();
    void verifyManifestNoKeys();
    void verifyManifestRollback();
    void verifyManifestExpired();
    void verifyManifestCreatedInFuture();
    void verifyManifestSignatureWithNewline();
    void matchesEntryCases();
    void fullCatalogVerification();
};

void TestManifest::sha256Known()
{
    QCOMPARE(sha256Hex(QByteArray()),
             QStringLiteral("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"));
    QCOMPARE(sha256Hex(QByteArray("abc")),
             QStringLiteral("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));
}

void TestManifest::sha256Device()
{
    QByteArray data(100000, 'x');
    QBuffer buf(&data);
    QVERIFY(buf.open(QIODevice::ReadOnly));
    QCOMPARE(sha256Hex(buf), sha256Hex(data));

    QFile missing(kDataDir + QStringLiteral("/does-not-exist"));
    QCOMPARE(sha256Hex(missing), QString());
}

void TestManifest::keyLoading()
{
    const QList<QByteArray> keys = loadTrustedKeys({kDataDir + QStringLiteral("/keys"),
                                                    kDataDir + QStringLiteral("/no-such-dir")});
    QCOMPARE(keys.size(), 1);
    QCOMPARE(keys.at(0).size(), 32);
    QCOMPARE(keys.at(0), testKey());
    QVERIFY(loadTrustedKeys({}).isEmpty());

    // Invalid files are skipped, valid ones kept.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    QFile bad(tmp.filePath(QStringLiteral("bad.pub")));
    QVERIFY(bad.open(QIODevice::WriteOnly));
    bad.write("not base64 !!!\n");
    bad.close();
    QFile shortKey(tmp.filePath(QStringLiteral("short.pub")));
    QVERIFY(shortKey.open(QIODevice::WriteOnly));
    shortKey.write(QByteArray(16, 'k').toBase64() + "\n");
    shortKey.close();
    QFile good(tmp.filePath(QStringLiteral("good.pub")));
    QVERIFY(good.open(QIODevice::WriteOnly));
    good.write(QByteArray(32, 'k').toBase64() + "\n");
    good.close();
    QFile other(tmp.filePath(QStringLiteral("readme.txt")));
    QVERIFY(other.open(QIODevice::WriteOnly));
    other.write(QByteArray(32, 'z').toBase64());
    other.close();
    const QList<QByteArray> found = loadTrustedKeys({tmp.path()});
    QCOMPARE(found.size(), 1);
    QCOMPARE(found.at(0), QByteArray(32, 'k'));
}

void TestManifest::verifyGoodSignature()
{
    const QByteArray sig = QByteArray::fromBase64(signatureBytes().trimmed());
    QCOMPARE(sig.size(), 64);
    QVERIFY(verifyEd25519(manifestBytes(), sig, testKey()));
}

void TestManifest::verifyTamperedMessage()
{
    const QByteArray sig = QByteArray::fromBase64(signatureBytes().trimmed());
    QByteArray msg = manifestBytes();
    QVERIFY(!msg.isEmpty());
    msg[msg.size() / 2] = static_cast<char>(msg.at(msg.size() / 2) ^ 0x01);
    QVERIFY(!verifyEd25519(msg, sig, testKey()));
    QVERIFY(!verifyEd25519(manifestBytes() + "\n", sig, testKey()));
    QVERIFY(!verifyEd25519(QByteArray(), sig, testKey()));
}

void TestManifest::verifyTamperedSignature()
{
    QByteArray sig = QByteArray::fromBase64(signatureBytes().trimmed());
    sig[10] = static_cast<char>(sig.at(10) ^ 0x80);
    QVERIFY(!verifyEd25519(manifestBytes(), sig, testKey()));
}

void TestManifest::verifyWrongKey()
{
    const QByteArray sig = QByteArray::fromBase64(signatureBytes().trimmed());
    QByteArray key = testKey();
    key[0] = static_cast<char>(key.at(0) ^ 0x01);
    QVERIFY(!verifyEd25519(manifestBytes(), sig, key));
    QVERIFY(!verifyEd25519(manifestBytes(), sig, QByteArray(32, '\0')));
}

void TestManifest::verifyBadSizes()
{
    const QByteArray sig = QByteArray::fromBase64(signatureBytes().trimmed());
    QVERIFY(!verifyEd25519(manifestBytes(), sig.left(63), testKey()));
    QVERIFY(!verifyEd25519(manifestBytes(), sig + "x", testKey()));
    QVERIFY(!verifyEd25519(manifestBytes(), QByteArray(), testKey()));
    QVERIFY(!verifyEd25519(manifestBytes(), sig, testKey().left(31)));
    QVERIFY(!verifyEd25519(manifestBytes(), sig, QByteArray()));
}

void TestManifest::parseSampleManifest()
{
    QString error;
    const auto m = parseManifest(manifestBytes(), &error);
    QVERIFY2(m.has_value(), qPrintable(error));
    QCOMPARE(m->schema, 1);
    QCOMPARE(m->serial, qint64(1));
    QVERIFY(m->created.isValid());
    QVERIFY(m->expires > m->created);
    QVERIFY(m->entry(QStringLiteral("catalog.json")).has_value());
    QVERIFY(!m->entry(QStringLiteral("manifest.json")).has_value());
    QVERIFY(!m->entry(QStringLiteral("nope.json")).has_value());
}

void TestManifest::parseRejections_data()
{
    QTest::addColumn<QByteArray>("json");
    QTest::newRow("garbage") << QByteArray("garbage");
    QTest::newRow("array") << QByteArray("[]");
    QTest::newRow("empty-object") << QByteArray("{}");

    auto withFile = [](const QString &key, const QJsonValue &value) {
        QJsonObject f = goodFile();
        if (value.isUndefined())
            f.remove(key);
        else
            f.insert(key, value);
        return manifestWithFile(f);
    };
    QTest::newRow("abs-path") << withFile(QStringLiteral("path"), QStringLiteral("/etc/passwd"));
    QTest::newRow("dotdot-path") << withFile(QStringLiteral("path"), QStringLiteral("a/../../b"));
    QTest::newRow("backslash-path") << withFile(QStringLiteral("path"), QStringLiteral("a\\b"));
    QTest::newRow("no-path") << withFile(QStringLiteral("path"), QJsonValue(QJsonValue::Undefined));
    QTest::newRow("short-sha") << withFile(QStringLiteral("sha256"), QString(63, QLatin1Char('a')));
    QTest::newRow("upper-sha") << withFile(QStringLiteral("sha256"), QString(64, QLatin1Char('A')));
    QTest::newRow("nonhex-sha") << withFile(QStringLiteral("sha256"), QString(64, QLatin1Char('z')));
    QTest::newRow("no-sha") << withFile(QStringLiteral("sha256"), QJsonValue(QJsonValue::Undefined));
    QTest::newRow("negative-size") << withFile(QStringLiteral("size"), -1);
    QTest::newRow("fractional-size") << withFile(QStringLiteral("size"), 1.5);
    QTest::newRow("string-size") << withFile(QStringLiteral("size"), QStringLiteral("10"));
    QTest::newRow("no-size") << withFile(QStringLiteral("size"), QJsonValue(QJsonValue::Undefined));

    // Top-level fields.
    const QByteArray good = manifestWithFile(goodFile());
    const QJsonObject base = QJsonDocument::fromJson(good).object();
    for (const char *key : {"schema", "serial", "revision", "created", "expires", "files"}) {
        QJsonObject o = base;
        o.remove(QString::fromLatin1(key));
        QTest::newRow(qPrintable(QStringLiteral("missing-") + QString::fromLatin1(key)))
            << QJsonDocument(o).toJson(QJsonDocument::Compact);
    }
    QJsonObject badSerial = base;
    badSerial.insert(QStringLiteral("serial"), 0);
    QTest::newRow("zero-serial") << QJsonDocument(badSerial).toJson(QJsonDocument::Compact);
    QJsonObject badDate = base;
    badDate.insert(QStringLiteral("expires"), QStringLiteral("soon"));
    QTest::newRow("bad-date") << QJsonDocument(badDate).toJson(QJsonDocument::Compact);
    QJsonObject badSchema = base;
    badSchema.insert(QStringLiteral("schema"), 2);
    QTest::newRow("bad-schema") << QJsonDocument(badSchema).toJson(QJsonDocument::Compact);
    QJsonObject dup = base;
    dup.insert(QStringLiteral("files"), QJsonArray{goodFile(), goodFile()});
    QTest::newRow("duplicate-path") << QJsonDocument(dup).toJson(QJsonDocument::Compact);
}

void TestManifest::parseRejections()
{
    QFETCH(QByteArray, json);
    QString error;
    QVERIFY(!parseManifest(json, &error).has_value());
    QVERIFY(!error.isEmpty());
    // The well-formed counterpart parses.
    QVERIFY(parseManifest(manifestWithFile(goodFile()), nullptr).has_value());
}

void TestManifest::verifyManifestAccepts()
{
    const VerifyResult r = verifyManifest(manifestBytes(), signatureBytes(), {testKey()}, 0, kNow);
    QVERIFY2(r.ok, qPrintable(r.error));
    QVERIFY(!r.expired);
    QCOMPARE(r.manifest.serial, qint64(1));
    // Same serial as cached is fine (no change); equal is not a rollback.
    QVERIFY(verifyManifest(manifestBytes(), signatureBytes(), {testKey()}, 1, kNow).ok);
}

void TestManifest::verifyManifestBadSignatureEncoding()
{
    QVERIFY(!verifyManifest(manifestBytes(), QByteArray("!!!not base64!!!"), {testKey()}, 0, kNow).ok);
    QVERIFY(!verifyManifest(manifestBytes(), QByteArray(), {testKey()}, 0, kNow).ok);
    QVERIFY(!verifyManifest(manifestBytes(), QByteArray("QUJD"), {testKey()}, 0, kNow).ok);
}

void TestManifest::verifyManifestTamperedBytes()
{
    QByteArray msg = manifestBytes();
    const int pos = msg.indexOf("\"serial\": 1");
    QVERIFY(pos > 0);
    msg.replace(pos + 10, 1, "9");
    const VerifyResult r = verifyManifest(msg, signatureBytes(), {testKey()}, 0, kNow);
    QVERIFY(!r.ok);
    QVERIFY(!r.expired);
    QVERIFY(!r.error.isEmpty());
}

void TestManifest::verifyManifestUntrustedKey()
{
    QByteArray wrong = testKey();
    wrong[5] = static_cast<char>(wrong.at(5) ^ 0x10);
    QVERIFY(!verifyManifest(manifestBytes(), signatureBytes(), {wrong}, 0, kNow).ok);
    // Any trusted key may verify.
    QVERIFY(verifyManifest(manifestBytes(), signatureBytes(), {wrong, testKey()}, 0, kNow).ok);
}

void TestManifest::verifyManifestNoKeys()
{
    QVERIFY(!verifyManifest(manifestBytes(), signatureBytes(), {}, 0, kNow).ok);
}

void TestManifest::verifyManifestRollback()
{
    const VerifyResult r = verifyManifest(manifestBytes(), signatureBytes(), {testKey()}, 2, kNow);
    QVERIFY(!r.ok);
    QVERIFY(!r.expired);
    QVERIFY(r.error.contains(QStringLiteral("older")));
}

void TestManifest::verifyManifestExpired()
{
    const QDateTime later = QDateTime::fromString(QStringLiteral("2040-01-01T00:00:00Z"), Qt::ISODate);
    const VerifyResult r = verifyManifest(manifestBytes(), signatureBytes(), {testKey()}, 0, later);
    QVERIFY(!r.ok);
    QVERIFY(r.expired);
    // The signature was fine, so the manifest content is available for read-only use.
    QCOMPARE(r.manifest.serial, qint64(1));
    QVERIFY(!r.manifest.files.isEmpty());
}

void TestManifest::verifyManifestCreatedInFuture()
{
    // created is 2026-11-02T00:00:00Z: more than a day after "now" is refused, within a day is fine.
    const QDateTime early = QDateTime::fromString(QStringLiteral("2026-10-30T00:00:00Z"), Qt::ISODate);
    QVERIFY(!verifyManifest(manifestBytes(), signatureBytes(), {testKey()}, 0, early).ok);
    const QDateTime close = QDateTime::fromString(QStringLiteral("2026-11-01T12:00:00Z"), Qt::ISODate);
    QVERIFY(verifyManifest(manifestBytes(), signatureBytes(), {testKey()}, 0, close).ok);
}

void TestManifest::verifyManifestSignatureWithNewline()
{
    QVERIFY(signatureBytes().endsWith('\n'));
    QVERIFY(verifyManifest(manifestBytes(), signatureBytes().trimmed(), {testKey()}, 0, kNow).ok);
    QVERIFY(verifyManifest(manifestBytes(), signatureBytes() + "\n", {testKey()}, 0, kNow).ok);
}

void TestManifest::matchesEntryCases()
{
    const QByteArray data("hello");
    ManifestEntry e{QStringLiteral("x"), sha256Hex(data), data.size()};
    QVERIFY(matchesEntry(data, e));
    QVERIFY(!matchesEntry(QByteArray("hellp"), e));
    QVERIFY(!matchesEntry(data + "!", e));
    ManifestEntry wrongSize = e;
    wrongSize.size += 1;
    QVERIFY(!matchesEntry(data, wrongSize));
    ManifestEntry wrongHash = e;
    wrongHash.sha256 = QString(64, QLatin1Char('0'));
    QVERIFY(!matchesEntry(data, wrongHash));
}

void TestManifest::fullCatalogVerification()
{
    const VerifyResult r = verifyManifest(manifestBytes(), signatureBytes(), loadTrustedKeys({kDataDir + QStringLiteral("/keys")}), 0, kNow);
    QVERIFY2(r.ok, qPrintable(r.error));
    QVERIFY(r.manifest.files.size() > 20);

    // Every listed file matches its size and hash.
    QSet<QString> listed;
    for (const ManifestEntry &e : r.manifest.files) {
        QVERIFY2(isSafeRelativePath(e.path), qPrintable(e.path));
        listed.insert(e.path);
        const QByteArray data = readFile(kCatalogDir + QLatin1Char('/') + e.path);
        QVERIFY2(!data.isEmpty(), qPrintable(e.path));
        QVERIFY2(matchesEntry(data, e), qPrintable(e.path));
        QFile f(kCatalogDir + QLatin1Char('/') + e.path);
        QCOMPARE(sha256Hex(f), e.sha256);
    }
    QVERIFY(listed.contains(QStringLiteral("catalog.json")));
    QVERIFY(listed.contains(QStringLiteral("stats.json")));

    // Every file in the tree (except manifest and signature) is listed.
    QDirIterator it(kCatalogDir, QDir::Files, QDirIterator::Subdirectories);
    const QDir root(kCatalogDir);
    int onDisk = 0;
    while (it.hasNext()) {
        const QString rel = root.relativeFilePath(it.next());
        if (rel == QLatin1String("manifest.json") || rel == QLatin1String("manifest.json.sig"))
            continue;
        ++onDisk;
        QVERIFY2(listed.contains(rel), qPrintable(rel));
    }
    QCOMPARE(onDisk, r.manifest.files.size());

    // One flipped byte in a listed file is detected.
    const ManifestEntry first = r.manifest.files.first();
    QByteArray corrupted = readFile(kCatalogDir + QLatin1Char('/') + first.path);
    corrupted[0] = static_cast<char>(corrupted.at(0) ^ 0x01);
    QVERIFY(!matchesEntry(corrupted, first));
}

QTEST_APPLESS_MAIN(TestManifest)
#include "test_manifest.moc"
