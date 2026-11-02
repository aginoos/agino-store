// SPDX-License-Identifier: GPL-3.0-or-later
#include "helperclient.h"

#include <QDBusConnection>
#include <QDBusPendingCallWatcher>
#include <QDBusPendingReply>

namespace {

constexpr int kCallTimeoutMs = 10 * 60 * 1000;   // polkit prompts and catalog downloads are slow
const QString kErrorPrefix = QStringLiteral("org.aginoos.Store1.Error.");

} // namespace

HelperClient::HelperClient(QObject *parent)
    : QObject(parent)
    , m_iface(QStringLiteral("org.aginoos.Store1"), QStringLiteral("/org/aginoos/Store1"),
              QDBusConnection::systemBus(), this)
{
    m_iface.setTimeout(kCallTimeoutMs);
    connect(&m_iface, &OrgAginoosStore1Interface::JobProgress, this, &HelperClient::jobProgress);
    connect(&m_iface, &OrgAginoosStore1Interface::JobFinished, this, &HelperClient::jobFinished);
    connect(&m_iface, &OrgAginoosStore1Interface::CatalogChanged, this, &HelperClient::catalogChanged);
    connect(&m_iface, &OrgAginoosStore1Interface::StateChanged, this, &HelperClient::stateChanged);
}

QString HelperClient::errorCode(const QDBusError &error)
{
    const QString name = error.name();
    if (!name.startsWith(kErrorPrefix))
        return QStringLiteral("helper-unavailable");
    QString camel = name.mid(kErrorPrefix.size());
    QString kebab;
    for (int i = 0; i < camel.size(); ++i) {
        const QChar c = camel.at(i);
        if (c.isUpper()) {
            if (i > 0)
                kebab += QLatin1Char('-');
            kebab += c.toLower();
        } else {
            kebab += c;
        }
    }
    return kebab;
}

void HelperClient::refresh()
{
    auto *watcher = new QDBusPendingCallWatcher(m_iface.Refresh(), this);
    connect(watcher, &QDBusPendingCallWatcher::finished, this, [this](QDBusPendingCallWatcher *w) {
        w->deleteLater();
        const QDBusPendingReply<qlonglong, QString> reply = *w;
        if (reply.isError())
            emit refreshFailed(errorCode(reply.error()), reply.error().message());
        else
            emit refreshed(reply.argumentAt<0>(), reply.argumentAt<1>());
    });
}

void HelperClient::getState()
{
    auto *watcher = new QDBusPendingCallWatcher(m_iface.GetState(), this);
    connect(watcher, &QDBusPendingCallWatcher::finished, this, [this](QDBusPendingCallWatcher *w) {
        w->deleteLater();
        const QDBusPendingReply<QString> reply = *w;
        if (!reply.isError())
            emit stateReceived(reply.value());
    });
}

void HelperClient::simulate(const QString &appId, const QString &action)
{
    auto *watcher = new QDBusPendingCallWatcher(m_iface.Simulate(appId, action), this);
    connect(watcher, &QDBusPendingCallWatcher::finished, this,
            [this, appId, action](QDBusPendingCallWatcher *w) {
                w->deleteLater();
                const QDBusPendingReply<QString> reply = *w;
                if (reply.isError())
                    emit simulateFailed(appId, action, errorCode(reply.error()), reply.error().message());
                else
                    emit simulated(appId, action, reply.value());
            });
}

void HelperClient::run(const QString &action, const QString &appId, const QVariantMap &options)
{
    QDBusPendingCall call = action == QLatin1String("remove")   ? m_iface.Remove(appId, options)
                            : action == QLatin1String("update") ? m_iface.Update(appId, options)
                                                                : m_iface.Install(appId, options);
    auto *watcher = new QDBusPendingCallWatcher(call, this);
    connect(watcher, &QDBusPendingCallWatcher::finished, this,
            [this, appId, action](QDBusPendingCallWatcher *w) {
                w->deleteLater();
                const QDBusPendingReply<uint> reply = *w;
                if (reply.isError())
                    emit jobStartFailed(appId, action, errorCode(reply.error()), reply.error().message());
                else
                    emit jobStarted(reply.value(), appId, action);
            });
}

void HelperClient::cancel(uint jobId)
{
    m_iface.Cancel(jobId);
}
