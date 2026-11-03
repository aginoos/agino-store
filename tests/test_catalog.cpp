// SPDX-License-Identifier: GPL-3.0-or-later
#include "catalog.h"

#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QtTest>
#include <functional>

using namespace AginoStore;

namespace {

QByteArray readData(const QString &relative)
{
    QFile f(QStringLiteral(TEST_DATA_DIR "/") + relative);
    return f.open(QIODevice::ReadOnly) ? f.readAll() : QByteArray();
}

QByteArray toJson(const QJsonObject &o)
{
    return QJsonDocument(o).toJson(QJsonDocument::Compact);
}

QJsonObject sampleCatalogObject()
{
    return QJsonDocument::fromJson(readData(QStringLiteral("catalog/catalog.json"))).object();
}

// Applies edit to the first app object of the sample catalog and returns the serialized result.
QByteArray catalogWithApp(const std::function<void(QJsonObject &)> &edit)
{
    QJsonObject root = sampleCatalogObject();
    QJsonArray apps = root.value(QStringLiteral("apps")).toArray();
    QJsonObject app = apps.at(0).toObject();
    edit(app);
    apps.replace(0, app);
    root.insert(QStringLiteral("apps"), apps);
    return toJson(root);
}

QJsonObject sampleDetailObject(const QString &id)
{
    return QJsonDocument::fromJson(readData(QStringLiteral("catalog/apps/") + id + QStringLiteral(".json"))).object();
}

} // namespace

class TestCatalog : public QObject
{
    Q_OBJECT
private slots:
    void parsesSampleCatalog();
    void sampleCatalogShape();
    void rejectsGarbage();
    void rejectsMissingFields_data();
    void rejectsMissingFields();
    void rejectsUnsafePaths_data();
    void rejectsUnsafePaths();
    void rejectsDebWithoutDeb();
    void l10nParsing_data();
    void l10nParsing();
    void l10nDebRejected();
    void l10nSelection_data();
    void l10nSelection();
    void l10nAllPackages();
    void launchable_data();
    void launchable();
    void rejectsBadSha256_data();
    void rejectsBadSha256();
    void rejectsUnknownSourceAndKind();
    void rejectsDuplicatesAndDanglingRefs();

    void parsesDetail();
    void detailRejections();
    void personalMaintainerPresent();
    void installProfileValidation();
    void debconfLines_data();
    void debconfLines();
    void defaultFiles_data();
    void defaultFiles();
    void safeRelativePath_data();
    void safeRelativePath();

    void textFallback();
    void paragraphFallback();
    void screenshotLanguage_data();
    void screenshotLanguage();
    void screenshotsFallBackToOtherSet();
    void statsParsing();
};

void TestCatalog::parsesSampleCatalog()
{
    QString error;
    const auto cat = parseCatalog(readData(QStringLiteral("catalog/catalog.json")), &error);
    QVERIFY2(cat.has_value(), qPrintable(error));
    QCOMPARE(cat->schema, 1);
}

void TestCatalog::sampleCatalogShape()
{
    const auto cat = parseCatalog(readData(QStringLiteral("catalog/catalog.json")), nullptr);
    QVERIFY(cat.has_value());
    QCOMPARE(cat->categories.size(), 8);
    QVERIFY(cat->apps.size() >= 16);

    bool seenSystem = false, seenOfficial = false, seenPersonal = false;
    for (const AppSummary &a : cat->apps) {
        seenSystem |= a.source == Source::System;
        seenOfficial |= a.source == Source::Official;
        seenPersonal |= a.source == Source::Personal;
        QVERIFY(a.updated.isValid());
        QVERIFY(!a.name.pick(QStringLiteral("zh_CN")).isEmpty());
        if (a.package.kind == PackageRef::Kind::Deb) {
            QVERIFY(a.package.deb.has_value());
            QCOMPARE(a.package.deb->sha256.size(), 64);
        } else {
            QVERIFY(!a.package.deb.has_value());
        }
    }
    QVERIFY(seenSystem && seenOfficial && seenPersonal);

    const AppSummary *gimp = cat->app(QStringLiteral("gimp"));
    QVERIFY(gimp);
    QCOMPARE(gimp->package.kind, PackageRef::Kind::Milkyway);
    QCOMPARE(gimp->package.name, QStringLiteral("gimp"));
    QCOMPARE(sourceToString(gimp->source), QStringLiteral("system"));
    QVERIFY(cat->app(QStringLiteral("no-such-app")) == nullptr);

    const AppSummary *wps = cat->app(QStringLiteral("wps-office"));
    QVERIFY(wps);
    QVERIFY(wps->proprietary);
    QCOMPARE(wps->package.kind, PackageRef::Kind::Deb);
    QVERIFY(wps->package.deb->url.startsWith(QStringLiteral("file:///usr/share/agino-store/test-debs/")));

    // First banner:false featured entry is "essentials".
    int banners = 0;
    for (const Featured &f : cat->featured)
        banners += f.banner ? 1 : 0;
    QVERIFY(banners >= 1);
}

void TestCatalog::rejectsGarbage()
{
    QString error;
    QVERIFY(!parseCatalog(QByteArray("not json"), &error).has_value());
    QVERIFY(!error.isEmpty());
    QVERIFY(!parseCatalog(QByteArray("[]"), &error).has_value());
    QVERIFY(!parseCatalog(QByteArray("{}"), &error).has_value());
    QVERIFY(!parseCatalog(QByteArray(), nullptr).has_value());
    QVERIFY(!parseAppDetail(QByteArray("nope"), &error).has_value());
    // Wrong schema number.
    QJsonObject root = sampleCatalogObject();
    root.insert(QStringLiteral("schema"), 2);
    QVERIFY(!parseCatalog(toJson(root), &error).has_value());
}

void TestCatalog::rejectsMissingFields_data()
{
    QTest::addColumn<QString>("field");
    for (const char *f : {"id", "name", "summary", "icon", "categories", "source", "license",
                          "proprietary", "updated", "package", "detail"})
        QTest::newRow(f) << QString::fromLatin1(f);
}

void TestCatalog::rejectsMissingFields()
{
    QFETCH(QString, field);
    QString error;
    const QByteArray json = catalogWithApp([&](QJsonObject &app) { app.remove(field); });
    QVERIFY(!parseCatalog(json, &error).has_value());
    QVERIFY(!error.isEmpty());

    // Wrong type for the same field.
    const QByteArray typed = catalogWithApp([&](QJsonObject &app) { app.insert(field, 12345); });
    QVERIFY(!parseCatalog(typed, &error).has_value());
}

void TestCatalog::rejectsUnsafePaths_data()
{
    QTest::addColumn<QString>("field");
    QTest::addColumn<QString>("path");
    for (const char *f : {"icon", "detail"}) {
        QTest::newRow("dotdot") << QString::fromLatin1(f) << "../etc/passwd";
        QTest::newRow("nested-dotdot") << QString::fromLatin1(f) << "assets/../../x";
        QTest::newRow("absolute") << QString::fromLatin1(f) << "/etc/passwd";
        QTest::newRow("backslash") << QString::fromLatin1(f) << "assets\\x.svg";
        QTest::newRow("empty") << QString::fromLatin1(f) << "";
    }
}

void TestCatalog::rejectsUnsafePaths()
{
    QFETCH(QString, field);
    QFETCH(QString, path);
    const QByteArray json = catalogWithApp([&](QJsonObject &app) { app.insert(field, path); });
    QString error;
    QVERIFY(!parseCatalog(json, &error).has_value());
    QVERIFY(!error.isEmpty());
}

void TestCatalog::launchable_data()
{
    QTest::addColumn<QJsonValue>("value");
    QTest::addColumn<bool>("ok");
    QTest::newRow("reverse-dns") << QJsonValue(QStringLiteral("org.keepassxc.KeePassXC.desktop")) << true;
    QTest::newRow("plain") << QJsonValue(QStringLiteral("firefox-esr.desktop")) << true;
    QTest::newRow("path") << QJsonValue(QStringLiteral("../x.desktop")) << false;
    QTest::newRow("absolute") << QJsonValue(QStringLiteral("/usr/share/applications/x.desktop")) << false;
    QTest::newRow("no-suffix") << QJsonValue(QStringLiteral("firefox-esr")) << false;
    QTest::newRow("dotdot-inside") << QJsonValue(QStringLiteral("a..b.desktop")) << false;
    QTest::newRow("trailing-newline") << QJsonValue(QStringLiteral("firefox-esr.desktop\n")) << false;
    QTest::newRow("number") << QJsonValue(12) << false;
}

void TestCatalog::launchable()
{
    QFETCH(QJsonValue, value);
    QFETCH(bool, ok);
    const QByteArray json = catalogWithApp([&](QJsonObject &app) { app.insert(QStringLiteral("launchable"), value); });
    QString error;
    const auto catalog = parseCatalog(json, &error);
    QCOMPARE(catalog.has_value(), ok);
    if (ok)
        QCOMPARE(catalog->apps.first().launchable, value.toString());
}

namespace {
QJsonObject milkywayPackage()
{
    QJsonObject pkg;
    pkg.insert(QStringLiteral("kind"), QStringLiteral("milkyway"));
    pkg.insert(QStringLiteral("name"), QStringLiteral("thunderbird"));
    return pkg;
}

QJsonObject l10nObject(const QString &key, const QJsonArray &names)
{
    QJsonObject o;
    o.insert(key, names);
    return o;
}
} // namespace

void TestCatalog::l10nParsing_data()
{
    QTest::addColumn<QJsonValue>("value");
    QTest::addColumn<bool>("ok");
    const QJsonArray one{QStringLiteral("thunderbird-l10n-zh-cn")};
    QTest::newRow("zh_CN") << QJsonValue(l10nObject(QStringLiteral("zh_CN"), one)) << true;
    QTest::newRow("bare-language") << QJsonValue(l10nObject(QStringLiteral("fr"), one)) << true;
    QTest::newRow("bad-key") << QJsonValue(l10nObject(QStringLiteral("zh-cn"), one)) << false;
    QTest::newRow("lowercase-region") << QJsonValue(l10nObject(QStringLiteral("zh_cn"), one)) << false;
    QTest::newRow("empty-object") << QJsonValue(QJsonObject()) << false;
    QTest::newRow("empty-list") << QJsonValue(l10nObject(QStringLiteral("fr"), QJsonArray())) << false;
    QTest::newRow("not-object") << QJsonValue(one) << false;
    QTest::newRow("bad-name") << QJsonValue(l10nObject(QStringLiteral("fr"), QJsonArray{QStringLiteral("Bad Name")})) << false;
    QTest::newRow("dash-name") << QJsonValue(l10nObject(QStringLiteral("fr"), QJsonArray{QStringLiteral("-x-y")})) << false;
    QTest::newRow("non-string") << QJsonValue(l10nObject(QStringLiteral("fr"), QJsonArray{1})) << false;
    QTest::newRow("five-packages")
        << QJsonValue(l10nObject(QStringLiteral("fr"), QJsonArray{QStringLiteral("aa"), QStringLiteral("bb"),
                                                              QStringLiteral("cc"), QStringLiteral("dd"),
                                                              QStringLiteral("ee")}))
        << false;
    QJsonObject nine;
    for (const char *k : {"aa", "bb", "cc", "dd", "ee", "ff", "gg", "hh", "ii"})
        nine.insert(QString::fromLatin1(k), one);
    QTest::newRow("nine-languages") << QJsonValue(nine) << false;
}

void TestCatalog::l10nParsing()
{
    QFETCH(QJsonValue, value);
    QFETCH(bool, ok);
    const QByteArray json = catalogWithApp([&](QJsonObject &app) {
        app.insert(QStringLiteral("package"), milkywayPackage());
        app.insert(QStringLiteral("l10n"), value);
    });
    const auto catalog = parseCatalog(json, nullptr);
    QCOMPARE(catalog.has_value(), ok);
    if (ok)
        QVERIFY(!catalog->apps.first().l10n.isEmpty());
}

void TestCatalog::l10nDebRejected()
{
    // The sample catalog has deb apps; find one and give it an l10n block.
    QJsonObject root = sampleCatalogObject();
    QJsonArray apps = root.value(QStringLiteral("apps")).toArray();
    bool changed = false;
    for (int i = 0; i < apps.size() && !changed; ++i) {
        QJsonObject app = apps.at(i).toObject();
        if (app.value(QStringLiteral("package")).toObject().value(QStringLiteral("kind")).toString() != QLatin1String("deb"))
            continue;
        app.insert(QStringLiteral("l10n"), l10nObject(QStringLiteral("fr"), QJsonArray{QStringLiteral("x-l10n-fr")}));
        apps.replace(i, app);
        changed = true;
    }
    QVERIFY(changed);
    root.insert(QStringLiteral("apps"), apps);
    QVERIFY(!parseCatalog(toJson(root), nullptr).has_value());
}

void TestCatalog::l10nSelection_data()
{
    QTest::addColumn<QStringList>("keys");
    QTest::addColumn<QString>("locale");
    QTest::addColumn<QString>("expected");
    QTest::newRow("exact") << QStringList{"zh_CN", "zh_TW", "zh"} << "zh_TW" << "zh_TW";
    QTest::newRow("bare-language") << QStringList{"zh_CN", "zh"} << "zh_HK" << "zh";
    QTest::newRow("same-language-prefix") << QStringList{"zh_TW", "zh_CN", "fr"} << "zh_HK" << "zh_CN";
    QTest::newRow("bare-locale-prefers-region-key") << QStringList{"zh_CN"} << "zh" << "zh_CN";
    QTest::newRow("other-language") << QStringList{"zh_CN"} << "fr_FR" << "";
    QTest::newRow("language-only-key") << QStringList{"fr"} << "fr_CA" << "fr";
    QTest::newRow("no-partial-prefix") << QStringList{"zhx_CN"} << "zh_CN" << "";
    QTest::newRow("c-locale") << QStringList{"zh_CN"} << "C" << "";
    QTest::newRow("empty-locale") << QStringList{"zh_CN"} << "" << "";
    QTest::newRow("hostile-locale") << QStringList{"zh_CN"} << "zh_CN;rm" << "";
    QTest::newRow("no-keys") << QStringList{} << "zh_CN" << "";
}

void TestCatalog::l10nSelection()
{
    QFETCH(QStringList, keys);
    QFETCH(QString, locale);
    QFETCH(QString, expected);
    QHash<QString, QStringList> l10n;
    for (const QString &k : keys)
        l10n.insert(k, QStringList{k + QStringLiteral("-pkg")});
    QCOMPARE(pickL10nKey(l10n, locale), expected);
    QCOMPARE(l10nPackagesFor(l10n, locale),
             expected.isEmpty() ? QStringList() : QStringList{expected + QStringLiteral("-pkg")});
}

void TestCatalog::l10nAllPackages()
{
    QHash<QString, QStringList> l10n;
    l10n.insert(QStringLiteral("zh_CN"), {QStringLiteral("b-zh"), QStringLiteral("a-common")});
    l10n.insert(QStringLiteral("fr"), {QStringLiteral("a-common"), QStringLiteral("c-fr")});
    QCOMPARE(allL10nPackages(l10n), (QStringList{"a-common", "b-zh", "c-fr"}));
    QVERIFY(allL10nPackages({}).isEmpty());
}

void TestCatalog::rejectsDebWithoutDeb()
{
    const QByteArray json = catalogWithApp([](QJsonObject &app) {
        QJsonObject pkg;
        pkg.insert(QStringLiteral("kind"), QStringLiteral("deb"));
        pkg.insert(QStringLiteral("name"), QStringLiteral("foo"));
        app.insert(QStringLiteral("package"), pkg);
    });
    QString error;
    QVERIFY(!parseCatalog(json, &error).has_value());
    QVERIFY(error.contains(QStringLiteral("deb")));
}

void TestCatalog::rejectsBadSha256_data()
{
    QTest::addColumn<QString>("sha");
    QTest::newRow("short") << QString(63, QLatin1Char('a'));
    QTest::newRow("long") << QString(65, QLatin1Char('a'));
    QTest::newRow("upper") << QString(64, QLatin1Char('A'));
    QTest::newRow("non-hex") << QString(64, QLatin1Char('g'));
    QTest::newRow("empty") << QString();
}

void TestCatalog::rejectsBadSha256()
{
    QFETCH(QString, sha);
    QJsonObject root = sampleCatalogObject();
    QJsonArray apps = root.value(QStringLiteral("apps")).toArray();
    bool patched = false;
    for (int i = 0; i < apps.size() && !patched; ++i) {
        QJsonObject app = apps.at(i).toObject();
        QJsonObject pkg = app.value(QStringLiteral("package")).toObject();
        if (pkg.value(QStringLiteral("kind")).toString() != QLatin1String("deb"))
            continue;
        QJsonObject deb = pkg.value(QStringLiteral("deb")).toObject();
        deb.insert(QStringLiteral("sha256"), sha);
        pkg.insert(QStringLiteral("deb"), deb);
        app.insert(QStringLiteral("package"), pkg);
        apps.replace(i, app);
        patched = true;
    }
    QVERIFY(patched);
    root.insert(QStringLiteral("apps"), apps);
    QVERIFY(!parseCatalog(toJson(root), nullptr).has_value());
}

void TestCatalog::rejectsUnknownSourceAndKind()
{
    QVERIFY(!parseCatalog(catalogWithApp([](QJsonObject &a) {
        a.insert(QStringLiteral("source"), QStringLiteral("mystery"));
    }), nullptr).has_value());
    QVERIFY(!parseCatalog(catalogWithApp([](QJsonObject &a) {
        QJsonObject pkg = a.value(QStringLiteral("package")).toObject();
        pkg.insert(QStringLiteral("kind"), QStringLiteral("snap"));
        a.insert(QStringLiteral("package"), pkg);
    }), nullptr).has_value());
    QVERIFY(!parseCatalog(catalogWithApp([](QJsonObject &a) {
        a.insert(QStringLiteral("id"), QStringLiteral("Bad Id/../x"));
    }), nullptr).has_value());
    QVERIFY(!parseCatalog(catalogWithApp([](QJsonObject &a) {
        a.insert(QStringLiteral("updated"), QStringLiteral("yesterday"));
    }), nullptr).has_value());
}

void TestCatalog::rejectsDuplicatesAndDanglingRefs()
{
    QJsonObject root = sampleCatalogObject();
    QJsonArray apps = root.value(QStringLiteral("apps")).toArray();
    apps.append(apps.at(0));
    QJsonObject dup = root;
    dup.insert(QStringLiteral("apps"), apps);
    QVERIFY(!parseCatalog(toJson(dup), nullptr).has_value());

    QVERIFY(!parseCatalog(catalogWithApp([](QJsonObject &a) {
        a.insert(QStringLiteral("categories"), QJsonArray{QStringLiteral("nonexistent")});
    }), nullptr).has_value());

    QJsonObject dangling = sampleCatalogObject();
    QJsonArray feats = dangling.value(QStringLiteral("featured")).toArray();
    QJsonObject f0 = feats.at(0).toObject();
    f0.insert(QStringLiteral("apps"), QJsonArray{QStringLiteral("ghost-app")});
    feats.replace(0, f0);
    dangling.insert(QStringLiteral("featured"), feats);
    QVERIFY(!parseCatalog(toJson(dangling), nullptr).has_value());
}

void TestCatalog::parsesDetail()
{
    QString error;
    const auto d = parseAppDetail(readData(QStringLiteral("catalog/apps/wps-office.json")), &error);
    QVERIFY2(d.has_value(), qPrintable(error));
    QCOMPARE(d->id, QStringLiteral("wps-office"));
    QCOMPARE(d->description.values.size(), 2);
    QCOMPARE(d->screenshots.value(QStringLiteral("zh_CN")).size(), 2);
    QCOMPARE(d->screenshots.value(QStringLiteral("en_US")).size(), 2);
    QVERIFY(d->screenshots.value(QStringLiteral("en_US")).at(0).image.startsWith(QStringLiteral("assets/")));
    QCOMPARE(d->screenshots.value(QStringLiteral("en_US")).at(0).width, 1280);
    QCOMPARE(d->releases.size(), 2);
    QVERIFY(d->releases.at(0).date.isValid());
    QVERIFY(!d->releases.at(0).notes.pick(QStringLiteral("en_US")).isEmpty());
    QVERIFY(d->installProfile.isEmpty());
}

void TestCatalog::detailRejections()
{
    QJsonObject base = sampleDetailObject(QStringLiteral("gimp"));
    QVERIFY(parseAppDetail(toJson(base), nullptr).has_value());

    QJsonObject noId = base;
    noId.remove(QStringLiteral("id"));
    QVERIFY(!parseAppDetail(toJson(noId), nullptr).has_value());

    QJsonObject noDesc = base;
    noDesc.remove(QStringLiteral("description"));
    QVERIFY(!parseAppDetail(toJson(noDesc), nullptr).has_value());

    QJsonObject badDesc = base;
    badDesc.insert(QStringLiteral("description"), QJsonObject{{QStringLiteral("en_US"), QStringLiteral("flat string")}});
    QVERIFY(!parseAppDetail(toJson(badDesc), nullptr).has_value());

    QJsonObject badShot = base;
    QJsonObject shots = badShot.value(QStringLiteral("screenshots")).toObject();
    shots.insert(QStringLiteral("en_US"), QJsonArray{QJsonObject{{QStringLiteral("image"), QStringLiteral("../../escape.png")}}});
    badShot.insert(QStringLiteral("screenshots"), shots);
    QVERIFY(!parseAppDetail(toJson(badShot), nullptr).has_value());

    QJsonObject badRel = base;
    badRel.insert(QStringLiteral("releases"), QJsonArray{QJsonObject{{QStringLiteral("version"), QStringLiteral("1")}}});
    QVERIFY(!parseAppDetail(toJson(badRel), nullptr).has_value());
}

void TestCatalog::personalMaintainerPresent()
{
    // The catalog only checks structure; the maintainer rule is enforced where the app is
    // acted on. The sample detail of a personal app must carry one.
    const auto cat = parseCatalog(readData(QStringLiteral("catalog/catalog.json")), nullptr);
    QVERIFY(cat.has_value());
    int personal = 0;
    for (const AppSummary &a : cat->apps) {
        if (a.source != Source::Personal)
            continue;
        ++personal;
        const auto d = parseAppDetail(readData(QStringLiteral("catalog/") + a.detail), nullptr);
        QVERIFY(d.has_value());
        QVERIFY(!d->maintainer.isEmpty());
    }
    QVERIFY(personal >= 1);
}

void TestCatalog::installProfileValidation()
{
    const auto chrome = parseAppDetail(readData(QStringLiteral("catalog/apps/google-chrome.json")), nullptr);
    QVERIFY(chrome.has_value());
    QCOMPARE(chrome->installProfile.defaultFiles.size(), 1);
    QCOMPARE(chrome->installProfile.defaultFiles.at(0).path, QStringLiteral("/etc/default/google-chrome"));
    const auto code = parseAppDetail(readData(QStringLiteral("catalog/apps/code.json")), nullptr);
    QVERIFY(code.has_value());
    QCOMPARE(code->installProfile.debconf.size(), 1);

    auto withProfile = [](const QJsonObject &profile) {
        QJsonObject d = sampleDetailObject(QStringLiteral("gimp"));
        d.insert(QStringLiteral("install_profile"), profile);
        return toJson(d);
    };
    QString error;
    QVERIFY(!parseAppDetail(withProfile(QJsonObject{{QStringLiteral("debconf"),
                QJsonArray{QStringLiteral("a b badtype value")}}}), &error).has_value());
    QVERIFY(!parseAppDetail(withProfile(QJsonObject{{QStringLiteral("debconf"),
                QJsonArray{QStringLiteral("a b string v\nx c boolean true")}}}), &error).has_value());
    QVERIFY(!parseAppDetail(withProfile(QJsonObject{{QStringLiteral("default_files"),
                QJsonArray{QJsonObject{{QStringLiteral("path"), QStringLiteral("/etc/passwd")},
                                       {QStringLiteral("content"), QStringLiteral("x")}}}}}), &error).has_value());
    QVERIFY(!parseAppDetail(withProfile(QJsonObject{{QStringLiteral("default_files"),
                QJsonArray{QJsonObject{{QStringLiteral("path"), QStringLiteral("/etc/default/ok")}}}}}), &error).has_value());
    QVERIFY(parseAppDetail(withProfile(QJsonObject{{QStringLiteral("default_files"),
                QJsonArray{QJsonObject{{QStringLiteral("path"), QStringLiteral("/etc/default/ok")},
                                       {QStringLiteral("content"), QStringLiteral("A=1\n")}}}}}), &error).has_value());
}

void TestCatalog::debconfLines_data()
{
    QTest::addColumn<QString>("line");
    QTest::addColumn<bool>("valid");
    QTest::newRow("boolean") << "code code/add-microsoft-repo boolean false" << true;
    QTest::newRow("string-with-spaces") << "pkg pkg/q string hello big world" << true;
    QTest::newRow("select") << "pkg pkg/q select Choice" << true;
    QTest::newRow("empty-value") << "pkg pkg/q string " << true;
    QTest::newRow("password-type") << "pkg pkg/q password secret" << false;
    QTest::newRow("multiselect") << "pkg pkg/q multiselect a, b" << false;
    QTest::newRow("newline") << "pkg pkg/q string a\nb" << false;
    QTest::newRow("carriage-return") << "pkg pkg/q string a\rb" << false;
    QTest::newRow("too-few-fields") << "pkg pkg/q" << false;
    QTest::newRow("empty") << "" << false;
    QTest::newRow("leading-space") << " pkg pkg/q string x" << false;
}

void TestCatalog::debconfLines()
{
    QFETCH(QString, line);
    QFETCH(bool, valid);
    QCOMPARE(isValidDebconfLine(line), valid);
}

void TestCatalog::defaultFiles_data()
{
    QTest::addColumn<QString>("path");
    QTest::addColumn<int>("size");
    QTest::addColumn<bool>("valid");
    QTest::newRow("ok") << "/etc/default/google-chrome" << 10 << true;
    QTest::newRow("ok-dots") << "/etc/default/a.b+c-d" << 0 << true;
    QTest::newRow("exactly-4k") << "/etc/default/x" << 4096 << true;
    QTest::newRow("too-big") << "/etc/default/x" << 4097 << false;
    QTest::newRow("outside") << "/etc/passwd" << 1 << false;
    QTest::newRow("traversal") << "/etc/default/../passwd" << 1 << false;
    QTest::newRow("subdir") << "/etc/default/a/b" << 1 << false;
    QTest::newRow("uppercase") << "/etc/default/Foo" << 1 << false;
    QTest::newRow("leading-dot") << "/etc/default/.hidden" << 1 << false;
    QTest::newRow("relative") << "etc/default/x" << 1 << false;
    QTest::newRow("trailing-newline") << "/etc/default/x\n" << 1 << false;
}

void TestCatalog::defaultFiles()
{
    QFETCH(QString, path);
    QFETCH(int, size);
    QFETCH(bool, valid);
    DefaultFile f;
    f.path = path;
    f.content = QByteArray(size, 'a');
    QCOMPARE(isValidDefaultFile(f), valid);
}

void TestCatalog::safeRelativePath_data()
{
    QTest::addColumn<QString>("path");
    QTest::addColumn<bool>("safe");
    QTest::newRow("file") << "catalog.json" << true;
    QTest::newRow("nested") << "assets/ab.svg" << true;
    QTest::newRow("dotfile-name") << "assets/.x" << true;
    QTest::newRow("empty") << "" << false;
    QTest::newRow("absolute") << "/etc/passwd" << false;
    QTest::newRow("dotdot") << ".." << false;
    QTest::newRow("dotdot-mid") << "a/../b" << false;
    QTest::newRow("dotdot-end") << "a/.." << false;
    QTest::newRow("dot") << "a/./b" << false;
    QTest::newRow("backslash") << "a\\b" << false;
    QTest::newRow("double-slash") << "a//b" << false;
    QTest::newRow("trailing-slash") << "a/" << false;
}

void TestCatalog::safeRelativePath()
{
    QFETCH(QString, path);
    QFETCH(bool, safe);
    QCOMPARE(isSafeRelativePath(path), safe);
}

void TestCatalog::textFallback()
{
    LocalizedText t;
    t.values = {{QStringLiteral("zh_CN"), QStringLiteral("中文")},
                {QStringLiteral("en_US"), QStringLiteral("English")}};
    QCOMPARE(t.pick(QStringLiteral("zh_CN")), QStringLiteral("中文"));
    QCOMPARE(t.pick(QStringLiteral("en_US")), QStringLiteral("English"));
    // Same language, different region: zh_TW -> zh_CN.
    QCOMPARE(t.pick(QStringLiteral("zh_TW")), QStringLiteral("中文"));
    // Other language: en_US.
    QCOMPARE(t.pick(QStringLiteral("fr_FR")), QStringLiteral("English"));
    QCOMPARE(t.pick(QStringLiteral("C")), QStringLiteral("English"));
    QCOMPARE(t.pick(QString()), QStringLiteral("English"));

    // No en_US: first (smallest key) entry.
    LocalizedText only;
    only.values = {{QStringLiteral("ja_JP"), QStringLiteral("日本語")},
                   {QStringLiteral("de_DE"), QStringLiteral("Deutsch")}};
    QCOMPARE(only.pick(QStringLiteral("fr_FR")), QStringLiteral("Deutsch"));
    QCOMPARE(only.pick(QStringLiteral("ja_JP")), QStringLiteral("日本語"));

    // Same language prefers the same-language entry over en_US.
    LocalizedText mix;
    mix.values = {{QStringLiteral("en_US"), QStringLiteral("English")},
                  {QStringLiteral("en_GB"), QStringLiteral("British")}};
    QCOMPARE(mix.pick(QStringLiteral("en_AU")), QStringLiteral("British"));

    LocalizedText empty;
    QVERIFY(empty.isEmpty());
    QCOMPARE(empty.pick(QStringLiteral("en_US")), QString());
}

void TestCatalog::paragraphFallback()
{
    LocalizedParagraphs p;
    p.values = {{QStringLiteral("zh_CN"), QStringList{QStringLiteral("一"), QStringLiteral("二")}},
                {QStringLiteral("en_US"), QStringList{QStringLiteral("one")}}};
    QCOMPARE(p.pick(QStringLiteral("zh_HK")).size(), 2);
    QCOMPARE(p.pick(QStringLiteral("es_ES")), QStringList{QStringLiteral("one")});
    QVERIFY(LocalizedParagraphs().pick(QStringLiteral("en_US")).isEmpty());
}

void TestCatalog::screenshotLanguage_data()
{
    QTest::addColumn<QString>("locale");
    QTest::addColumn<QString>("expected");
    QTest::newRow("zh_CN") << "zh_CN" << "zh_CN";
    QTest::newRow("zh_TW") << "zh_TW" << "zh_CN";
    QTest::newRow("zh_HK") << "zh_HK" << "zh_CN";
    QTest::newRow("en_US") << "en_US" << "en_US";
    QTest::newRow("de_DE") << "de_DE" << "en_US";
    QTest::newRow("C") << "C" << "en_US";
}

void TestCatalog::screenshotLanguage()
{
    QFETCH(QString, locale);
    QFETCH(QString, expected);
    QCOMPARE(screenshotLocale(locale), expected);

    const auto d = parseAppDetail(readData(QStringLiteral("catalog/apps/gimp.json")), nullptr);
    QVERIFY(d.has_value());
    const QList<Screenshot> got = screenshotsFor(*d, locale);
    const QList<Screenshot> want = d->screenshots.value(expected);
    QVERIFY(!want.isEmpty());
    QCOMPARE(got.size(), want.size());
    QCOMPARE(got.at(0).image, want.at(0).image);
    const QString other = expected == QLatin1String("zh_CN") ? QStringLiteral("en_US") : QStringLiteral("zh_CN");
    QVERIFY(got.at(0).image != d->screenshots.value(other).at(0).image);
}

void TestCatalog::screenshotsFallBackToOtherSet()
{
    AppDetail d;
    d.screenshots.insert(QStringLiteral("en_US"), {Screenshot{QStringLiteral("assets/e.png"), {}, 1, 1}});
    QCOMPARE(screenshotsFor(d, QStringLiteral("zh_CN")).size(), 1);
    QCOMPARE(screenshotsFor(d, QStringLiteral("zh_CN")).at(0).image, QStringLiteral("assets/e.png"));

    AppDetail z;
    z.screenshots.insert(QStringLiteral("zh_CN"), {Screenshot{QStringLiteral("assets/z.png"), {}, 1, 1}});
    QCOMPARE(screenshotsFor(z, QStringLiteral("en_US")).at(0).image, QStringLiteral("assets/z.png"));
    QVERIFY(screenshotsFor(AppDetail(), QStringLiteral("en_US")).isEmpty());
}

void TestCatalog::statsParsing()
{
    const auto stats = parseStats(readData(QStringLiteral("catalog/stats.json")));
    QVERIFY(stats.size() >= 16);
    QVERIFY(stats.value(QStringLiteral("wechat")) > stats.value(QStringLiteral("hex-peek")));

    QVERIFY(parseStats(QByteArray("garbage")).isEmpty());
    QVERIFY(parseStats(QByteArray("{}")).isEmpty());
    QVERIFY(parseStats(QByteArray("{\"downloads\":{\"a\":\"many\"}}")).isEmpty());
    QCOMPARE(parseStats(QByteArray("{\"downloads\":{\"a\":5}}")).value(QStringLiteral("a")), qint64(5));
}

QTEST_APPLESS_MAIN(TestCatalog)
#include "test_catalog.moc"
