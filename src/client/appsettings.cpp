// SPDX-License-Identifier: GPL-3.0-or-later
#include "appsettings.h"

#include <DConfig>
#include <QCoreApplication>
#include <QJSEngine>
#include <QQmlEngine>

namespace {
// TODO(verify): DConfig::create(appId, name) looks the metadata up under
// /usr/share/dsg/configs/<appId>/<name>.json; both are "org.aginoos.store" here.
const QString kAppId = QStringLiteral("org.aginoos.store");
const QString kConfigName = QStringLiteral("org.aginoos.store");
} // namespace

AppSettings *AppSettings::instance()
{
    static AppSettings *self = new AppSettings(qApp);
    return self;
}

AppSettings *AppSettings::create(QQmlEngine *, QJSEngine *)
{
    AppSettings *self = instance();
    QJSEngine::setObjectOwnership(self, QJSEngine::CppOwnership);
    return self;
}

AppSettings::AppSettings(QObject *parent)
    : QObject(parent)
    , m_config(Dtk::Core::DConfig::create(kAppId, kConfigName, QString(), this))
{
    if (m_config && m_config->isValid()) {
        connect(m_config, &Dtk::Core::DConfig::valueChanged, this, [this](const QString &) {
            emit changed();
        });
    } else {
        qWarning("agino-store: DConfig %s unavailable, settings are kept in memory only",
                 qPrintable(kConfigName));
    }
}

bool AppSettings::get(const QString &key, bool fallback) const
{
    if (m_config && m_config->isValid()) {
        const QVariant value = m_config->value(key, fallback);
        return value.isValid() ? value.toBool() : fallback;
    }
    return m_fallback.value(key, fallback);
}

void AppSettings::set(const QString &key, bool value)
{
    if (m_config && m_config->isValid()) {
        m_config->setValue(key, value);
        // valueChanged will notify; emit anyway so bindings settle if the backend is slow.
    } else {
        m_fallback.insert(key, value);
    }
    emit changed();
}
