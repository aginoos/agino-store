// SPDX-License-Identifier: GPL-3.0-or-later
#include "desktopicons.h"

#include "catalog.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJSEngine>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QRegularExpression>
#include <QQmlEngine>
#include <QSaveFile>
#include <QStandardPaths>
#include <QVariantMap>

namespace {

constexpr int kProcessTimeoutMs = 5000;

// Package names are used as process arguments: accept the Debian grammar only.
bool isSafePackageName(const QString &name)
{
    static const QRegularExpression re(QStringLiteral("\\A[a-z0-9][a-z0-9.+-]+\\z"));
    return re.match(name).hasMatch();
}

QString runCapture(const QString &program, const QStringList &args)
{
    QProcess process;
    process.start(program, args);
    if (!process.waitForFinished(kProcessTimeoutMs) || process.exitStatus() != QProcess::NormalExit
        || process.exitCode() != 0) {
        process.kill();
        return {};
    }
    return QString::fromUtf8(process.readAllStandardOutput());
}

} // namespace

DesktopIcons *DesktopIcons::instance()
{
    static DesktopIcons *self = new DesktopIcons(qApp);
    return self;
}

DesktopIcons *DesktopIcons::create(QQmlEngine *, QJSEngine *)
{
    DesktopIcons *self = instance();
    QJSEngine::setObjectOwnership(self, QJSEngine::CppOwnership);
    return self;
}

DesktopIcons::DesktopIcons(QObject *parent)
    : QObject(parent)
{
}

QString DesktopIcons::desktopFileFor(const QString &packageName, const QString &launchable)
{
    // The catalog's launchable names the launcher even when another package (a dependency of a
    // metapackage) ships it.
    if (AginoStore::isValidDesktopId(launchable)) {
        const QString found = QStandardPaths::locate(QStandardPaths::GenericDataLocation,
                                                     QStringLiteral("applications/") + launchable);
        if (!found.isEmpty())
            return found;
    }
    if (!isSafePackageName(packageName))
        return {};
    // TODO(verify): the first /usr/share/applications/*.desktop owned by the package is taken to
    // be its launcher; packages with several launchers get the alphabetically first one.
    const QString listing = runCapture(QStringLiteral("dpkg"), {QStringLiteral("-L"), packageName});
    QStringList candidates;
    const QStringList lines = listing.split(QLatin1Char('\n'), Qt::SkipEmptyParts);
    for (const QString &line : lines) {
        if (line.startsWith(QStringLiteral("/usr/share/applications/")) && line.endsWith(QStringLiteral(".desktop"))
            && QFileInfo(line).isFile())
            candidates.append(line);
    }
    candidates.sort();
    return candidates.isEmpty() ? QString() : candidates.first();
}

QString DesktopIcons::desktopDir()
{
    const QString out = runCapture(QStringLiteral("xdg-user-dir"), {QStringLiteral("DESKTOP")}).trimmed();
    if (!out.isEmpty() && QFileInfo(out).isDir())
        return out;
    return QDir::homePath() + QStringLiteral("/Desktop");
}

QString DesktopIcons::recordPath()
{
    return QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation)
           + QStringLiteral("/agino-store/desktop-icons.json");
}

QVariantMap DesktopIcons::loadRecord() const
{
    QFile file(recordPath());
    if (!file.open(QIODevice::ReadOnly))
        return {};
    return QJsonDocument::fromJson(file.readAll()).object().toVariantMap();
}

void DesktopIcons::saveRecord(const QVariantMap &record) const
{
    QDir().mkpath(QFileInfo(recordPath()).absolutePath());
    QSaveFile file(recordPath());
    if (file.open(QIODevice::WriteOnly)) {
        file.write(QJsonDocument(QJsonObject::fromVariantMap(record)).toJson());
        file.commit();
    }
}

void DesktopIcons::installed(const QString &appId, const QString &packageName, const QString &launchable)
{
    const QString source = desktopFileFor(packageName, launchable);
    if (source.isEmpty())
        return;
    const QString dir = desktopDir();
    QDir().mkpath(dir);
    const QString target = dir + QLatin1Char('/') + QFileInfo(source).fileName();
    if (QFileInfo::exists(target))
        return;   // never overwrite something the user put there
    if (!QFile::copy(source, target))
        return;
    QFile::setPermissions(target, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner
                                      | QFileDevice::ReadGroup | QFileDevice::ReadOther);
    QVariantMap record = loadRecord();
    record.insert(appId, target);
    saveRecord(record);
}

void DesktopIcons::removed(const QString &appId)
{
    QVariantMap record = loadRecord();
    const QString target = record.value(appId).toString();
    if (target.isEmpty())
        return;
    // Only touch files directly inside the desktop folder that we recorded.
    if (QFileInfo(target).isFile() && target.endsWith(QStringLiteral(".desktop")))
        QFile::remove(target);
    record.remove(appId);
    saveRecord(record);
}

void DesktopIcons::launch(const QString &packageName, const QString &launchable)
{
    const QString file = desktopFileFor(packageName, launchable);
    if (file.isEmpty())
        return;
    // TODO(verify): "gio launch" (libglib2.0-bin) starts a desktop file the way the launcher does.
    QProcess::startDetached(QStringLiteral("gio"), {QStringLiteral("launch"), file});
}
