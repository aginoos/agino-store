// SPDX-License-Identifier: GPL-3.0-or-later
#include "pkclient.h"

#include <PackageKit/daemon.h>

#include <memory>

using PackageKit::Daemon;
using PackageKit::Transaction;

namespace AginoStore {

QString mapPkError(Transaction::Error error)
{
    switch (error) {
    case Transaction::ErrorDepResolutionFailed:
    case Transaction::ErrorPackageConflicts:
    case Transaction::ErrorFileConflicts:
        return QStringLiteral("dependency-unresolvable");
    case Transaction::ErrorNoNetwork:
    case Transaction::ErrorPackageDownloadFailed:
    case Transaction::ErrorNoMoreMirrorsToTry:
    case Transaction::ErrorCannotFetchSources:
        return QStringLiteral("download-failed");
    case Transaction::ErrorCannotRemoveSystemPackage:
        return QStringLiteral("protected-package");
    case Transaction::ErrorTransactionCancelled:
        return QStringLiteral("cancelled");
    default:
        return QStringLiteral("packagekit-error");
    }
}

PkClient::PkClient(QObject *parent)
    : QObject(parent)
{
}

void PkClient::track(Transaction *transaction, Progress progress, Done done)
{
    auto result = std::make_shared<PkResult>();
    // The lambdas live as long as the transaction (it is their context object).
    connect(transaction, &Transaction::package, transaction,
            [result](Transaction::Info info, const QString &id, const QString &) {
                result->packages.append(PkPackage{info, id});
            });
    connect(transaction, &Transaction::details, transaction,
            [result](const PackageKit::Details &details) { result->details.append(details); });
    connect(transaction, &Transaction::errorCode, transaction,
            [result](Transaction::Error error, const QString &text) {
                result->errorCode = mapPkError(error);
                result->message = text;
            });
    if (progress) {
        connect(transaction, &Transaction::percentageChanged, transaction, [transaction, progress]() {
            const uint percent = transaction->percentage();
            if (percent <= 100)          // 101 means unknown
                progress(percent);
        });
    }
    connect(transaction, &Transaction::finished, transaction,
            [transaction, result, done](Transaction::Exit status, uint) {
                switch (status) {
                case Transaction::ExitSuccess:
                    result->ok = true;
                    break;
                case Transaction::ExitCancelled:
                case Transaction::ExitKilled:
                case Transaction::ExitCancelledPriority:
                    result->cancelled = true;
                    result->errorCode = QStringLiteral("cancelled");
                    break;
                default:
                    if (result->errorCode.isEmpty())
                        result->errorCode = QStringLiteral("packagekit-error");
                    if (result->message.isEmpty())
                        result->message = QStringLiteral("PackageKit transaction failed");
                    break;
                }
                transaction->deleteLater();
                done(*result);
            });
    // TODO(verify): when packagekitd is unreachable, PackageKit-Qt reports it through
    // errorCode (ErrorInternalError) followed by finished; confirm it never stays silent.
}

void PkClient::resolve(const QString &packageName, ResolvedDone done)
{
    auto resolved = std::make_shared<PkResolved>();
    // Two lookups: the newest candidate that is not installed, then the installed one.
    Transaction *first = Daemon::resolve(
        packageName, Transaction::Filters(Transaction::FilterNotInstalled | Transaction::FilterArch | Transaction::FilterNewest));
    track(first, nullptr, [this, packageName, resolved, done](const PkResult &available) {
        if (!available.ok) {
            resolved->errorCode = available.errorCode;
            resolved->message = available.message;
            done(*resolved);
            return;
        }
        for (const PkPackage &package : available.packages) {
            if (resolved->availableId.isEmpty() && package.info == Transaction::InfoAvailable)
                resolved->availableId = package.id;
        }
        Transaction *second = Daemon::resolve(
            packageName, Transaction::Filters(Transaction::FilterInstalled | Transaction::FilterArch));
        track(second, nullptr, [resolved, done](const PkResult &installed) {
            if (!installed.ok) {
                resolved->errorCode = installed.errorCode;
                resolved->message = installed.message;
                done(*resolved);
                return;
            }
            for (const PkPackage &package : installed.packages) {
                if (resolved->installedId.isEmpty() && package.info == Transaction::InfoInstalled)
                    resolved->installedId = package.id;
            }
            resolved->ok = true;
            done(*resolved);
        });
    });
}

void PkClient::installPackages(const QStringList &ids, Transaction::TransactionFlags flags,
                               Progress progress, Done done)
{
    track(Daemon::installPackages(ids, flags), progress, done);
}

void PkClient::updatePackages(const QStringList &ids, Transaction::TransactionFlags flags,
                              Progress progress, Done done)
{
    track(Daemon::updatePackages(ids, flags), progress, done);
}

void PkClient::removePackages(const QStringList &ids, Transaction::TransactionFlags flags,
                              Progress progress, Done done)
{
    // allowDeps=false: never remove other packages that depend on this one; autoRemove=true.
    track(Daemon::removePackages(ids, false, true, flags), progress, done);
}

void PkClient::installFiles(const QStringList &files, Transaction::TransactionFlags flags,
                            Progress progress, Done done)
{
    track(Daemon::installFiles(files, flags), progress, done);
}

void PkClient::getDetails(const QStringList &ids, Done done)
{
    track(Daemon::getDetails(ids), nullptr, done);
}

} // namespace AginoStore
