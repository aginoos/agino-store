// SPDX-License-Identifier: GPL-3.0-or-later
// File system and process helpers. Nothing here goes through a shell.
#pragma once

#include <QByteArray>
#include <QFileDevice>
#include <QString>
#include <QStringList>

namespace AginoStore {

// Qt permission flags are not octal modes; these are the three modes the helper uses.
inline QFileDevice::Permissions perm755()
{
    return QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner
        | QFileDevice::ReadGroup | QFileDevice::ExeGroup | QFileDevice::ReadOther | QFileDevice::ExeOther;
}
inline QFileDevice::Permissions perm644()
{
    return QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ReadGroup | QFileDevice::ReadOther;
}
inline QFileDevice::Permissions perm700()
{
    return QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner;
}

// Writes through a temporary file in the same directory and renames it over `path`
// (never writes through an existing symlink).
bool writeFileAtomic(const QString &path, const QByteArray &data, QFileDevice::Permissions perms,
                     QString *error);

// Creates the directory (and parents) and sets its permissions. Refuses a symlink.
bool ensureDirectory(const QString &path, QFileDevice::Permissions perms, QString *error);

// Reads at most maxBytes; fails when the file is larger.
bool readFileLimited(const QString &path, qint64 maxBytes, QByteArray *out, QString *error);

// Absolute paths of the entries in `dir` that are not real directories (symlinks included, even
// broken ones). Empty when the directory does not exist.
QStringList listNonDirectories(const QString &dir);

// Atomically makes `newDir` the directory `target`. On success `newDir` no longer exists or holds
// the previous content (the caller removes it).
bool replaceDirectory(const QString &newDir, const QString &target, QString *error);

struct ProcessResult {
    bool started = false;
    bool timedOut = false;
    int exitCode = -1;
    QByteArray out;
    QByteArray err;
    bool ok() const { return started && !timedOut && exitCode == 0; }
};

// Runs program with an argument list (no shell), a clean environment, and a timeout.
ProcessResult runProcess(const QString &program, const QStringList &args,
                         const QByteArray &stdinData = QByteArray(), int timeoutMs = 60000);

} // namespace AginoStore
