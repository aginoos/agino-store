// SPDX-License-Identifier: GPL-3.0-or-later
// Desktop shortcuts for freshly installed apps, and launching installed apps.
// Created shortcuts are recorded in ~/.local/share/agino-store/desktop-icons.json so that only
// those are removed again on uninstall.
#pragma once

#include <QObject>
#include <QString>
#include <QVariantMap>
#include <QtQml/qqmlregistration.h>

class QJSEngine;
class QQmlEngine;

class DesktopIcons : public QObject
{
    Q_OBJECT
    QML_ELEMENT
    QML_SINGLETON

public:
    static DesktopIcons *instance();
    static DesktopIcons *create(QQmlEngine *, QJSEngine *);

    // Copies the package's desktop file to the desktop when none exists there yet.
    void installed(const QString &appId, const QString &packageName);
    // Removes the shortcut this store created for the app, if any.
    void removed(const QString &appId);
    Q_INVOKABLE void launch(const QString &packageName);

private:
    explicit DesktopIcons(QObject *parent = nullptr);
    static QString desktopFileFor(const QString &packageName);
    static QString desktopDir();
    static QString recordPath();
    QVariantMap loadRecord() const;
    void saveRecord(const QVariantMap &record) const;
};
