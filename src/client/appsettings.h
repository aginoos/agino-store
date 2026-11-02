// SPDX-License-Identifier: GPL-3.0-or-later
// User settings in DConfig org.aginoos.store (docs/design.md section 8, key table).
#pragma once

#include <QHash>
#include <QObject>
#include <QVariant>
#include <QtQml/qqmlregistration.h>

class QJSEngine;
class QQmlEngine;

namespace Dtk::Core {
class DConfig;
}

class AppSettings : public QObject
{
    Q_OBJECT
    QML_NAMED_ELEMENT(Settings)
    QML_SINGLETON

    Q_PROPERTY(bool autoCheckUpdates READ autoCheckUpdates WRITE setAutoCheckUpdates NOTIFY changed)
    Q_PROPERTY(bool noDownloadOnMetered READ noDownloadOnMetered WRITE setNoDownloadOnMetered NOTIFY changed)
    Q_PROPERTY(bool notifyUpdates READ notifyUpdates WRITE setNotifyUpdates NOTIFY changed)
    // Second phase: readable, not writable from the UI.
    Q_PROPERTY(bool autoInstallUpdates READ autoInstallUpdates NOTIFY changed)
    Q_PROPERTY(bool createDesktopIcon READ createDesktopIcon WRITE setCreateDesktopIcon NOTIFY changed)
    Q_PROPERTY(bool deletePackagesAfterInstall READ deletePackagesAfterInstall
                   WRITE setDeletePackagesAfterInstall NOTIFY changed)
    Q_PROPERTY(bool showPersonal READ showPersonal WRITE setShowPersonal NOTIFY changed)
    Q_PROPERTY(bool showProprietary READ showProprietary WRITE setShowProprietary NOTIFY changed)

public:
    static AppSettings *instance();
    static AppSettings *create(QQmlEngine *, QJSEngine *);

    bool autoCheckUpdates() const { return get(QStringLiteral("autoCheckUpdates"), true); }
    bool noDownloadOnMetered() const { return get(QStringLiteral("noDownloadOnMetered"), true); }
    bool notifyUpdates() const { return get(QStringLiteral("notifyUpdates"), true); }
    bool autoInstallUpdates() const { return get(QStringLiteral("autoInstallUpdates"), false); }
    bool createDesktopIcon() const { return get(QStringLiteral("createDesktopIcon"), true); }
    bool deletePackagesAfterInstall() const { return get(QStringLiteral("deletePackagesAfterInstall"), true); }
    bool showPersonal() const { return get(QStringLiteral("showPersonal"), true); }
    bool showProprietary() const { return get(QStringLiteral("showProprietary"), true); }

    void setAutoCheckUpdates(bool v) { set(QStringLiteral("autoCheckUpdates"), v); }
    void setNoDownloadOnMetered(bool v) { set(QStringLiteral("noDownloadOnMetered"), v); }
    void setNotifyUpdates(bool v) { set(QStringLiteral("notifyUpdates"), v); }
    void setCreateDesktopIcon(bool v) { set(QStringLiteral("createDesktopIcon"), v); }
    void setDeletePackagesAfterInstall(bool v) { set(QStringLiteral("deletePackagesAfterInstall"), v); }
    void setShowPersonal(bool v) { set(QStringLiteral("showPersonal"), v); }
    void setShowProprietary(bool v) { set(QStringLiteral("showProprietary"), v); }

signals:
    void changed();

private:
    explicit AppSettings(QObject *parent = nullptr);
    bool get(const QString &key, bool fallback) const;
    void set(const QString &key, bool value);

    Dtk::Core::DConfig *m_config = nullptr;
    QHash<QString, bool> m_fallback;   // used when no DConfig backend is available
};
