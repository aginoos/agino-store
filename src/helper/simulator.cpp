// SPDX-License-Identifier: GPL-3.0-or-later
#include "simulator.h"

#include "dpkgutil.h"
#include "paths.h"
#include "pkclient.h"

#include <PackageKit/daemon.h>

#include <QFileInfo>
#include <QHash>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRegularExpression>
#include <QSet>

#include <memory>

using PackageKit::Daemon;
using PackageKit::Transaction;

namespace AginoStore {

namespace {

struct Plan {
    QStringList installIds;
    QStringList removeIds;
};

Plan planFrom(const PkResult &result)
{
    Plan plan;
    for (const PkPackage &package : result.packages) {
        switch (package.info) {
        case Transaction::InfoInstalling:
        case Transaction::InfoInstall:
        case Transaction::InfoReinstalling:
        case Transaction::InfoUpdating:
        case Transaction::InfoDowngrading:
        case Transaction::InfoDowngrade:
            if (!plan.installIds.contains(package.id))
                plan.installIds.append(package.id);
            break;
        case Transaction::InfoRemoving:
        case Transaction::InfoRemove:
        case Transaction::InfoObsoleting:
        case Transaction::InfoObsolete:
            if (!plan.removeIds.contains(package.id))
                plan.removeIds.append(package.id);
            break;
        default:
            break;
        }
    }
    return plan;
}

QJsonArray entries(const QStringList &ids)
{
    QJsonArray array;
    for (const QString &id : ids) {
        array.append(QJsonObject{{QStringLiteral("name"), Daemon::packageName(id)},
                                 {QStringLiteral("version"), Daemon::packageVersion(id)}});
    }
    return array;
}

// Package ids of local files carry a "local" data field and are unknown to the repositories.
bool isLocalId(const QString &id)
{
    return Daemon::packageData(id).startsWith(QLatin1String("local"));
}

// Simulated transactions mark package ids with the reason ("+auto:REPO", "manual:REPO");
// GetDetails only knows the plain repository id "name;version;arch;REPO".
QString repoId(const QString &id)
{
    QStringList parts = id.split(QLatin1Char(';'));
    if (parts.size() != 4)
        return id;
    const qsizetype colon = parts[3].lastIndexOf(QLatin1Char(':'));
    if (colon >= 0)
        parts[3] = parts[3].mid(colon + 1);
    return parts.join(QLatin1Char(';'));
}

// Adds sizes from PackageKit details (when available) and the catalog's deb numbers.
void finishPlan(PkClient *pk, const AppSummary &app, const Plan &plan, bool debInPlan, SimulateDone done)
{
    QStringList lookup;
    for (const QString &id : plan.installIds + plan.removeIds) {
        if (!isLocalId(id))
            lookup.append(repoId(id));
    }
    auto emitResult = [app, plan, debInPlan, done](const PkResult &details) {
        QHash<QString, qulonglong> size;
        QHash<QString, qulonglong> download;
        for (const PackageKit::Details &d : details.details) {
            // size() is the installed size; the archive size is only in the raw map (PackageKit
            // >= 1.2 sends "download-size", PackageKit-Qt 1.1 has no accessor for it).
            size.insert(d.packageId(), d.size());
            download.insert(d.packageId(), d.value(QStringLiteral("download-size")).toULongLong());
        }
        qint64 downloadBytes = 0;
        qint64 delta = 0;
        for (const QString &id : plan.installIds) {
            downloadBytes += static_cast<qint64>(download.value(repoId(id)));
            delta += static_cast<qint64>(size.value(repoId(id)));
        }
        for (const QString &id : plan.removeIds)
            delta -= static_cast<qint64>(size.value(repoId(id)));
        if (debInPlan && app.package.deb) {
            // The vendor deb is not in any repository: use the numbers from the catalog.
            downloadBytes += app.package.deb->size;
            // An update replaces the installed version: count only the difference.
            delta += app.package.deb->installedSize - installedSizeBytes(app.package.name);
        }
        const QJsonObject json{{QStringLiteral("install"), entries(plan.installIds)},
                               {QStringLiteral("remove"), entries(plan.removeIds)},
                               {QStringLiteral("download_bytes"), downloadBytes},
                               {QStringLiteral("installed_delta"), delta}};
        done(true, QString(), QString::fromUtf8(QJsonDocument(json).toJson(QJsonDocument::Compact)));
    };
    if (lookup.isEmpty()) {
        emitResult(PkResult());
        return;
    }
    // Sizes are best effort: a failed lookup leaves them at 0.
    pk->getDetails(lookup, emitResult);
}

} // namespace

void simulateAction(PkClient *pk, const HelperConfig &config, const AppSummary &app, const QString &action,
                    SimulateDone done)
{
    const bool install = action == QLatin1String("install");
    const bool update = action == QLatin1String("update");
    const bool remove = action == QLatin1String("remove");
    const QString name = app.package.name;
    if ((!install && !update && !remove) || !isValidPackageName(name)) {
        done(false, QStringLiteral("unknown-app"), QStringLiteral("invalid action or package"));
        return;
    }
    if (remove && config.isProtected(name)) {
        done(false, QStringLiteral("protected-package"), QStringLiteral("%1 cannot be removed through the store").arg(name));
        return;
    }

    const Transaction::TransactionFlags simulateTrusted =
        Transaction::TransactionFlagSimulate | Transaction::TransactionFlagOnlyTrusted;

    if (!remove && app.package.kind == PackageRef::Kind::Deb && app.package.deb) {
        const DebInfo &deb = *app.package.deb;
        const QString cached = debsDir() + QLatin1Char('/') + deb.sha256 + QStringLiteral(".deb");
        const bool haveFile = QRegularExpression(QStringLiteral("\\A[0-9a-f]{64}\\z")).match(deb.sha256).hasMatch()
            && QFileInfo(cached).isFile() && QFileInfo(cached).size() == deb.size;
        auto basic = [pk, app, done]() {
            Plan plan;
            plan.installIds = {QStringLiteral("%1;%2;;local").arg(app.package.name, app.package.deb->version)};
            finishPlan(pk, app, plan, true, done);
        };
        if (!haveFile) {
            // TODO(verify): dependencies of a deb that is not downloaded yet are unknown here.
            basic();
            return;
        }
        pk->installFiles({cached}, Transaction::TransactionFlagSimulate, nullptr,
                         [pk, app, done, basic](const PkResult &result) {
                             if (!result.ok) {
                                 basic();
                                 return;
                             }
                             Plan plan = planFrom(result);
                             if (plan.installIds.isEmpty())
                                 plan.installIds = {QStringLiteral("%1;%2;;local").arg(app.package.name, app.package.deb->version)};
                             finishPlan(pk, app, plan, true, done);
                         });
        return;
    }

    pk->resolve(name, [pk, app, done, install, update, remove, simulateTrusted](const PkResolved &resolved) {
        if (!resolved.ok) {
            done(false, resolved.errorCode, resolved.message);
            return;
        }
        const QString id = remove ? resolved.installedId : resolved.availableId;
        if (id.isEmpty()) {
            if (!remove && resolved.installedId.isEmpty()) {
                done(false, QStringLiteral("packagekit-error"), QStringLiteral("package not found"));
            } else {
                // Nothing to do (already installed / up to date / not installed).
                finishPlan(pk, app, Plan(), false, done);
            }
            return;
        }
        auto onResult = [pk, app, done](const PkResult &result) {
            if (!result.ok) {
                done(false, result.errorCode, result.message);
                return;
            }
            finishPlan(pk, app, planFrom(result), false, done);
        };
        if (remove) {
            pk->removePackages({id}, Transaction::TransactionFlagSimulate, nullptr, onResult);
        } else if (update && !resolved.installedId.isEmpty()) {
            pk->updatePackages({id}, simulateTrusted, nullptr, onResult);
        } else {
            Q_UNUSED(install)
            pk->installPackages({id}, simulateTrusted, nullptr, onResult);
        }
    });
}

} // namespace AginoStore
