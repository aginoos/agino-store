// SPDX-License-Identifier: GPL-3.0-or-later
// agino-store-helper: system-bus service org.aginoos.Store1 (docs/design.md section 5).
// Started by D-Bus activation; exits after 5 minutes without calls or jobs.

#include "config.h"
#include "paths.h"
#include "storeadaptor.h"
#include "storeservice.h"

#include <QCoreApplication>
#include <QDBusConnection>
#include <QMap>
#include <QtDBus/qdbusmetatype.h>

using namespace AginoStore;

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("agino-store-helper"));

    // a{ss} for the polkit call.
    qDBusRegisterMetaType<QMap<QString, QString>>();

    QDBusConnection bus = QDBusConnection::systemBus();
    if (!bus.isConnected()) {
        qCritical("cannot connect to the system bus");
        return 1;
    }

    StoreService service(HelperConfig::load(configFilePath()));
    new StoreAdaptor(&service);   // owned by the service
    if (!bus.registerObject(QStringLiteral("/org/aginoos/Store1"), &service)) {
        qCritical("cannot register the object: %s", qPrintable(bus.lastError().message()));
        return 1;
    }
    if (!bus.registerService(QStringLiteral("org.aginoos.Store1"))) {
        qCritical("cannot own org.aginoos.Store1: %s", qPrintable(bus.lastError().message()));
        return 1;
    }
    qInfo("agino-store-helper started");
    return app.exec();
}
