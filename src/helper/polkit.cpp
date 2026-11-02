// SPDX-License-Identifier: GPL-3.0-or-later
#include "polkit.h"

#include <QDBusArgument>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusPendingCall>
#include <QDBusPendingCallWatcher>
#include <QMap>
#include <QVariantMap>

namespace AginoStore {

namespace {
constexpr int kPolkitTimeoutMs = 10 * 60 * 1000;   // an authentication dialog may stay open
}

void checkAuthorization(QObject *parent, const QString &busName, const QString &actionId,
                        bool allowInteraction, std::function<void(AuthResult)> done)
{
    QDBusMessage call = QDBusMessage::createMethodCall(
        QStringLiteral("org.freedesktop.PolicyKit1"), QStringLiteral("/org/freedesktop/PolicyKit1/Authority"),
        QStringLiteral("org.freedesktop.PolicyKit1.Authority"), QStringLiteral("CheckAuthorization"));

    // Same marshalling as agino-gpu: subject (sa{sv}), action, details a{ss}, flags, cancellation id.
    QVariantMap subjectDetails;
    subjectDetails.insert(QStringLiteral("name"), busName);
    QDBusArgument subject;
    subject.beginStructure();
    subject << QStringLiteral("system-bus-name") << subjectDetails;
    subject.endStructure();

    call.setArguments({QVariant::fromValue(subject), actionId, QVariant::fromValue(QMap<QString, QString>()),
                       QVariant::fromValue<quint32>(allowInteraction ? 1u : 0u), QString()});

    auto *watcher = new QDBusPendingCallWatcher(
        QDBusConnection::systemBus().asyncCall(call, kPolkitTimeoutMs), parent);
    QObject::connect(watcher, &QDBusPendingCallWatcher::finished, parent,
                     [watcher, done = std::move(done)]() {
                         watcher->deleteLater();
                         const QDBusMessage reply = watcher->reply();
                         if (reply.type() != QDBusMessage::ReplyMessage || reply.arguments().isEmpty()) {
                             done(AuthResult::Error);
                             return;
                         }
                         bool authorized = false;
                         bool challenge = false;
                         QMap<QString, QString> details;
                         const QDBusArgument arg = reply.arguments().constFirst().value<QDBusArgument>();
                         arg.beginStructure();
                         arg >> authorized >> challenge >> details;
                         arg.endStructure();
                         done(authorized ? AuthResult::Authorized : AuthResult::Denied);
                     });
}

} // namespace AginoStore
