// SPDX-License-Identifier: GPL-3.0-or-later
// Store: install / update / remove flow (Simulate, confirmation, jobs) - docs/design.md 7 and 8.
#include "store.h"

#include "appsettings.h"
#include "desktopicons.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>

using namespace AginoStore;

namespace {

QStringList namesOf(const QJsonArray &packages)
{
    QStringList names;
    for (const QJsonValue &v : packages) {
        const QString name = v.toObject().value(QStringLiteral("name")).toString();
        if (!name.isEmpty())
            names.append(name);
    }
    return names;
}

QString pendingKey(const QString &appId, const QString &action)
{
    return appId + QLatin1Char(':') + action;
}

} // namespace

void Store::requestAction(const QString &appId, const QString &action)
{
    if (!m_loaded.catalog.app(appId) || m_tasks.isBusy(appId))
        return;
    if (action != QLatin1String("install") && action != QLatin1String("update") && action != QLatin1String("remove"))
        return;
    const QString key = pendingKey(appId, action);
    if (m_pending.contains(key))
        return;
    m_pending.append(key);
    m_helper.simulate(appId, action);
}

void Store::requestUpdateAll()
{
    if (m_batch.active)
        return;
    QStringList ids;
    for (auto it = m_updates.constBegin(); it != m_updates.constEnd(); ++it) {
        if (m_loaded.catalog.app(it.key()) && !m_tasks.isBusy(it.key()))
            ids.append(it.key());
    }
    if (ids.isEmpty())
        return;
    ids.sort();
    m_batch = Batch();
    m_batch.active = true;
    m_batch.ids = ids;
    m_batch.remaining = int(ids.size());
    for (const QString &id : std::as_const(ids)) {
        m_pending.append(pendingKey(id, QStringLiteral("update")));
        m_helper.simulate(id, QStringLiteral("update"));
    }
}

void Store::onSimulated(const QString &appId, const QString &action, const QString &json)
{
    m_pending.removeAll(pendingKey(appId, action));
    const QJsonObject facts = QJsonDocument::fromJson(json.toUtf8()).object();
    const qint64 download = qint64(facts.value(QStringLiteral("download_bytes")).toDouble());
    const qint64 delta = qint64(facts.value(QStringLiteral("installed_delta")).toDouble());
    const QStringList installs = namesOf(facts.value(QStringLiteral("install")).toArray());
    const QStringList removes = namesOf(facts.value(QStringLiteral("remove")).toArray());

    if (m_batch.active && action == QLatin1String("update") && m_batch.ids.contains(appId)) {
        m_batch.download += download;
        m_batch.delta += delta;
        m_batch.installs += installs;
        if (--m_batch.remaining > 0)
            return;
        const Batch done = m_batch;
        m_batch = Batch();
        if (!done.failed)
            emitRequest(done.ids, QStringLiteral("update"), done.download, done.delta, done.installs, {});
        return;
    }
    emitRequest({appId}, action, download, delta, installs, removes);
}

void Store::emitRequest(const QStringList &appIds, const QString &action, qint64 download, qint64 delta,
                        const QStringList &installs, const QStringList &removes)
{
    // The apps' own packages are not "extra components".
    QStringList own;
    for (const QString &id : appIds)
        own.append(packageName(id));
    auto extras = [&own](const QStringList &names) {
        QStringList out;
        for (const QString &n : names) {
            if (!own.contains(n) && !out.contains(n))
                out.append(n);
        }
        out.sort();
        return out;
    };

    QVariantMap request;
    request.insert(QStringLiteral("appIds"), appIds);
    request.insert(QStringLiteral("action"), action);
    request.insert(QStringLiteral("batch"), appIds.size() != 1);
    request.insert(QStringLiteral("count"), int(appIds.size()));
    if (appIds.size() == 1) {
        request.insert(QStringLiteral("name"), app(appIds.first()).value(QStringLiteral("name")));
        request.insert(QStringLiteral("source"), sourceText(appIds.first()));
    }
    request.insert(QStringLiteral("downloadBytes"), download);
    request.insert(QStringLiteral("installedDelta"), delta);
    request.insert(QStringLiteral("extraInstall"), extras(installs));
    request.insert(QStringLiteral("extraRemove"), extras(removes));
    emit confirmRequested(request);
}

void Store::confirmAction(const QVariantMap &request)
{
    const QStringList ids = request.value(QStringLiteral("appIds")).toStringList();
    const QString action = request.value(QStringLiteral("action")).toString();
    QVariantMap options;
    if (action != QLatin1String("remove"))
        options.insert(QStringLiteral("keep_package"), !AppSettings::instance()->deletePackagesAfterInstall());
    for (const QString &id : ids) {
        if (m_loaded.catalog.app(id))
            m_helper.run(action, id, options);
    }
}

void Store::cancelTask(uint jobId)
{
    m_helper.cancel(jobId);
}

void Store::onJobStarted(uint jobId, const QString &appId, const QString &action)
{
    m_tasks.started(jobId, appId, action);
    bumpState();
    maybeHandleFinished(jobId);
}

void Store::maybeHandleFinished(uint jobId)
{
    const TaskModel::Task *found = m_tasks.find(jobId);
    if (!found || !found->finished || found->handled || found->kind.isEmpty())
        return;
    const TaskModel::Task task = *found;   // markHandled does not invalidate, but copy anyway
    m_tasks.markHandled(jobId);

    const QString name = app(task.appId).value(QStringLiteral("name"), task.appId).toString();
    if (task.ok) {
        if (task.kind == QLatin1String("remove")) {
            DesktopIcons::instance()->removed(task.appId);
            emit toast(tr("%1 was uninstalled").arg(name));
        } else {
            if (task.kind == QLatin1String("install") && AppSettings::instance()->createDesktopIcon())
                DesktopIcons::instance()->installed(task.appId, packageName(task.appId), launchable(task.appId));
            emit toast(task.kind == QLatin1String("update") ? tr("%1 was updated").arg(name)
                                                            : tr("%1 is installed").arg(name));
        }
    } else {
        emit toast(tr("%1: %2").arg(name, friendlyError(task.errorCode, task.message)));
    }
    m_helper.getState();
}
