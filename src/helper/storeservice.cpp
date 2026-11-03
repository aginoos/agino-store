// SPDX-License-Identifier: GPL-3.0-or-later
#include "storeservice.h"

#include "dpkgutil.h"
#include "paths.h"
#include "polkit.h"
#include "simulator.h"

#include <QCoreApplication>
#include <QDBusConnection>
#include <QDebug>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>

#include <utility>

namespace AginoStore {

namespace {

constexpr qint64 kIdleMs = 5 * 60 * 1000;
constexpr int kIdleCheckMs = 15 * 1000;
constexpr int kMaxRefreshWaiters = 32;
constexpr int kMaxSimulations = 4;
constexpr qint64 kStateCacheMs = 60 * 1000;

// "not-authorized" -> "org.aginoos.Store1.Error.NotAuthorized"
QString errorName(const QString &code)
{
    QString name;
    const QStringList parts = code.split(QLatin1Char('-'), Qt::SkipEmptyParts);
    for (const QString &part : parts)
        name += part.left(1).toUpper() + part.mid(1);
    return QStringLiteral("org.aginoos.Store1.Error.") + name;
}

QString actionName(JobAction action)
{
    switch (action) {
    case JobAction::Install: return QStringLiteral("install");
    case JobAction::Update: return QStringLiteral("update");
    case JobAction::Remove: return QStringLiteral("remove");
    }
    return QString();
}

} // namespace

StoreService::StoreService(const HelperConfig &config, QObject *parent)
    : QObject(parent)
    , m_config(config)
    , m_cache(config)
    , m_refresher(config, &m_cache, &m_nam)
{
    m_activity.start();
    connect(&m_refresher, &CatalogRefresher::finished, this, &StoreService::onRefreshFinished);
    m_idleTimer.setInterval(kIdleCheckMs);
    connect(&m_idleTimer, &QTimer::timeout, this, [this]() {
        if (m_activity.elapsed() >= kIdleMs && isIdle()) {
            qInfo("idle, exiting");
            QCoreApplication::quit();
        }
    });
    m_idleTimer.start();
}

bool StoreService::isIdle() const
{
    return m_jobs.isEmpty() && m_pending == 0 && m_refreshWaiters.isEmpty() && !m_refresher.isRunning();
}

void StoreService::replyError(const QDBusMessage &msg, const QString &code, const QString &text)
{
    touch();
    QDBusConnection::systemBus().send(msg.createErrorReply(errorName(code), text));
}

void StoreService::authorize(const QDBusMessage &msg, std::function<void()> authorized)
{
    ++m_pending;
    checkAuthorization(this, msg.service(), QString::fromLatin1(kActionManage), true,
                       [this, msg, authorized = std::move(authorized)](AuthResult result) {
                           --m_pending;
                           touch();
                           if (result == AuthResult::Authorized) {
                               authorized();
                           } else {
                               qWarning().noquote() << "not authorized:" << msg.service() << msg.member();
                               replyError(msg, QStringLiteral("not-authorized"),
                                          result == AuthResult::Denied ? QStringLiteral("not authorized")
                                                                       : QStringLiteral("authorization check failed"));
                           }
                       });
}

// --- Refresh -------------------------------------------------------------------------------

qlonglong StoreService::Refresh(QString &revision)
{
    Q_UNUSED(revision)
    touch();
    const QDBusMessage msg = message();
    if (m_refreshWaiters.size() >= kMaxRefreshWaiters) {
        sendErrorReply(errorName(QStringLiteral("busy")), QStringLiteral("too many pending refreshes"));
        return 0;
    }
    setDelayedReply(true);
    m_refreshWaiters.append(msg);
    m_refresher.start();
    return 0;
}

void StoreService::onRefreshFinished(bool ok, const QString &errorCode, const QString &text, bool changed)
{
    touch();
    const QList<QDBusMessage> waiters = std::exchange(m_refreshWaiters, {});
    const CatalogSnapshot &snap = m_cache.snapshot();
    for (const QDBusMessage &msg : waiters) {
        if (ok) {
            QDBusConnection::systemBus().send(msg.createReply(
                QVariantList{QVariant::fromValue<qlonglong>(snap.manifest.serial), snap.manifest.revision}));
        } else {
            replyError(msg, errorCode, text);
        }
    }
    if (ok && changed) {
        invalidateState();
        Q_EMIT CatalogChanged(snap.manifest.serial, snap.manifest.revision);
    }
}

// --- GetState ------------------------------------------------------------------------------

void StoreService::invalidateState()
{
    m_stateValid = false;
}

void StoreService::refreshStateCache()
{
    // dpkg may also change behind our back (apt), so the cache expires on its own as well.
    if (m_stateValid && m_stateAge.elapsed() < kStateCacheMs)
        return;
    const CatalogSnapshot &snap = m_cache.snapshot();

    QStringList names;
    for (const AppSummary &app : snap.catalog.apps)
        names.append(app.package.name);
    for (auto it = m_installed.entries().cbegin(); it != m_installed.entries().cend(); ++it)
        names.append(it.value().package);
    const QHash<QString, QString> versions = installedVersions(names);

    QJsonObject installed;
    QJsonArray updates;
    QStringList seen;
    for (const AppSummary &app : snap.catalog.apps) {
        const QString version = versions.value(app.package.name);
        if (version.isEmpty())
            continue;
        seen.append(app.id);
        installed.insert(app.id, QJsonObject{{QStringLiteral("package"), app.package.name},
                                             {QStringLiteral("version"), version}});
        // Only vendor debs are updated through the store; milkyway packages follow the system.
        if (app.package.kind == PackageRef::Kind::Deb && app.package.deb
            && dpkgVersionGreater(app.package.deb->version, version)) {
            updates.append(QJsonObject{{QStringLiteral("id"), app.id},
                                       {QStringLiteral("installed"), version},
                                       {QStringLiteral("available"), app.package.deb->version}});
        }
    }
    // Apps the store installed that are no longer in the catalog.
    for (auto it = m_installed.entries().cbegin(); it != m_installed.entries().cend(); ++it) {
        const QString version = versions.value(it.value().package);
        if (!seen.contains(it.key()) && !version.isEmpty()) {
            installed.insert(it.key(), QJsonObject{{QStringLiteral("package"), it.value().package},
                                                   {QStringLiteral("version"), version}});
        }
    }
    m_cachedInstalled = installed;
    m_cachedUpdates = updates;
    m_stateValid = true;
    m_stateAge.restart();
}

QString StoreService::GetState()
{
    touch();
    const CatalogSnapshot &snap = m_cache.snapshot();
    refreshStateCache();
    const QJsonObject installed = m_cachedInstalled;
    const QJsonArray updates = m_cachedUpdates;

    QJsonArray jobs;
    for (const std::shared_ptr<Job> &job : m_jobs) {
        jobs.append(QJsonObject{{QStringLiteral("id"), static_cast<qint64>(job->id)},
                                {QStringLiteral("app"), job->appId},
                                {QStringLiteral("action"), actionName(job->action)},
                                {QStringLiteral("stage"), job->stage},
                                {QStringLiteral("percent"), static_cast<qint64>(job->percent)},
                                {QStringLiteral("done"), static_cast<qint64>(job->done)},
                                {QStringLiteral("total"), static_cast<qint64>(job->total)}});
    }

    const QJsonObject state{{QStringLiteral("serial"), static_cast<qint64>(snap.manifest.serial)},
                            {QStringLiteral("revision"), snap.manifest.revision},
                            {QStringLiteral("verified"), snap.valid && !snap.expired},
                            {QStringLiteral("expired"), snap.valid && snap.expired},
                            {QStringLiteral("installed"), installed},
                            {QStringLiteral("updates"), updates},
                            {QStringLiteral("jobs"), jobs}};
    return QString::fromUtf8(QJsonDocument(state).toJson(QJsonDocument::Compact));
}

// --- Simulate ------------------------------------------------------------------------------

QString StoreService::Simulate(const QString &appId, const QString &action)
{
    touch();
    const QDBusMessage msg = message();
    setDelayedReply(true);

    const CatalogSnapshot &snap = m_cache.snapshot();
    const AppSummary *app = isValidAppId(appId) && snap.valid ? snap.catalog.app(appId) : nullptr;
    if (!app) {
        replyError(msg, snap.valid ? QStringLiteral("unknown-app") : QStringLiteral("catalog-unavailable"),
                   QStringLiteral("unknown app"));
        return QString();
    }
    const QString sender = msg.service();
    if (m_simulatingSenders.contains(sender) || m_simulatingSenders.size() >= kMaxSimulations) {
        replyError(msg, QStringLiteral("busy"), QStringLiteral("too many simulations in flight"));
        return QString();
    }
    m_simulatingSenders.insert(sender);
    ++m_pending;
    simulateAction(&m_pk, m_config, *app, action,
                   [this, msg, sender](bool ok, const QString &errorCode, const QString &text) {
                       --m_pending;
                       m_simulatingSenders.remove(sender);
                       touch();
                       if (ok)
                           QDBusConnection::systemBus().send(msg.createReply(QVariant(text)));
                       else
                           replyError(msg, errorCode, text);
                   });
    return QString();
}

// --- jobs ----------------------------------------------------------------------------------

uint StoreService::Install(const QString &appId, const QVariantMap &options)
{
    return submit(JobAction::Install, appId, options);
}

uint StoreService::Update(const QString &appId, const QVariantMap &options)
{
    return submit(JobAction::Update, appId, options);
}

uint StoreService::Remove(const QString &appId, const QVariantMap &options)
{
    return submit(JobAction::Remove, appId, options);
}

uint StoreService::submit(JobAction action, const QString &appId, const QVariantMap &options)
{
    touch();
    const QDBusMessage msg = message();
    setDelayedReply(true);
    const bool keep = options.value(QStringLiteral("keep_package")).toBool();
    // Only the language string is used; the helper picks the packages from its verified catalog.
    QString lang = options.value(QStringLiteral("lang")).toString();
    if (!isValidL10nKey(lang))
        lang.clear();
    authorize(msg, [this, msg, action, appId, keep, lang]() { enqueue(msg, action, appId, keep, lang); });
    return 0;
}

void StoreService::enqueue(const QDBusMessage &msg, JobAction action, const QString &appId, bool keepPackage,
                           const QString &lang)
{
    const CatalogSnapshot &snap = m_cache.snapshot();
    if (!snap.valid) {
        qWarning().noquote() << "catalog unverified:" << snap.error;
        replyError(msg, QStringLiteral("catalog-unverified"), QStringLiteral("the catalog could not be verified"));
        return;
    }
    if (snap.expired && action != JobAction::Remove) {
        replyError(msg, QStringLiteral("catalog-unverified"), QStringLiteral("the cached catalog has expired"));
        return;
    }
    const AppSummary *app = isValidAppId(appId) ? snap.catalog.app(appId) : nullptr;
    if (!app || !isValidPackageName(app->package.name)) {
        replyError(msg, QStringLiteral("unknown-app"), QStringLiteral("unknown app"));
        return;
    }
    if (action != JobAction::Install && action != JobAction::Update && action != JobAction::Remove) {
        replyError(msg, QStringLiteral("unknown-app"), QStringLiteral("unknown action"));
        return;
    }
    if (action == JobAction::Remove && m_config.isProtected(app->package.name)) {
        replyError(msg, QStringLiteral("protected-package"),
                   QStringLiteral("%1 cannot be removed through the store").arg(app->package.name));
        return;
    }
    if (action != JobAction::Remove && app->package.kind == PackageRef::Kind::Deb && !app->package.deb) {
        replyError(msg, QStringLiteral("unknown-app"), QStringLiteral("the app has no deb"));
        return;
    }
    if (findJobForApp(appId)) {
        replyError(msg, QStringLiteral("busy"), QStringLiteral("the app already has a job"));
        return;
    }

    auto job = std::make_shared<Job>();
    job->id = m_nextJobId++;
    job->appId = appId;
    job->action = action;
    job->keepPackage = keepPackage;
    job->lang = lang;
    m_jobs.append(job);
    QDBusConnection::systemBus().send(msg.createReply(QVariant::fromValue<uint>(job->id)));
    Q_EMIT JobProgress(job->id, appId, job->stage, 0, 0, 0);
    QTimer::singleShot(0, this, &StoreService::startNext);
}

StoreService::Job *StoreService::findJob(uint jobId)
{
    for (const std::shared_ptr<Job> &job : m_jobs) {
        if (job->id == jobId)
            return job.get();
    }
    return nullptr;
}

StoreService::Job *StoreService::findJobForApp(const QString &appId)
{
    for (const std::shared_ptr<Job> &job : m_jobs) {
        if (job->appId == appId)
            return job.get();
    }
    return nullptr;
}

void StoreService::startNext()
{
    if (m_jobRunning)
        return;
    Job *job = nullptr;
    for (const std::shared_ptr<Job> &candidate : m_jobs) {
        if (!candidate->started) {
            job = candidate.get();
            break;
        }
    }
    if (!job)
        return;
    job->started = true;
    m_jobRunning = true;
    touch();

    // Resolve against the catalog as it is now; it may have been refreshed while the job waited.
    const CatalogSnapshot &snap = m_cache.snapshot();
    const AppSummary *app = snap.valid ? snap.catalog.app(job->appId) : nullptr;
    if (!app) {
        finishJob(job->id, false, QStringLiteral("unknown-app"), QStringLiteral("the app is no longer in the catalog"));
        return;
    }
    if (snap.expired && job->action != JobAction::Remove) {
        finishJob(job->id, false, QStringLiteral("catalog-unverified"), QStringLiteral("the cached catalog has expired"));
        return;
    }
    JobContext context;
    context.config = m_config;
    context.app = *app;
    context.action = job->action;
    context.keepPackage = job->keepPackage;
    context.lang = job->lang;
    if (job->action != JobAction::Remove && app->package.kind == PackageRef::Kind::Deb) {
        QString error;
        context.detail = m_cache.loadDetail(*app, &error);
        if (!context.detail) {
            qWarning().noquote() << "catalog detail unavailable:" << job->appId << error;
            finishJob(job->id, false, QStringLiteral("catalog-unverified"),
                      QStringLiteral("the app details could not be verified"));
            return;
        }
    }

    auto *runner = new JobRunner(std::move(context), &m_nam, &m_pk, this);
    job->runner = runner;
    const uint jobId = job->id;
    connect(runner, &JobRunner::progress, this,
            [this, jobId](const QString &stage, uint percent, quint64 done, quint64 total) {
                Job *j = findJob(jobId);
                if (!j)
                    return;
                touch();
                j->stage = stage;
                j->percent = percent;
                j->done = done;
                j->total = total;
                Q_EMIT JobProgress(jobId, j->appId, stage, percent, done, total);
            });
    connect(runner, &JobRunner::finished, this,
            [this, jobId](bool ok, const QString &errorCode, const QString &text) {
                finishJob(jobId, ok, errorCode, text);
            });
    runner->start();
}

void StoreService::finishJob(uint jobId, bool ok, const QString &errorCode, const QString &text)
{
    touch();
    Job *job = findJob(jobId);
    if (!job)
        return;
    const QString appId = job->appId;
    const JobAction action = job->action;
    if (job->runner)
        job->runner->deleteLater();

    if (ok) {
        const CatalogSnapshot &snap = m_cache.snapshot();
        const AppSummary *app = snap.catalog.app(appId);
        if (action == JobAction::Remove) {
            m_installed.erase(appId);
        } else if (app) {
            const bool deb = app->package.kind == PackageRef::Kind::Deb;
            m_installed.record(appId, app->package.name, deb ? QStringLiteral("deb") : QStringLiteral("milkyway"));
        }
    }
    if (!ok)
        qWarning().noquote() << "job" << jobId << appId << actionName(action) << "failed:" << errorCode << text;

    for (int i = 0; i < m_jobs.size(); ++i) {
        if (m_jobs.at(i)->id == jobId) {
            m_jobs.removeAt(i);
            break;
        }
    }
    m_jobRunning = false;
    invalidateState();
    Q_EMIT JobFinished(jobId, appId, ok, ok ? QString() : errorCode, ok ? QString() : text);
    if (ok)
        Q_EMIT StateChanged();
    QTimer::singleShot(0, this, &StoreService::startNext);
}

void StoreService::Cancel(uint jobId)
{
    touch();
    const QDBusMessage msg = message();
    setDelayedReply(true);
    authorize(msg, [this, msg, jobId]() {
        Job *job = findJob(jobId);
        if (!job) {
            replyError(msg, QStringLiteral("unknown-app"), QStringLiteral("no such job"));
            return;
        }
        if (!job->started) {
            const QString appId = job->appId;
            for (int i = 0; i < m_jobs.size(); ++i) {
                if (m_jobs.at(i)->id == jobId) {
                    m_jobs.removeAt(i);
                    break;
                }
            }
            QDBusConnection::systemBus().send(msg.createReply());
            Q_EMIT JobFinished(jobId, appId, false, QStringLiteral("cancelled"), QStringLiteral("cancelled"));
            return;
        }
        if (job->runner && job->runner->canCancel()) {
            job->runner->cancel();
            QDBusConnection::systemBus().send(msg.createReply());
            return;
        }
        replyError(msg, QStringLiteral("busy"), QStringLiteral("the job can no longer be cancelled"));
    });
}

} // namespace AginoStore
