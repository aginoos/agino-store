// SPDX-License-Identifier: GPL-3.0-or-later
#include "dpkgutil.h"

#include "fsutil.h"
#include "paths.h"

#include <QRegularExpression>

namespace AginoStore {

bool isValidAppId(const QString &id)
{
    static const QRegularExpression re(QStringLiteral("\\A[a-z0-9][a-z0-9._+-]{0,127}\\z"));
    return re.match(id).hasMatch();
}

bool isValidPackageName(const QString &name)
{
    static const QRegularExpression re(QStringLiteral("\\A[a-z0-9][a-z0-9+.-]{0,127}\\z"));
    return re.match(name).hasMatch();
}

bool isValidVersion(const QString &version)
{
    // Debian version characters; must not start with '-' so it can never look like an option.
    static const QRegularExpression re(QStringLiteral("\\A[A-Za-z0-9][A-Za-z0-9.+~:-]{0,127}\\z"));
    return re.match(version).hasMatch();
}

QHash<QString, QString> installedVersions(const QStringList &packageNames)
{
    QHash<QString, QString> out;
    QStringList valid;
    for (const QString &name : packageNames) {
        if (isValidPackageName(name))
            valid.append(name);
    }
    if (valid.isEmpty())
        return out;

    QStringList args{QStringLiteral("-W"),
                     QStringLiteral("--showformat=${Package}\\t${db:Status-Status}\\t${Version}\\n")};
    args += valid;
    // Exit status 1 just means that some of the names are unknown; parse stdout regardless.
    const ProcessResult result = runProcess(dpkgQueryPath(), args, QByteArray(), 10000);
    if (!result.started || result.timedOut)
        return out;
    const QList<QByteArray> lines = result.out.split('\n');
    for (const QByteArray &line : lines) {
        const QList<QByteArray> fields = line.split('\t');
        if (fields.size() != 3 || fields.at(1) != "installed")
            continue;
        const QString name = QString::fromUtf8(fields.at(0));
        if (!out.contains(name))
            out.insert(name, QString::fromUtf8(fields.at(2)));
    }
    return out;
}

bool dpkgVersionGreater(const QString &a, const QString &b)
{
    if (!isValidVersion(a) || !isValidVersion(b))
        return false;
    const ProcessResult result =
        runProcess(dpkgPath(), {QStringLiteral("--compare-versions"), a, QStringLiteral("gt"), b},
                   QByteArray(), 10000);
    return result.ok();
}

bool dpkgOwnsPath(const QString &path)
{
    const ProcessResult result = runProcess(dpkgQueryPath(), {QStringLiteral("-S"), path}, QByteArray(), 30000);
    // If dpkg-query cannot run, treat the file as owned: never delete on doubt.
    if (!result.started || result.timedOut)
        return true;
    return result.exitCode == 0;
}

} // namespace AginoStore
