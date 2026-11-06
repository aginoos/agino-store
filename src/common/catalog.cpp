// SPDX-License-Identifier: GPL-3.0-or-later
#include "catalog.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QRegularExpression>
#include <QSet>
#include <QUrl>
#include <algorithm>
#include <cmath>
#include <utility>

namespace AginoStore {

namespace {

constexpr int kMaxDefaultFileSize = 4096;

void setError(QString *error, const QString &text)
{
    if (error)
        *error = text;
}

QString langOf(const QString &locale)
{
    const int i = locale.indexOf(QLatin1Char('_'));
    return i < 0 ? locale : locale.left(i);
}

// Locale fallback of docs/design.md 4.1: exact, same language, en_US, first (by key order).
template <typename T>
const T *pickValue(const QHash<QString, T> &values, const QString &locale)
{
    if (values.isEmpty())
        return nullptr;
    auto it = values.constFind(locale);
    if (it != values.constEnd())
        return &it.value();

    QStringList keys = values.keys();
    std::sort(keys.begin(), keys.end());
    const QString lang = langOf(locale);
    for (const QString &k : keys) {
        if (langOf(k) == lang)
            return &values.constFind(k).value();
    }
    it = values.constFind(QStringLiteral("en_US"));
    if (it != values.constEnd())
        return &it.value();
    return &values.constFind(keys.first()).value();
}

bool isHex64(const QString &s)
{
    static const QRegularExpression re(QStringLiteral("\\A[0-9a-f]{64}\\z"));
    return re.match(s).hasMatch();
}

bool isValidId(const QString &id)
{
    static const QRegularExpression re(QStringLiteral("\\A[a-z0-9][a-z0-9._+-]*\\z"));
    return re.match(id).hasMatch();
}

bool readString(const QJsonObject &o, const char *key, QString *out)
{
    const QJsonValue v = o.value(QLatin1String(key));
    if (!v.isString())
        return false;
    *out = v.toString();
    return true;
}

bool readInt(const QJsonValue &v, qint64 *out)
{
    if (!v.isDouble())
        return false;
    const double d = v.toDouble();
    if (!std::isfinite(d) || d < 0 || d > 9007199254740992.0 || d != std::floor(d))
        return false;
    *out = static_cast<qint64>(d);
    return true;
}

bool readLocalizedText(const QJsonValue &v, LocalizedText *out)
{
    if (!v.isObject())
        return false;
    const QJsonObject o = v.toObject();
    for (auto it = o.begin(); it != o.end(); ++it) {
        if (!it.value().isString())
            return false;
        out->values.insert(it.key(), it.value().toString());
    }
    return true;
}

bool readParagraphs(const QJsonValue &v, LocalizedParagraphs *out)
{
    if (!v.isObject())
        return false;
    const QJsonObject o = v.toObject();
    for (auto it = o.begin(); it != o.end(); ++it) {
        if (!it.value().isArray())
            return false;
        QStringList list;
        const QJsonArray arr = it.value().toArray();
        for (const QJsonValue &p : arr) {
            if (!p.isString())
                return false;
            list.append(p.toString());
        }
        out->values.insert(it.key(), list);
    }
    return true;
}

bool readStringList(const QJsonValue &v, QStringList *out)
{
    if (!v.isArray())
        return false;
    const QJsonArray arr = v.toArray();
    for (const QJsonValue &e : arr) {
        if (!e.isString())
            return false;
        out->append(e.toString());
    }
    return true;
}

std::optional<QJsonObject> parseObject(const QByteArray &json, QString *error)
{
    QJsonParseError perr;
    const QJsonDocument doc = QJsonDocument::fromJson(json, &perr);
    if (perr.error != QJsonParseError::NoError || !doc.isObject()) {
        setError(error, QStringLiteral("not a valid JSON object"));
        return std::nullopt;
    }
    return doc.object();
}

bool parseSource(const QString &s, Source *out)
{
    if (s == QLatin1String("system"))
        *out = Source::System;
    else if (s == QLatin1String("official"))
        *out = Source::Official;
    else if (s == QLatin1String("personal"))
        *out = Source::Personal;
    else
        return false;
    return true;
}

// The url is optional for vendor distribution (the store never downloads that deb).
bool parseDeb(const QJsonObject &o, bool urlRequired, DebInfo *deb, QString *error)
{
    const bool urlOk = urlRequired ? (readString(o, "url", &deb->url) && !deb->url.isEmpty())
                                   : (!o.contains(QStringLiteral("url")) || readString(o, "url", &deb->url));
    if (!readString(o, "version", &deb->version) || deb->version.isEmpty() || !urlOk
        || !readString(o, "sha256", &deb->sha256)) {
        setError(error, urlRequired ? QStringLiteral("deb needs version, url and sha256")
                                    : QStringLiteral("deb needs version and sha256"));
        return false;
    }
    if (!isHex64(deb->sha256)) {
        setError(error, QStringLiteral("deb sha256 must be 64 lower-case hex characters"));
        return false;
    }
    if (o.contains(QStringLiteral("vendor_url")) && !readString(o, "vendor_url", &deb->vendorUrl)) {
        setError(error, QStringLiteral("deb vendor_url must be a string"));
        return false;
    }
    if (o.contains(QStringLiteral("arch"))) {
        static const QRegularExpression archRe(QStringLiteral("\\A[a-z0-9][a-z0-9-]{0,15}\\z"));
        if (!readString(o, "arch", &deb->arch) || !archRe.match(deb->arch).hasMatch()) {
            setError(error, QStringLiteral("deb arch must be a short lower-case name such as amd64"));
            return false;
        }
    }
    if (!readInt(o.value(QStringLiteral("size")), &deb->size) || deb->size <= 0) {
        setError(error, QStringLiteral("deb size missing or invalid"));
        return false;
    }
    if (o.contains(QStringLiteral("installed_size"))
        && !readInt(o.value(QStringLiteral("installed_size")), &deb->installedSize)) {
        setError(error, QStringLiteral("deb installed_size invalid"));
        return false;
    }
    return true;
}

constexpr int kMaxL10nKeys = 8;
constexpr int kMaxL10nPackages = 4;

bool parseL10n(const QJsonValue &v, QHash<QString, QStringList> *out)
{
    if (!v.isObject())
        return false;
    const QJsonObject o = v.toObject();
    if (o.isEmpty() || o.size() > kMaxL10nKeys)
        return false;
    for (auto it = o.begin(); it != o.end(); ++it) {
        QStringList names;
        if (!isValidL10nKey(it.key()) || !readStringList(it.value(), &names) || names.isEmpty()
            || names.size() > kMaxL10nPackages)
            return false;
        for (const QString &name : names) {
            if (!isValidL10nPackage(name))
                return false;
        }
        out->insert(it.key(), names);
    }
    return true;
}

bool parseVendorDownload(const QJsonObject &o, const DebInfo &deb, PackageRef *pkg, QString *error)
{
    const QJsonValue download = o.value(QStringLiteral("download"));
    if (!download.isObject()) {
        setError(error, QStringLiteral("vendor package without download object"));
        return false;
    }
    const QJsonObject d = download.toObject();
    const QJsonValue direct = d.value(QStringLiteral("direct"));
    if (!readString(d, "page", &pkg->downloadPage) || !isHttpsUrl(pkg->downloadPage) || !direct.isBool()) {
        setError(error, QStringLiteral("vendor download needs an https page and a boolean direct"));
        return false;
    }
    pkg->downloadDirect = direct.toBool();
    if (pkg->downloadDirect && !deb.vendorUrl.isEmpty() && !isHttpsUrl(deb.vendorUrl)) {
        setError(error, QStringLiteral("deb vendor_url must be an https URL"));
        return false;
    }
    pkg->distribution = Distribution::Vendor;
    return true;
}

bool parsePackage(const QJsonValue &v, PackageRef *pkg, QString *error)
{
    if (!v.isObject()) {
        setError(error, QStringLiteral("package missing"));
        return false;
    }
    const QJsonObject o = v.toObject();
    QString kind;
    if (!readString(o, "kind", &kind) || !readString(o, "name", &pkg->name) || pkg->name.isEmpty()) {
        setError(error, QStringLiteral("package needs kind and name"));
        return false;
    }
    if (kind == QLatin1String("milkyway")) {
        pkg->kind = PackageRef::Kind::Milkyway;
        return true;
    }
    if (kind == QLatin1String("deb")) {
        pkg->kind = PackageRef::Kind::Deb;
        if (!o.value(QStringLiteral("deb")).isObject()) {
            setError(error, QStringLiteral("package kind deb without deb object"));
            return false;
        }
        // Anything but "vendor" (including no key) keeps the plain behaviour.
        const bool vendor = o.value(QStringLiteral("distribution")).toString() == QLatin1String("vendor");
        DebInfo deb;
        if (!parseDeb(o.value(QStringLiteral("deb")).toObject(), !vendor, &deb, error))
            return false;
        if (vendor && !parseVendorDownload(o, deb, pkg, error))
            return false;
        pkg->deb = deb;
        return true;
    }
    setError(error, QStringLiteral("unknown package kind: %1").arg(kind));
    return false;
}

bool parseApp(const QJsonObject &o, AppSummary *app, QString *error)
{
    if (!readString(o, "id", &app->id) || !isValidId(app->id)) {
        setError(error, QStringLiteral("app id missing or invalid"));
        return false;
    }
    const QString where = QStringLiteral("app %1: ").arg(app->id);
    if (!readLocalizedText(o.value(QStringLiteral("name")), &app->name) || app->name.isEmpty()
        || !readLocalizedText(o.value(QStringLiteral("summary")), &app->summary)) {
        setError(error, where + QStringLiteral("name/summary missing or invalid"));
        return false;
    }
    if (!readString(o, "icon", &app->icon) || !isSafeRelativePath(app->icon)) {
        setError(error, where + QStringLiteral("icon missing or unsafe path"));
        return false;
    }
    if (!readStringList(o.value(QStringLiteral("categories")), &app->categories)) {
        setError(error, where + QStringLiteral("categories missing or invalid"));
        return false;
    }
    QString source;
    if (!readString(o, "source", &source) || !parseSource(source, &app->source)) {
        setError(error, where + QStringLiteral("source missing or unknown"));
        return false;
    }
    if (!readString(o, "license", &app->license)) {
        setError(error, where + QStringLiteral("license missing"));
        return false;
    }
    if (!o.value(QStringLiteral("proprietary")).isBool()) {
        setError(error, where + QStringLiteral("proprietary missing"));
        return false;
    }
    app->proprietary = o.value(QStringLiteral("proprietary")).toBool();
    if (o.contains(QStringLiteral("launchable"))) {
        if (!readString(o, "launchable", &app->launchable) || !isValidDesktopId(app->launchable)) {
            setError(error, where + QStringLiteral("launchable is not a desktop file id"));
            return false;
        }
    }
    QString updated;
    if (!readString(o, "updated", &updated)) {
        setError(error, where + QStringLiteral("updated missing"));
        return false;
    }
    app->updated = QDate::fromString(updated, Qt::ISODate);
    if (!app->updated.isValid()) {
        setError(error, where + QStringLiteral("updated is not a date"));
        return false;
    }
    QString pkgError;
    if (!parsePackage(o.value(QStringLiteral("package")), &app->package, &pkgError)) {
        setError(error, where + pkgError);
        return false;
    }
    if (o.contains(QStringLiteral("l10n"))
        && (!parseL10n(o.value(QStringLiteral("l10n")), &app->l10n)
            || app->package.kind != PackageRef::Kind::Milkyway)) {
        setError(error, where + QStringLiteral("l10n is invalid"));
        return false;
    }
    if (!readString(o, "detail", &app->detail) || !isSafeRelativePath(app->detail)) {
        setError(error, where + QStringLiteral("detail missing or unsafe path"));
        return false;
    }
    return true;
}

bool parseScreenshots(const QJsonValue &v, QHash<QString, QList<Screenshot>> *out)
{
    if (!v.isObject())
        return false;
    const QJsonObject o = v.toObject();
    for (auto it = o.begin(); it != o.end(); ++it) {
        if (!it.value().isArray())
            return false;
        QList<Screenshot> list;
        const QJsonArray arr = it.value().toArray();
        for (const QJsonValue &sv : arr) {
            if (!sv.isObject())
                return false;
            const QJsonObject so = sv.toObject();
            Screenshot s;
            if (!readString(so, "image", &s.image) || !isSafeRelativePath(s.image))
                return false;
            if (so.contains(QStringLiteral("thumb"))) {
                if (!readString(so, "thumb", &s.thumb) || !isSafeRelativePath(s.thumb))
                    return false;
            }
            qint64 w = 0;
            qint64 h = 0;
            if (so.contains(QStringLiteral("width"))) {
                if (!readInt(so.value(QStringLiteral("width")), &w))
                    return false;
            }
            if (so.contains(QStringLiteral("height"))) {
                if (!readInt(so.value(QStringLiteral("height")), &h))
                    return false;
            }
            s.width = static_cast<int>(w);
            s.height = static_cast<int>(h);
            list.append(s);
        }
        out->insert(it.key(), list);
    }
    return true;
}

bool parseInstallProfile(const QJsonValue &v, InstallProfile *out, QString *error)
{
    if (!v.isObject()) {
        setError(error, QStringLiteral("install_profile is not an object"));
        return false;
    }
    const QJsonObject o = v.toObject();
    if (o.contains(QStringLiteral("debconf"))) {
        if (!readStringList(o.value(QStringLiteral("debconf")), &out->debconf)) {
            setError(error, QStringLiteral("install_profile.debconf must be a string array"));
            return false;
        }
        for (const QString &line : std::as_const(out->debconf)) {
            if (!isValidDebconfLine(line)) {
                setError(error, QStringLiteral("invalid debconf line: %1").arg(line));
                return false;
            }
        }
    }
    if (o.contains(QStringLiteral("default_files"))) {
        if (!o.value(QStringLiteral("default_files")).isArray()) {
            setError(error, QStringLiteral("install_profile.default_files must be an array"));
            return false;
        }
        const QJsonArray arr = o.value(QStringLiteral("default_files")).toArray();
        for (const QJsonValue &fv : arr) {
            QString content;
            DefaultFile f;
            if (!fv.isObject() || !readString(fv.toObject(), "path", &f.path)
                || !readString(fv.toObject(), "content", &content)) {
                setError(error, QStringLiteral("default_files entry needs path and content"));
                return false;
            }
            f.content = content.toUtf8();
            if (!isValidDefaultFile(f)) {
                setError(error, QStringLiteral("invalid default file: %1").arg(f.path));
                return false;
            }
            out->defaultFiles.append(f);
        }
    }
    return true;
}

} // namespace

QString LocalizedText::pick(const QString &locale) const
{
    const QString *s = pickValue(values, locale);
    return s ? *s : QString();
}

QStringList LocalizedParagraphs::pick(const QString &locale) const
{
    const QStringList *l = pickValue(values, locale);
    return l ? *l : QStringList();
}

const AppSummary *Catalog::app(const QString &id) const
{
    for (const AppSummary &a : apps) {
        if (a.id == id)
            return &a;
    }
    return nullptr;
}

bool isValidDesktopId(const QString &id)
{
    static const QRegularExpression re(QStringLiteral("\\A[A-Za-z0-9][A-Za-z0-9._-]{0,200}\\.desktop\\z"));
    return re.match(id).hasMatch() && !id.contains(QStringLiteral(".."));
}

bool isSafeRelativePath(const QString &path)
{
    if (path.isEmpty() || path.startsWith(QLatin1Char('/')) || path.contains(QLatin1Char('\\'))
        || path.contains(QChar(0)))
        return false;
    const QStringList parts = path.split(QLatin1Char('/'));
    for (const QString &p : parts) {
        if (p.isEmpty() || p == QLatin1String(".") || p == QLatin1String(".."))
            return false;
    }
    return true;
}

bool isValidDebconfLine(const QString &line)
{
    for (const QChar c : line) {
        if (c.unicode() < 0x20 || c.unicode() == 0x7f)
            return false;
    }
    // "<owner> <question> <type> <value>"; the value may be empty or contain spaces.
    const QStringList parts = line.split(QLatin1Char(' '), Qt::KeepEmptyParts);
    if (parts.size() < 3 || parts[0].isEmpty() || parts[1].isEmpty() || parts[2].isEmpty())
        return false;
    const QString &type = parts[2];
    return type == QLatin1String("boolean") || type == QLatin1String("string")
        || type == QLatin1String("select");
}

bool isValidDefaultFile(const DefaultFile &file)
{
    static const QRegularExpression re(QStringLiteral("\\A/etc/default/[a-z0-9][a-z0-9.+-]*\\z"));
    return re.match(file.path).hasMatch() && file.content.size() <= kMaxDefaultFileSize
        && !file.content.contains('\0');
}

std::optional<Catalog> parseCatalog(const QByteArray &json, QString *error)
{
    const std::optional<QJsonObject> root = parseObject(json, error);
    if (!root)
        return std::nullopt;

    Catalog cat;
    qint64 schema = 0;
    if (!readInt(root->value(QStringLiteral("schema")), &schema) || schema != 1) {
        setError(error, QStringLiteral("unsupported catalog schema"));
        return std::nullopt;
    }
    cat.schema = static_cast<int>(schema);

    if (!root->value(QStringLiteral("categories")).isArray()
        || !root->value(QStringLiteral("apps")).isArray()) {
        setError(error, QStringLiteral("catalog needs categories and apps arrays"));
        return std::nullopt;
    }

    QSet<QString> categoryIds;
    const QJsonArray cats = root->value(QStringLiteral("categories")).toArray();
    for (const QJsonValue &cv : cats) {
        const QJsonObject co = cv.toObject();
        Category c;
        qint64 order = 0;
        if (!cv.isObject() || !readString(co, "id", &c.id) || !isValidId(c.id)
            || !readLocalizedText(co.value(QStringLiteral("name")), &c.name) || c.name.isEmpty()
            || !readInt(co.value(QStringLiteral("order")), &order)) {
            setError(error, QStringLiteral("invalid category entry"));
            return std::nullopt;
        }
        if (categoryIds.contains(c.id)) {
            setError(error, QStringLiteral("duplicate category: %1").arg(c.id));
            return std::nullopt;
        }
        categoryIds.insert(c.id);
        c.order = static_cast<int>(order);
        cat.categories.append(c);
    }

    // "featured" is optional.
    if (root->contains(QStringLiteral("featured"))) {
        if (!root->value(QStringLiteral("featured")).isArray()) {
            setError(error, QStringLiteral("featured must be an array"));
            return std::nullopt;
        }
        const QJsonArray feats = root->value(QStringLiteral("featured")).toArray();
        for (const QJsonValue &fv : feats) {
            const QJsonObject fo = fv.toObject();
            Featured f;
            if (!fv.isObject() || !readString(fo, "id", &f.id) || f.id.isEmpty()
                || !readLocalizedText(fo.value(QStringLiteral("title")), &f.title)
                || !readStringList(fo.value(QStringLiteral("apps")), &f.apps)) {
                setError(error, QStringLiteral("invalid featured entry"));
                return std::nullopt;
            }
            if (fo.contains(QStringLiteral("subtitle"))
                && !readLocalizedText(fo.value(QStringLiteral("subtitle")), &f.subtitle)) {
                setError(error, QStringLiteral("invalid featured subtitle"));
                return std::nullopt;
            }
            if (fo.contains(QStringLiteral("banner"))) {
                if (!fo.value(QStringLiteral("banner")).isBool()) {
                    setError(error, QStringLiteral("featured banner must be a boolean"));
                    return std::nullopt;
                }
                f.banner = fo.value(QStringLiteral("banner")).toBool();
            }
            cat.featured.append(f);
        }
    }

    QSet<QString> appIds;
    const QJsonArray apps = root->value(QStringLiteral("apps")).toArray();
    for (const QJsonValue &av : apps) {
        if (!av.isObject()) {
            setError(error, QStringLiteral("app entry is not an object"));
            return std::nullopt;
        }
        AppSummary app;
        if (!parseApp(av.toObject(), &app, error))
            return std::nullopt;
        if (appIds.contains(app.id)) {
            setError(error, QStringLiteral("duplicate app id: %1").arg(app.id));
            return std::nullopt;
        }
        for (const QString &c : std::as_const(app.categories)) {
            if (!categoryIds.contains(c)) {
                setError(error, QStringLiteral("app %1: unknown category %2").arg(app.id, c));
                return std::nullopt;
            }
        }
        appIds.insert(app.id);
        cat.apps.append(app);
    }

    for (const Featured &f : std::as_const(cat.featured)) {
        for (const QString &id : f.apps) {
            if (!appIds.contains(id)) {
                setError(error, QStringLiteral("featured %1: unknown app %2").arg(f.id, id));
                return std::nullopt;
            }
        }
    }
    return cat;
}

std::optional<AppDetail> parseAppDetail(const QByteArray &json, QString *error)
{
    const std::optional<QJsonObject> root = parseObject(json, error);
    if (!root)
        return std::nullopt;

    AppDetail d;
    if (!readString(*root, "id", &d.id) || !isValidId(d.id)) {
        setError(error, QStringLiteral("detail id missing or invalid"));
        return std::nullopt;
    }
    if (!readParagraphs(root->value(QStringLiteral("description")), &d.description)) {
        setError(error, QStringLiteral("detail description missing or invalid"));
        return std::nullopt;
    }
    if (root->contains(QStringLiteral("developer"))
        && !readLocalizedText(root->value(QStringLiteral("developer")), &d.developer)) {
        setError(error, QStringLiteral("detail developer invalid"));
        return std::nullopt;
    }
    const QJsonValue homepage = root->value(QStringLiteral("homepage"));
    if (!homepage.isUndefined() && !homepage.isNull()) {
        if (!homepage.isString()) {
            setError(error, QStringLiteral("detail homepage invalid"));
            return std::nullopt;
        }
        d.homepage = homepage.toString();
    }
    const QJsonValue maintainer = root->value(QStringLiteral("maintainer"));
    if (!maintainer.isUndefined() && !maintainer.isNull()) {
        if (!maintainer.isString()) {
            setError(error, QStringLiteral("detail maintainer invalid"));
            return std::nullopt;
        }
        d.maintainer = maintainer.toString();
    }
    if (root->contains(QStringLiteral("screenshots"))
        && !parseScreenshots(root->value(QStringLiteral("screenshots")), &d.screenshots)) {
        setError(error, QStringLiteral("detail screenshots invalid"));
        return std::nullopt;
    }
    if (root->contains(QStringLiteral("releases"))) {
        if (!root->value(QStringLiteral("releases")).isArray()) {
            setError(error, QStringLiteral("detail releases invalid"));
            return std::nullopt;
        }
        const QJsonArray rels = root->value(QStringLiteral("releases")).toArray();
        for (const QJsonValue &rv : rels) {
            const QJsonObject ro = rv.toObject();
            Release r;
            QString date;
            if (!rv.isObject() || !readString(ro, "version", &r.version) || !readString(ro, "date", &date)
                || !readParagraphs(ro.value(QStringLiteral("notes")), &r.notes)) {
                setError(error, QStringLiteral("invalid release entry"));
                return std::nullopt;
            }
            r.date = QDate::fromString(date, Qt::ISODate);
            if (!r.date.isValid()) {
                setError(error, QStringLiteral("release date is not a date"));
                return std::nullopt;
            }
            d.releases.append(r);
        }
    }
    const QJsonValue profile = root->value(QStringLiteral("install_profile"));
    if (!profile.isUndefined() && !profile.isNull()) {
        if (!parseInstallProfile(profile, &d.installProfile, error))
            return std::nullopt;
    }
    return d;
}

QHash<QString, qint64> parseStats(const QByteArray &json)
{
    QHash<QString, qint64> stats;
    const std::optional<QJsonObject> root = parseObject(json, nullptr);
    if (!root || !root->value(QStringLiteral("downloads")).isObject())
        return stats;
    const QJsonObject dl = root->value(QStringLiteral("downloads")).toObject();
    QHash<QString, qint64> parsed;
    for (auto it = dl.begin(); it != dl.end(); ++it) {
        qint64 n = 0;
        if (!readInt(it.value(), &n))
            return stats;
        parsed.insert(it.key(), n);
    }
    return parsed;
}

QString screenshotLocale(const QString &uiLocale)
{
    return uiLocale.startsWith(QLatin1String("zh")) ? QStringLiteral("zh_CN") : QStringLiteral("en_US");
}

QList<Screenshot> screenshotsFor(const AppDetail &detail, const QString &uiLocale)
{
    const QString primary = screenshotLocale(uiLocale);
    const QString other = primary == QLatin1String("zh_CN") ? QStringLiteral("en_US")
                                                            : QStringLiteral("zh_CN");
    const QList<Screenshot> first = detail.screenshots.value(primary);
    return first.isEmpty() ? detail.screenshots.value(other) : first;
}

bool isValidL10nKey(const QString &key)
{
    static const QRegularExpression re(QStringLiteral("\\A[a-z]{2,3}(_[A-Z]{2})?\\z"));
    return re.match(key).hasMatch();
}

bool isValidL10nPackage(const QString &name)
{
    static const QRegularExpression re(QStringLiteral("\\A[a-z0-9][a-z0-9+.-]+\\z"));
    return name.size() <= 128 && re.match(name).hasMatch();
}

QString pickL10nKey(const QHash<QString, QStringList> &l10n, const QString &locale)
{
    if (l10n.isEmpty() || !isValidL10nKey(locale))
        return QString();
    if (l10n.contains(locale))
        return locale;
    const QString lang = langOf(locale);
    if (l10n.contains(lang))
        return lang;
    QStringList keys = l10n.keys();
    std::sort(keys.begin(), keys.end());
    for (const QString &k : std::as_const(keys)) {
        if (k.startsWith(lang + QLatin1Char('_')))
            return k;
    }
    return QString();
}

QStringList l10nPackagesFor(const QHash<QString, QStringList> &l10n, const QString &locale)
{
    const QString key = pickL10nKey(l10n, locale);
    return key.isEmpty() ? QStringList() : l10n.value(key);
}

QStringList allL10nPackages(const QHash<QString, QStringList> &l10n)
{
    QSet<QString> seen;
    for (const QStringList &names : l10n)
        seen.unite(QSet<QString>(names.begin(), names.end()));
    QStringList out(seen.begin(), seen.end());
    std::sort(out.begin(), out.end());
    return out;
}

QString PackageRef::downloadUrl() const
{
    if (distribution != Distribution::Vendor)
        return QString();
    if (downloadDirect && deb && !deb->vendorUrl.isEmpty())
        return deb->vendorUrl;
    return downloadPage;
}

QString distributionToString(Distribution distribution)
{
    return distribution == Distribution::Vendor ? QStringLiteral("vendor") : QStringLiteral("store");
}

bool isHttpsUrl(const QString &text)
{
    const QUrl url(text, QUrl::StrictMode);
    return url.isValid() && url.scheme() == QLatin1String("https") && !url.host().isEmpty();
}

QString sourceToString(Source source)
{
    switch (source) {
    case Source::System:
        return QStringLiteral("system");
    case Source::Official:
        return QStringLiteral("official");
    case Source::Personal:
        return QStringLiteral("personal");
    }
    return QStringLiteral("system");
}

} // namespace AginoStore
