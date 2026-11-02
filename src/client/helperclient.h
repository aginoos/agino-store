// SPDX-License-Identifier: GPL-3.0-or-later
// Qt-friendly wrapper around the generated org.aginoos.Store1 proxy (docs/design.md section 5).
#pragma once

#include "store_interface.h"

#include <QObject>
#include <QString>
#include <QVariantMap>

class HelperClient : public QObject
{
    Q_OBJECT
public:
    explicit HelperClient(QObject *parent = nullptr);

    void refresh();
    void getState();
    void simulate(const QString &appId, const QString &action);
    // action: "install", "update" or "remove".
    void run(const QString &action, const QString &appId, const QVariantMap &options);
    void cancel(uint jobId);

    // "org.aginoos.Store1.Error.NotAuthorized" -> "not-authorized"; bus problems -> "helper-unavailable".
    static QString errorCode(const QDBusError &error);

signals:
    void refreshed(qint64 serial, const QString &revision);
    void refreshFailed(const QString &code, const QString &message);
    void stateReceived(const QString &json);
    void simulated(const QString &appId, const QString &action, const QString &json);
    void simulateFailed(const QString &appId, const QString &action, const QString &code,
                        const QString &message);
    void jobStarted(uint jobId, const QString &appId, const QString &action);
    void jobStartFailed(const QString &appId, const QString &action, const QString &code,
                        const QString &message);
    void jobProgress(uint jobId, const QString &appId, const QString &stage, uint percent,
                     qulonglong done, qulonglong total);
    void jobFinished(uint jobId, const QString &appId, bool ok, const QString &errorCode,
                     const QString &message);
    void catalogChanged(qint64 serial, const QString &revision);
    void stateChanged();

private:
    OrgAginoosStore1Interface m_iface;
};
