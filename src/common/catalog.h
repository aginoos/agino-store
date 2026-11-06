// SPDX-License-Identifier: GPL-3.0-or-later
// Catalog data model and parsing (docs/design.md section 4). Shared by the client and the helper.
#pragma once

#include <QDate>
#include <QDateTime>
#include <QHash>
#include <QList>
#include <QString>
#include <QStringList>
#include <optional>

namespace AginoStore {

// Text keyed by locale ("zh_CN", "en_US", ...). pick() applies the fallback of section 4.1.
struct LocalizedText {
    QHash<QString, QString> values;
    QString pick(const QString &locale) const;
    bool isEmpty() const { return values.isEmpty(); }
};

// Paragraph lists keyed by locale (descriptions, release notes).
struct LocalizedParagraphs {
    QHash<QString, QStringList> values;
    QStringList pick(const QString &locale) const;
};

enum class Source { System, Official, Personal };

struct DebInfo {
    QString version;
    QString url;
    QString vendorUrl;
    QString sha256;          // lower-case hex, 64 chars
    qint64 size = 0;
    qint64 installedSize = 0;
    QString arch;            // only for vendor-distributed apps: the architecture of the deb the store tested
};

// How the app's deb reaches the user: through the store (the helper downloads it) or from the
// vendor's website (the licence does not allow the store to distribute it; docs/design.md 4.4).
enum class Distribution { Store, Vendor };

struct PackageRef {
    enum class Kind { Milkyway, Deb };
    Kind kind = Kind::Milkyway;
    QString name;
    std::optional<DebInfo> deb;   // set when kind == Deb
    Distribution distribution = Distribution::Store;
    // Vendor distribution only (https URLs). downloadPage is the vendor's download page;
    // downloadDirect means deb->vendorUrl is the real address of the file for this version.
    QString downloadPage;
    bool downloadDirect = false;

    bool isVendorDistributed() const { return distribution == Distribution::Vendor; }
    // Where "download from the vendor" leads: deb->vendorUrl when downloadDirect and it is set,
    // otherwise downloadPage. Empty for store distribution.
    QString downloadUrl() const;
};

struct Category {
    QString id;
    LocalizedText name;
    int order = 0;
};

struct Featured {
    QString id;
    LocalizedText title;
    LocalizedText subtitle;
    QStringList apps;
    bool banner = false;
};

struct AppSummary {
    QString id;
    LocalizedText name;
    LocalizedText summary;
    QString icon;            // path relative to the catalog root
    QStringList categories;
    Source source = Source::System;
    QString license;
    bool proprietary = false;
    QString launchable;      // desktop file id (AppStream launchable), optional
    QDate updated;
    PackageRef package;
    QString detail;          // path relative to the catalog root
    // Language packages of a milkyway app: locale key ("zh_CN", "fr") -> Debian package names.
    QHash<QString, QStringList> l10n;
};

struct Catalog {
    int schema = 0;
    QList<Category> categories;
    QList<Featured> featured;
    QList<AppSummary> apps;
    const AppSummary *app(const QString &id) const;
};

struct Screenshot {
    QString image;
    QString thumb;
    int width = 0;
    int height = 0;
};

struct Release {
    QString version;
    QDate date;
    LocalizedParagraphs notes;
};

struct DefaultFile {
    QString path;            // must match ^/etc/default/[a-z0-9][a-z0-9.+-]*$
    QByteArray content;      // at most 4 KiB
};

struct InstallProfile {
    QStringList debconf;     // debconf-set-selections lines, validated
    QList<DefaultFile> defaultFiles;
    bool isEmpty() const { return debconf.isEmpty() && defaultFiles.isEmpty(); }
};

struct AppDetail {
    QString id;
    LocalizedParagraphs description;
    LocalizedText developer;
    QString homepage;
    QString maintainer;      // required when the app's source is Personal
    QHash<QString, QList<Screenshot>> screenshots;   // "zh_CN" / "en_US"
    QList<Release> releases;
    InstallProfile installProfile;
};

// Parsers return std::nullopt and set *error on invalid input; they never throw.
std::optional<Catalog> parseCatalog(const QByteArray &json, QString *error);
std::optional<AppDetail> parseAppDetail(const QByteArray &json, QString *error);
// Popularity from stats.json: app id -> downloads. Empty on invalid input.
QHash<QString, qint64> parseStats(const QByteArray &json);

// Validation helpers, also used by the helper before acting.
bool isSafeRelativePath(const QString &path);
// A desktop file id such as "org.keepassxc.KeePassXC.desktop" (no path, no "..").
bool isValidDesktopId(const QString &id);
bool isValidDebconfLine(const QString &line);
bool isValidDefaultFile(const DefaultFile &file);

// "zh_CN" for any locale starting with "zh", otherwise "en_US" (section 4.1).
QString screenshotLocale(const QString &uiLocale);
// Screenshots for the UI locale, falling back to the other set when empty.
QList<Screenshot> screenshotsFor(const AppDetail &detail, const QString &uiLocale);

// l10n validation: key like "zh_CN" / "fr"; package names like Debian binary packages.
bool isValidL10nKey(const QString &key);
bool isValidL10nPackage(const QString &name);
// The l10n key serving the system locale name (e.g. "zh_CN"): exact key, else the bare language
// key ("zh"), else the first (sorted) key with the same language prefix ("zh_" ...); empty if none.
QString pickL10nKey(const QHash<QString, QStringList> &l10n, const QString &locale);
// Packages of pickL10nKey(), empty when no key serves the locale.
QStringList l10nPackagesFor(const QHash<QString, QStringList> &l10n, const QString &locale);
// Every package of every language (sorted, unique): removal has to take all of them.
QStringList allL10nPackages(const QHash<QString, QStringList> &l10n);

QString sourceToString(Source source);
QString distributionToString(Distribution distribution);   // "store" | "vendor"

// True for an absolute https URL with a host (the only kind the store opens or accepts).
bool isHttpsUrl(const QString &url);

} // namespace AginoStore
