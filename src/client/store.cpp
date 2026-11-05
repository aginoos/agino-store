// SPDX-License-Identifier: GPL-3.0-or-later
#include "store.h"

#include "desktopicons.h"

#include <QCoreApplication>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QJSEngine>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocale>
#include <QUrl>
#include <algorithm>

using namespace AginoStore;

Store *Store::instance()
{
    static Store *self = new Store(qApp);
    return self;
}

Store *Store::create(QQmlEngine *, QJSEngine *)
{
    Store *self = instance();
    QJSEngine::setObjectOwnership(self, QJSEngine::CppOwnership);
    return self;
}

Store::Store(QObject *parent)
    : QObject(parent)
    , m_config(StoreConfig::load())
    , m_cache(StoreConfig::catalogDir(), m_config.keyDirs)
    , m_assets(QUrl(m_config.catalogUrl), m_config.allowInsecureUrls, this)
    , m_helper(this)
    , m_tasks(this)
    , m_locale(QLocale::system().name())
{
    connect(&m_assets, &AssetCache::ready, this, [this]() {
        ++m_assetTick;
        emit assetTickChanged();
    });
    connect(&m_helper, &HelperClient::stateReceived, this, &Store::applyState);
    connect(&m_helper, &HelperClient::stateChanged, &m_helper, &HelperClient::getState);
    connect(&m_helper, &HelperClient::catalogChanged, this, [this]() { reloadCatalog(); });
    connect(&m_helper, &HelperClient::refreshed, this, [this]() {
        m_refreshing = false;
        emit refreshingChanged();
        reloadCatalog();
        m_helper.getState();
        m_userRefresh = false;
    });
    connect(&m_helper, &HelperClient::refreshFailed, this, [this](const QString &code, const QString &message) {
        m_refreshing = false;
        emit refreshingChanged();
        // The cached catalog stays usable offline; only a manual refresh reports the failure.
        if (m_userRefresh)
            emit toast(friendlyError(code, message));
        m_userRefresh = false;
        m_helper.getState();
    });
    connect(&m_helper, &HelperClient::simulated, this, &Store::onSimulated);
    connect(&m_helper, &HelperClient::simulateFailed, this,
            [this](const QString &appId, const QString &action, const QString &code, const QString &message) {
                m_pending.removeAll(appId + QLatin1Char(':') + action);
                if (m_batch.active && m_batch.ids.contains(appId)) {
                    // Report the failure once; the other simulations of the batch end silently.
                    if (!m_batch.failed)
                        emit toast(friendlyError(code, message));
                    m_batch.failed = true;
                    if (--m_batch.remaining <= 0)
                        m_batch = Batch();
                    return;
                }
                emit toast(friendlyError(code, message));
            });
    connect(&m_helper, &HelperClient::jobStarted, this, &Store::onJobStarted);
    connect(&m_helper, &HelperClient::jobStartFailed, this,
            [this](const QString &, const QString &, const QString &code, const QString &message) {
                emit toast(friendlyError(code, message));
            });
    connect(&m_helper, &HelperClient::jobProgress, this,
            [this](uint jobId, const QString &appId, const QString &stage, uint percent, qulonglong done,
                   qulonglong total) {
                const bool known = m_tasks.find(jobId) != nullptr;
                m_tasks.progress(jobId, appId, stage, percent, done, total);
                if (!known)
                    bumpState();
            });
    connect(&m_helper, &HelperClient::jobFinished, this,
            [this](uint jobId, const QString &appId, bool ok, const QString &code, const QString &message) {
                m_tasks.finished(jobId, appId, ok, code, message);
                maybeHandleFinished(jobId);
                bumpState();
            });

    m_syncTimer.setInterval(60 * 1000);
    connect(&m_syncTimer, &QTimer::timeout, this, &Store::syncTextChanged);
    m_syncTimer.start();

    // Show what is cached right away, then ask the helper for a fresh catalog and the state.
    reloadCatalog();
    m_helper.getState();
    refresh();
    m_userRefresh = false;
}

void Store::refresh()
{
    if (m_refreshing)
        return;
    m_refreshing = true;
    m_userRefresh = m_haveCatalog;   // startup refresh is silent
    emit refreshingChanged();
    m_helper.refresh();
}

void Store::reloadCatalog()
{
    CatalogCache::Loaded loaded = m_cache.load(m_loaded.manifest.serial);
    if (loaded.status == CatalogCache::Status::Unavailable) {
        // Keep showing a catalog that was good a moment ago (e.g. a rollback attempt is rejected).
        if (!m_haveCatalog) {
            m_state = QStringLiteral("unavailable");
            m_error = loaded.error;
            emit catalogReloaded();
        }
        return;
    }

    m_loaded = std::move(loaded);
    m_haveCatalog = true;
    m_state = m_loaded.status == CatalogCache::Status::Expired ? QStringLiteral("expired")
                                                                : QStringLiteral("ready");
    m_error.clear();
    m_details.clear();
    m_synced = m_cache.manifestModified();
    m_assets.setManifest(m_loaded.manifest);

    QList<Category> cats = m_loaded.catalog.categories;
    std::stable_sort(cats.begin(), cats.end(), [](const Category &a, const Category &b) { return a.order < b.order; });
    m_categories.clear();
    for (const Category &c : cats)
        m_categories.append(QVariantMap{{QStringLiteral("id"), c.id}, {QStringLiteral("name"), c.name.pick(m_locale)}});

    m_featured.clear();
    for (const Featured &f : m_loaded.catalog.featured) {
        QStringList apps;
        for (const QString &id : f.apps) {
            if (m_loaded.catalog.app(id))
                apps.append(id);
        }
        m_featured.append(QVariantMap{{QStringLiteral("id"), f.id},
                                      {QStringLiteral("title"), f.title.pick(m_locale)},
                                      {QStringLiteral("subtitle"), f.subtitle.pick(m_locale)},
                                      {QStringLiteral("apps"), apps},
                                      {QStringLiteral("banner"), f.banner}});
    }
    emit catalogReloaded();
    emit syncTextChanged();
    bumpState();
}

void Store::applyState(const QString &json)
{
    const QJsonObject root = QJsonDocument::fromJson(json.toUtf8()).object();
    m_installed.clear();
    const QJsonObject installed = root.value(QStringLiteral("installed")).toObject();
    for (auto it = installed.begin(); it != installed.end(); ++it) {
        const QJsonObject o = it.value().toObject();
        m_installed.insert(it.key(), {o.value(QStringLiteral("package")).toString(),
                                      o.value(QStringLiteral("version")).toString()});
    }
    m_updates.clear();
    const QJsonArray updates = root.value(QStringLiteral("updates")).toArray();
    for (const QJsonValue &v : updates) {
        const QJsonObject o = v.toObject();
        m_updates.insert(o.value(QStringLiteral("id")).toString(),
                         {o.value(QStringLiteral("installed")).toString(),
                          o.value(QStringLiteral("available")).toString()});
    }
    // TODO(verify): the "jobs" entries are not specified in detail; accept jobId/id, appId/app,
    // kind/action and stage when present so a second client's jobs show up too.
    const QJsonArray jobs = root.value(QStringLiteral("jobs")).toArray();
    for (const QJsonValue &v : jobs) {
        const QJsonObject o = v.toObject();
        const uint id = uint(o.contains(QStringLiteral("jobId")) ? o.value(QStringLiteral("jobId")).toInt()
                                                                 : o.value(QStringLiteral("id")).toInt());
        const QString appId = o.contains(QStringLiteral("appId")) ? o.value(QStringLiteral("appId")).toString()
                                                                  : o.value(QStringLiteral("app")).toString();
        if (id == 0 || appId.isEmpty() || m_tasks.find(id))
            continue;
        const QString kind = o.contains(QStringLiteral("kind")) ? o.value(QStringLiteral("kind")).toString()
                                                                : o.value(QStringLiteral("action")).toString();
        m_tasks.started(id, appId, kind);
        m_tasks.progress(id, appId, o.value(QStringLiteral("stage")).toString(QStringLiteral("waiting")),
                         uint(o.value(QStringLiteral("percent")).toInt()), 0, 0);
    }
    bumpState();
}

void Store::bumpState()
{
    ++m_stateTick;
    emit stateTickChanged();
}

QString Store::packageName(const QString &appId) const
{
    const AppSummary *a = m_loaded.catalog.app(appId);
    return a ? a->package.name : QString();
}

QString Store::launchable(const QString &appId) const
{
    const AppSummary *a = m_loaded.catalog.app(appId);
    return a ? a->launchable : QString();
}

QString Store::syncText() const
{
    if (!m_synced.isValid())
        return {};
    const qint64 minutes = m_synced.secsTo(QDateTime::currentDateTime()) / 60;
    if (minutes < 1)
        return tr("Updated just now");
    if (minutes < 60)
        return tr("Updated %1 min ago").arg(minutes);
    if (minutes < 60 * 24)
        return tr("Updated %1 h ago").arg(minutes / 60);
    return tr("Updated %1 d ago").arg(minutes / (60 * 24));
}

QVariantMap Store::app(const QString &id) const
{
    const AppSummary *a = m_loaded.catalog.app(id);
    if (!a)
        return {};
    const bool deb = a->package.kind == PackageRef::Kind::Deb && a->package.deb;
    QString version = deb ? a->package.deb->version : QString();
    const auto upd = m_updates.constFind(id);
    if (upd != m_updates.constEnd() && !upd->available.isEmpty())
        version = upd->available;
    if (version.isEmpty())
        version = installedVersion(id);
    QVariantMap map;
    map.insert(QStringLiteral("id"), a->id);
    map.insert(QStringLiteral("name"), a->name.pick(m_locale));
    map.insert(QStringLiteral("summary"), a->summary.pick(m_locale));
    map.insert(QStringLiteral("source"), sourceToString(a->source));
    map.insert(QStringLiteral("proprietary"), a->proprietary);
    map.insert(QStringLiteral("license"), a->license);
    map.insert(QStringLiteral("categories"), a->categories);
    map.insert(QStringLiteral("updated"), a->updated.toString(Qt::ISODate));
    map.insert(QStringLiteral("version"), version);
    map.insert(QStringLiteral("installedVersion"), installedVersion(id));
    map.insert(QStringLiteral("downloadSize"), deb ? a->package.deb->size : 0);
    map.insert(QStringLiteral("installedSize"), deb ? a->package.deb->installedSize : 0);
    map.insert(QStringLiteral("checksum"), deb ? a->package.deb->sha256 : QString());
    map.insert(QStringLiteral("package"), a->package.name);
    map.insert(QStringLiteral("distribution"), distributionToString(a->package.distribution));
    map.insert(QStringLiteral("downloadPage"), a->package.downloadPage);
    map.insert(QStringLiteral("downloadDirect"), a->package.downloadDirect);
    map.insert(QStringLiteral("downloadUrl"), a->package.downloadUrl());
    map.insert(QStringLiteral("state"), appState(id));
    return map;
}

QVariantMap Store::appDetail(const QString &id)
{
    const AppSummary *a = m_loaded.catalog.app(id);
    if (!a)
        return {};
    auto cached = m_details.constFind(id);
    if (cached == m_details.constEnd()) {
        QString error;
        const std::optional<AppDetail> parsed =
            parseAppDetail(m_cache.readVerified(m_loaded.manifest, a->detail), &error);
        if (!parsed)
            return {};
        cached = m_details.insert(id, *parsed);
    }
    const AppDetail &d = cached.value();

    QVariantList shots;
    for (const Screenshot &s : screenshotsFor(d, m_locale)) {
        shots.append(QVariantMap{{QStringLiteral("image"), s.image},
                                 {QStringLiteral("thumb"), s.thumb},
                                 {QStringLiteral("width"), s.width},
                                 {QStringLiteral("height"), s.height}});
    }
    QStringList notes;
    QString releaseVersion;
    if (!d.releases.isEmpty()) {
        notes = d.releases.first().notes.pick(m_locale);
        releaseVersion = d.releases.first().version;
    }
    QVariantMap map;
    map.insert(QStringLiteral("description"), d.description.pick(m_locale));
    map.insert(QStringLiteral("developer"), d.developer.pick(m_locale));
    map.insert(QStringLiteral("homepage"), d.homepage);
    map.insert(QStringLiteral("maintainer"), d.maintainer);
    map.insert(QStringLiteral("screenshots"), shots);
    map.insert(QStringLiteral("releaseNotes"), notes);
    map.insert(QStringLiteral("releaseVersion"), releaseVersion);
    return map;
}

QString Store::appState(const QString &id) const
{
    if (m_tasks.isBusy(id))
        return QStringLiteral("busy");
    if (m_updates.contains(id))
        return QStringLiteral("update");
    if (m_installed.contains(id))
        return QStringLiteral("installed");
    return QStringLiteral("none");
}

QString Store::installedVersion(const QString &id) const
{
    const auto upd = m_updates.constFind(id);
    if (upd != m_updates.constEnd() && !upd->installed.isEmpty())
        return upd->installed;
    return m_installed.value(id).version;
}

QString Store::sourceText(const QString &id)
{
    const AppSummary *a = m_loaded.catalog.app(id);
    if (!a)
        return {};
    switch (a->source) {
    case Source::System:
        return tr("agino system repository");
    case Source::Official:
        return tr("Official package from the developer, reviewed and mirrored by agino");
    case Source::Personal:
        return tr("Packaged by %1, reviewed and mirrored by agino").arg(appDetail(id).value(QStringLiteral("maintainer")).toString());
    }
    return {};
}

QString Store::iconUrl(const QString &id)
{
    const AppSummary *a = m_loaded.catalog.app(id);
    return a ? assetUrl(a->icon) : QString();
}

QString Store::assetUrl(const QString &relPath)
{
    const QString local = m_assets.localPath(relPath);
    return local.isEmpty() ? QString() : QUrl::fromLocalFile(local).toString();
}

QString Store::formatBytes(double bytes) const
{
    return QLocale::system().formattedDataSize(qint64(bytes), 1, QLocale::DataSizeSIFormat);
}

qint64 Store::updatesDownloadBytes() const
{
    qint64 total = 0;
    for (auto it = m_updates.constBegin(); it != m_updates.constEnd(); ++it) {
        const AppSummary *a = m_loaded.catalog.app(it.key());
        if (a && a->package.deb && !a->package.isVendorDistributed())
            total += a->package.deb->size;
    }
    return total;
}

void Store::clearAssetCache()
{
    m_assets.clear();
    ++m_assetTick;
    emit assetTickChanged();
}

void Store::launch(const QString &appId)
{
    DesktopIcons::instance()->launch(packageName(appId), launchable(appId));
}

void Store::openSystemUpdates()
{
    // TODO(verify): interface name and the "update" page id of the Control Center.
    QDBusMessage msg = QDBusMessage::createMethodCall(
        QStringLiteral("org.deepin.dde.ControlCenter1"), QStringLiteral("/org/deepin/dde/ControlCenter1"),
        QStringLiteral("org.deepin.dde.ControlCenter1"), QStringLiteral("ShowPage"));
    msg << QStringLiteral("update");
    QDBusConnection::sessionBus().asyncCall(msg);
}

QString Store::friendlyError(const QString &code, const QString &message)
{
    static const QHash<QString, QString> known = {
        {QStringLiteral("not-authorized"), tr("Not authorized")},
        {QStringLiteral("catalog-unavailable"), tr("The catalog is unavailable")},
        {QStringLiteral("catalog-unverified"), tr("The catalog signature could not be verified")},
        {QStringLiteral("unknown-app"), tr("Unknown app")},
        {QStringLiteral("protected-package"), tr("This package belongs to the system and can't be removed")},
        {QStringLiteral("busy"), tr("This app already has a task running")},
        {QStringLiteral("download-failed"), tr("Download failed")},
        {QStringLiteral("vendor-download"), tr("Download it from the vendor's website")},
        {QStringLiteral("hash-mismatch"), tr("The download doesn't match its checksum")},
        {QStringLiteral("install-profile-invalid"), tr("The install settings are invalid")},
        {QStringLiteral("dependency-unresolvable"), tr("Dependencies can't be resolved")},
        {QStringLiteral("packagekit-error"), tr("The package manager reported an error")},
        {QStringLiteral("cancelled"), tr("Cancelled")},
        {QStringLiteral("helper-unavailable"), tr("The store service isn't available")},
    };
    const QString text = known.value(code);
    if (text.isEmpty())
        return message;
    return message.isEmpty() || code == QLatin1String("cancelled") ? text : text + QStringLiteral(": ") + message;
}
