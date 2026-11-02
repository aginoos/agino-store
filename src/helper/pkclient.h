// SPDX-License-Identifier: GPL-3.0-or-later
// Thin asynchronous wrapper over PackageKit-Qt6 (docs/design.md section 7).
#pragma once

#include <PackageKit/details.h>
#include <PackageKit/transaction.h>

#include <QList>
#include <QObject>
#include <QString>
#include <QStringList>

#include <functional>

namespace AginoStore {

struct PkPackage {
    PackageKit::Transaction::Info info = PackageKit::Transaction::InfoUnknown;
    QString id;                       // "name;version;arch;data"
};

struct PkResult {
    bool ok = false;
    bool cancelled = false;
    QString errorCode;                // design.md error code when !ok
    QString message;
    QList<PkPackage> packages;
    QList<PackageKit::Details> details;
};

// Result of resolving one package name.
struct PkResolved {
    bool ok = false;
    QString errorCode;
    QString message;
    QString availableId;              // newest not-yet-installed (or newer) candidate
    QString installedId;
};

class PkClient : public QObject
{
    Q_OBJECT
public:
    using Progress = std::function<void(uint percent)>;
    using Done = std::function<void(const PkResult &)>;
    using ResolvedDone = std::function<void(const PkResolved &)>;

    explicit PkClient(QObject *parent = nullptr);

    void resolve(const QString &packageName, ResolvedDone done);
    void installPackages(const QStringList &ids, PackageKit::Transaction::TransactionFlags flags,
                         Progress progress, Done done);
    void updatePackages(const QStringList &ids, PackageKit::Transaction::TransactionFlags flags,
                        Progress progress, Done done);
    void removePackages(const QStringList &ids, PackageKit::Transaction::TransactionFlags flags,
                        Progress progress, Done done);
    void installFiles(const QStringList &files, PackageKit::Transaction::TransactionFlags flags,
                      Progress progress, Done done);
    void getDetails(const QStringList &ids, Done done);

private:
    void track(PackageKit::Transaction *transaction, Progress progress, Done done);
};

QString mapPkError(PackageKit::Transaction::Error error);

} // namespace AginoStore
