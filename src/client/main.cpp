// SPDX-License-Identifier: GPL-3.0-or-later
#include <QGuiApplication>
#include <QIcon>
#include <QLocale>
#include <QQmlApplicationEngine>
#include <QTranslator>

int main(int argc, char *argv[])
{
    QGuiApplication app(argc, argv);
    QGuiApplication::setApplicationName(QStringLiteral("agino-store"));
    QGuiApplication::setApplicationVersion(QStringLiteral(AGINO_STORE_VERSION));
    QGuiApplication::setOrganizationName(QStringLiteral("aginoos"));
    QGuiApplication::setDesktopFileName(QStringLiteral("agino-store"));
    QGuiApplication::setWindowIcon(QIcon::fromTheme(QStringLiteral("agino-store")));

    // Translations are compiled into the executable by qt_add_translations (prefix /i18n).
    QTranslator translator;
    if (translator.load(QLocale::system(), QStringLiteral("agino-store"), QStringLiteral("_"),
                        QStringLiteral(":/i18n")))
        app.installTranslator(&translator);

    QQmlApplicationEngine engine;
    QObject::connect(&engine, &QQmlApplicationEngine::objectCreationFailed, &app,
                     []() { QCoreApplication::exit(1); }, Qt::QueuedConnection);
    engine.loadFromModule("org.aginoos.store", "Main");
    return app.exec();
}
