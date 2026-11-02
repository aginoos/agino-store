// SPDX-License-Identifier: GPL-3.0-or-later
// org.aginoos.Store1 implementation (docs/design.md sections 5 to 7). The generated
// StoreAdaptor forwards the D-Bus calls to the public slots below.
#pragma once

#include "catalogcache.h"
#include "catalogrefresher.h"
#include "config.h"
#include "installedstore.h"
#include "jobrunner.h"
#include "pkclient.h"

#include <QDBusContext>
#include <QDBusMessage>
#include <QElapsedTimer>
#include <QJsonArray>
#include <QJsonObject>
#include <QList>
#include <QNetworkAccessManager>
#include <QObject>
#include <QSet>
#include <QTimer>
#include <QVariantMap>

#include <functional>
#include <memory>

namespace AginoStore {

class StoreService : public QObject, protected QDBusContext
{
    Q_OBJECT
public:
    explicit StoreService(const HelperConfig &config, QObject *parent = nullptr);

public Q_SLOTS:
    // D-Bus methods. Methods that reply later call setDelayedReply(true) and return a dummy value.
    // qdbusxml2cpp maps the first out argument (serial) to the return value.
    qlonglong Refresh(QString &revision);
    QString GetState();
    QString Simulate(const QString &appId, const QString &action);
    uint Install(const QString &appId, const QVariantMap &options);
    uint Update(const QString &appId, const QVariantMap &options);
    uint Remove(const QString &appId, const QVariantMap &options);
    void Cancel(uint jobId);

Q_SIGNALS:
    void JobProgress(uint jobId, const QString &appId, const QString &stage, uint percent, qulonglong done,
                     qulonglong total);
    void JobFinished(uint jobId, const QString &appId, bool ok, const QString &errorCode, const QString &message);
    void CatalogChanged(qlonglong serial, const QString &revision);
    void StateChanged();

private:
    struct Job {
        uint id = 0;
        QString appId;
        JobAction action = JobAction::Install;
        bool keepPackage = false;
        bool started = false;
        QString stage = QStringLiteral("waiting");
        uint percent = 0;
        quint64 done = 0;
        quint64 total = 0;
        JobRunner *runner = nullptr;
    };

    uint submit(JobAction action, const QString &appId, const QVariantMap &options);
    void enqueue(const QDBusMessage &msg, JobAction action, const QString &appId, bool keepPackage);
    void startNext();
    void finishJob(uint jobId, bool ok, const QString &errorCode, const QString &message);
    Job *findJob(uint jobId);
    Job *findJobForApp(const QString &appId);

    void authorize(const QDBusMessage &msg, std::function<void()> authorized);
    void replyError(const QDBusMessage &msg, const QString &code, const QString &text);
    void onRefreshFinished(bool ok, const QString &errorCode, const QString &message, bool changed);
    bool isIdle() const;
    void invalidateState();
    void refreshStateCache();
    void touch() { m_activity.restart(); }

    HelperConfig m_config;
    CatalogCache m_cache;
    InstalledStore m_installed;
    QNetworkAccessManager m_nam;
    PkClient m_pk;
    CatalogRefresher m_refresher;

    QList<std::shared_ptr<Job>> m_jobs;
    uint m_nextJobId = 1;
    bool m_jobRunning = false;
    QList<QDBusMessage> m_refreshWaiters;
    QSet<QString> m_simulatingSenders;
    QJsonObject m_cachedInstalled;
    QJsonArray m_cachedUpdates;
    QElapsedTimer m_stateAge;
    bool m_stateValid = false;
    int m_pending = 0;         // authorization checks and simulations in flight

    QElapsedTimer m_activity;
    QTimer m_idleTimer;
};

} // namespace AginoStore
