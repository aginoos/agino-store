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
};

struct PackageRef {
    enum class Kind { Milkyway, Deb };
    Kind kind = Kind::Milkyway;
    QString name;
    std::optional<DebInfo> deb;   // set when kind == Deb
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
    QDate updated;
    PackageRef package;
    QString detail;          // path relative to the catalog root
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
bool isValidDebconfLine(const QString &line);
bool isValidDefaultFile(const DefaultFile &file);

// "zh_CN" for any locale starting with "zh", otherwise "en_US" (section 4.1).
QString screenshotLocale(const QString &uiLocale);
// Screenshots for the UI locale, falling back to the other set when empty.
QList<Screenshot> screenshotsFor(const AppDetail &detail, const QString &uiLocale);

QString sourceToString(Source source);

} // namespace AginoStore
