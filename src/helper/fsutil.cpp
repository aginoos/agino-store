// SPDX-License-Identifier: GPL-3.0-or-later
#include "fsutil.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QProcess>
#include <QProcessEnvironment>
#include <QSaveFile>

#include <cstdio>
#include <fcntl.h>
#include <unistd.h>

namespace AginoStore {

bool writeFileAtomic(const QString &path, const QByteArray &data, QFileDevice::Permissions perms,
                     QString *error)
{
    QSaveFile file(path);
    file.setDirectWriteFallback(false);
    if (!file.open(QIODevice::WriteOnly)) {
        if (error)
            *error = QStringLiteral("cannot write %1: %2").arg(path, file.errorString());
        return false;
    }
    if (file.write(data) != data.size() || !file.setPermissions(perms) || !file.commit()) {
        if (error)
            *error = QStringLiteral("cannot write %1: %2").arg(path, file.errorString());
        return false;
    }
    return true;
}

bool ensureDirectory(const QString &path, QFileDevice::Permissions perms, QString *error)
{
    const QFileInfo info(path);
    if (info.isSymLink()) {
        if (error)
            *error = QStringLiteral("%1 is a symbolic link").arg(path);
        return false;
    }
    if (!info.exists() && !QDir().mkpath(path)) {
        if (error)
            *error = QStringLiteral("cannot create %1").arg(path);
        return false;
    }
    if (!QFile::setPermissions(path, perms)) {
        if (error)
            *error = QStringLiteral("cannot set permissions on %1").arg(path);
        return false;
    }
    return true;
}

bool readFileLimited(const QString &path, qint64 maxBytes, QByteArray *out, QString *error)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        if (error)
            *error = QStringLiteral("cannot read %1: %2").arg(path, file.errorString());
        return false;
    }
    if (file.size() > maxBytes) {
        if (error)
            *error = QStringLiteral("%1 is too large").arg(path);
        return false;
    }
    *out = file.read(maxBytes + 1);
    if (out->size() > maxBytes) {
        if (error)
            *error = QStringLiteral("%1 is too large").arg(path);
        return false;
    }
    return true;
}

QStringList listNonDirectories(const QString &dir)
{
    QStringList out;
    const QDir d(dir);
    const QFileInfoList entries = d.entryInfoList(QDir::AllEntries | QDir::System | QDir::Hidden | QDir::NoDotAndDotDot);
    for (const QFileInfo &info : entries) {
        if (info.isDir() && !info.isSymLink())
            continue;
        out.append(info.absoluteFilePath());
    }
    out.sort();
    return out;
}

bool replaceDirectory(const QString &newDir, const QString &target, QString *error)
{
    const QByteArray from = QFile::encodeName(newDir);
    const QByteArray to = QFile::encodeName(target);
    const QFileInfo targetInfo(target);
    if (!targetInfo.exists() && !targetInfo.isSymLink()) {
        if (::rename(from.constData(), to.constData()) == 0)
            return true;
        if (error)
            *error = QStringLiteral("cannot rename %1 to %2").arg(newDir, target);
        return false;
    }
    // TODO(verify): renameat2 / RENAME_EXCHANGE availability (glibc >= 2.28, _GNU_SOURCE).
    if (::renameat2(AT_FDCWD, from.constData(), AT_FDCWD, to.constData(), RENAME_EXCHANGE) == 0)
        return true;

    // Fallback: two renames (a tiny window without a catalog directory).
    const QString old = target + QStringLiteral(".old");
    const QByteArray oldEnc = QFile::encodeName(old);
    QDir(old).removeRecursively();
    if (::rename(to.constData(), oldEnc.constData()) != 0) {
        if (error)
            *error = QStringLiteral("cannot move %1 aside").arg(target);
        return false;
    }
    if (::rename(from.constData(), to.constData()) != 0) {
        ::rename(oldEnc.constData(), to.constData());
        if (error)
            *error = QStringLiteral("cannot rename %1 to %2").arg(newDir, target);
        return false;
    }
    QDir(old).removeRecursively();
    return true;
}

ProcessResult runProcess(const QString &program, const QStringList &args, const QByteArray &stdinData,
                         int timeoutMs)
{
    ProcessResult result;
    QProcess process;
    QProcessEnvironment env;
    env.insert(QStringLiteral("PATH"), QStringLiteral("/usr/sbin:/usr/bin:/sbin:/bin"));
    env.insert(QStringLiteral("LC_ALL"), QStringLiteral("C"));
    env.insert(QStringLiteral("DEBIAN_FRONTEND"), QStringLiteral("noninteractive"));
    process.setProcessEnvironment(env);
    process.start(program, args);
    if (!process.waitForStarted(10000))
        return result;
    result.started = true;
    if (!stdinData.isEmpty())
        process.write(stdinData);
    process.closeWriteChannel();
    if (!process.waitForFinished(timeoutMs)) {
        result.timedOut = true;
        process.kill();
        process.waitForFinished(5000);
    }
    result.exitCode = process.exitStatus() == QProcess::NormalExit ? process.exitCode() : -1;
    result.out = process.readAllStandardOutput();
    result.err = process.readAllStandardError();
    return result;
}

} // namespace AginoStore
