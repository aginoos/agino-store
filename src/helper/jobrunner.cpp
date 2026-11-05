// SPDX-License-Identifier: GPL-3.0-or-later
#include "jobrunner.h"

#include "dpkgutil.h"
#include "fsutil.h"
#include "manifest.h"
#include "paths.h"
#include "pkclient.h"

#include <QDateTime>
#include <QDebug>
#include <QFile>
#include <QFileInfo>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QRegularExpression>
#include <QUrl>

#include <functional>
#include <memory>

namespace AginoStore {

namespace {

constexpr qint64 kMaxDebBytes = 8LL * 1024 * 1024 * 1024;
constexpr int kDownloadTimeoutMs = 60000;

// Resolves every name one after the other; a package that cannot be resolved is logged and left
// out (language packages never fail a job).
void resolveAll(PkClient *pk, const QStringList &names, std::function<void(const QList<ResolvedL10n> &)> done)
{
    auto result = std::make_shared<QList<ResolvedL10n>>();
    auto step = std::make_shared<std::function<void(int)>>();
    *step = [pk, names, done, result, step](int index) {
        if (index >= names.size()) {
            done(*result);
            return;
        }
        const QString name = names.at(index);
        pk->resolve(name, [name, result, step, index](const PkResolved &resolved) {
            if (resolved.ok)
                result->append({name, resolved});
            else
                qInfo().noquote() << "l10n package" << name << "skipped: cannot resolve:" << resolved.message;
            (*step)(index + 1);
        });
    };
    (*step)(0);
}

// Directories that a vendor package may use to add itself as an apt source (design 7.3.4).
QStringList sourceDirectories()
{
    return {QStringLiteral("/etc/apt/sources.list.d"), QStringLiteral("/etc/apt/trusted.gpg.d"),
            QStringLiteral("/usr/share/keyrings"), QStringLiteral("/etc/apt/keyrings")};
}

QStringList listSourceFiles()
{
    QStringList files;
    for (const QString &dir : sourceDirectories())
        files += listNonDirectories(dir);
    return files;
}

} // namespace

JobRunner::JobRunner(JobContext context, QNetworkAccessManager *nam, PkClient *pk, QObject *parent)
    : QObject(parent)
    , m_ctx(std::move(context))
    , m_nam(nam)
    , m_pk(pk)
{
}

void JobRunner::setStage(const QString &stage, uint percent, quint64 done, quint64 total)
{
    m_stage = stage;
    Q_EMIT progress(stage, percent, done, total);
}

void JobRunner::finish(bool ok, const QString &errorCode, const QString &message)
{
    if (m_finished)
        return;
    m_finished = true;
    m_cancellable = false;
    Q_EMIT finished(ok, errorCode, message);
}

QString JobRunner::debPath() const
{
    return debsDir() + QLatin1Char('/') + m_ctx.app.package.deb->sha256 + QStringLiteral(".deb");
}

void JobRunner::start()
{
    if (m_ctx.action == JobAction::Remove) {
        runRemove();
    } else if (m_ctx.app.package.kind == PackageRef::Kind::Milkyway) {
        runMilkywayInstall();
    } else {
        runDeb();
    }
}

void JobRunner::cancel()
{
    if (!m_cancellable || m_finished)
        return;
    m_cancelled = true;
    if (m_reply)
        m_reply->abort();
    // Not downloading yet (e.g. hashing a cached file): the flag is checked at the next step.
}

// --- remove -----------------------------------------------------------------------------

void JobRunner::runRemove()
{
    m_cancellable = false;
    const QString name = m_ctx.app.package.name;
    if (!isValidPackageName(name)) {
        finish(false, QStringLiteral("unknown-app"), QStringLiteral("invalid package name"));
        return;
    }
    if (m_ctx.config.isProtected(name)) {
        finish(false, QStringLiteral("protected-package"), QStringLiteral("%1 cannot be removed through the store").arg(name));
        return;
    }
    setStage(QStringLiteral("removing"));
    m_pk->resolve(name, [this, name](const PkResolved &resolved) {
        if (!resolved.ok) {
            finish(false, resolved.errorCode, resolved.message);
            return;
        }
        const QString mainInstalled = resolved.installedId;
        // The language packages depend on the app, so they have to go in the same transaction
        // (allow_deps is false): every installed package of every language in the catalog entry.
        QStringList l10n;
        for (const QString &package : allL10nPackages(m_ctx.app.l10n)) {
            if (isValidPackageName(package) && package != name)
                l10n.append(package);
        }
        resolveAll(m_pk, l10n, [this, mainInstalled](const QList<ResolvedL10n> &items) {
            QStringList ids;
            if (!mainInstalled.isEmpty())
                ids.append(mainInstalled);
            for (const ResolvedL10n &item : items) {
                if (!item.resolved.installedId.isEmpty())
                    ids.append(item.resolved.installedId);
            }
            if (ids.isEmpty()) {
                finish(true);    // already gone
                return;
            }
            m_pk->removePackages(ids, PackageKit::Transaction::TransactionFlagNone,
                                 [this](uint percent) { setStage(QStringLiteral("removing"), percent, percent, 100); },
                                 [this](const PkResult &result) {
                                     finish(result.ok, result.errorCode, result.message);
                                 });
        });
    });
}

// --- milkyway install / update --------------------------------------------------------

void JobRunner::runMilkywayInstall()
{
    m_cancellable = false;
    const QString name = m_ctx.app.package.name;
    if (!isValidPackageName(name)) {
        finish(false, QStringLiteral("unknown-app"), QStringLiteral("invalid package name"));
        return;
    }
    setStage(QStringLiteral("installing"));
    const bool update = m_ctx.action == JobAction::Update;
    m_pk->resolve(name, [this, update](const PkResolved &resolved) {
        if (!resolved.ok) {
            finish(false, resolved.errorCode, resolved.message);
            return;
        }
        if (resolved.availableId.isEmpty() && resolved.installedId.isEmpty()) {
            finish(false, QStringLiteral("packagekit-error"), QStringLiteral("package not found"));
            return;
        }
        resolveAll(m_pk, l10nPackages(), [this, update, resolved](const QList<ResolvedL10n> &items) {
            installResolved(update, resolved, items);
        });
    });
}

// The language packages for the system language, chosen from the verified catalog entry. The
// client only sends the language string; its own package list is never used.
QStringList JobRunner::l10nPackages() const
{
    QStringList out;
    for (const QString &package : l10nPackagesFor(m_ctx.app.l10n, m_ctx.lang)) {
        if (isValidPackageName(package) && package != m_ctx.app.package.name)
            out.append(package);
    }
    return out;
}

void JobRunner::installResolved(bool update, const PkResolved &app, const QList<ResolvedL10n> &l10n)
{
    QStringList missing;    // language packages that are not installed yet
    QStringList upgrades;   // installed ones with a newer candidate
    for (const ResolvedL10n &item : l10n) {
        if (item.resolved.availableId.isEmpty())
            continue;    // installed and current, or not available: nothing to do
        (item.resolved.installedId.isEmpty() ? missing : upgrades).append(item.resolved.availableId);
    }
    QStringList ids = missing;
    ids.append(upgrades);
    if (!app.availableId.isEmpty())
        ids.prepend(app.availableId);
    if (ids.isEmpty()) {
        finish(true);    // already installed / up to date
        return;
    }
    auto progress = [this](uint percent) { setStage(QStringLiteral("installing"), percent, percent, 100); };
    auto done = [this](const PkResult &result) { finish(result.ok, result.errorCode, result.message); };
    // A plain update is only possible when everything in it is installed already.
    if (update && !app.installedId.isEmpty() && missing.isEmpty())
        m_pk->updatePackages(ids, PackageKit::Transaction::TransactionFlagOnlyTrusted, progress, done);
    else
        m_pk->installPackages(ids, PackageKit::Transaction::TransactionFlagOnlyTrusted, progress, done);
}

// --- vendor deb --------------------------------------------------------------------------

void JobRunner::runDeb()
{
    if (m_ctx.app.package.isVendorDistributed()) {
        finish(false, QStringLiteral("vendor-download"), QStringLiteral("the app must be downloaded from the vendor"));
        return;
    }
    if (!m_ctx.app.package.deb || !isValidPackageName(m_ctx.app.package.name)) {
        finish(false, QStringLiteral("unknown-app"), QStringLiteral("the app has no deb to install"));
        return;
    }
    const DebInfo &deb = *m_ctx.app.package.deb;
    static const QRegularExpression sha(QStringLiteral("\\A[0-9a-f]{64}\\z"));
    const QUrl url(deb.url);
    const bool schemeOk = url.isValid() && m_ctx.config.isAllowedScheme(url.scheme());
    if (!sha.match(deb.sha256).hasMatch() || deb.size <= 0 || deb.size > kMaxDebBytes || !schemeOk) {
        finish(false, QStringLiteral("download-failed"), QStringLiteral("the catalog entry has an invalid deb description"));
        return;
    }
    QString error;
    if (!ensureDirectory(debsDir(), perm700(), &error)) {
        finish(false, QStringLiteral("download-failed"), error);
        return;
    }
    // A previously verified download can be reused.
    QFile existing(debPath());
    if (existing.exists()) {
        if (existing.size() == deb.size && existing.open(QIODevice::ReadOnly)
            && sha256Hex(existing) == deb.sha256) {
            existing.close();
            applyProfile();
            return;
        }
        existing.close();
        QFile::remove(debPath());
    }
    downloadDeb();
}

void JobRunner::downloadDeb()
{
    const DebInfo &deb = *m_ctx.app.package.deb;
    const QString partPath = debPath() + QStringLiteral(".part");
    QFile::remove(partPath);
    m_part.setFileName(partPath);
    if (!m_part.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        finish(false, QStringLiteral("download-failed"), QStringLiteral("cannot create %1: %2").arg(partPath, m_part.errorString()));
        return;
    }
    m_part.setPermissions(perm600());
    m_written = 0;
    m_downloadFailed = false;

    setStage(QStringLiteral("downloading"), 0, 0, static_cast<quint64>(deb.size));
    QNetworkRequest request{QUrl(deb.url)};
    request.setTransferTimeout(kDownloadTimeoutMs);
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::NoLessSafeRedirectPolicy);
    QNetworkReply *reply = m_nam->get(request);
    m_reply = reply;

    connect(reply, &QNetworkReply::readyRead, this, [this, reply, size = deb.size]() {
        if (m_downloadFailed)
            return;
        const QByteArray chunk = reply->readAll();
        m_written += chunk.size();
        if (m_written > size) {
            // Stop at once: the server sends more than the signed catalog says.
            m_downloadFailed = true;
            m_downloadErrorCode = QStringLiteral("hash-mismatch");
            m_downloadError = QStringLiteral("download is larger than the size in the catalog");
            reply->abort();
            return;
        }
        if (m_part.write(chunk) != chunk.size()) {
            m_downloadFailed = true;
            m_downloadErrorCode = QStringLiteral("download-failed");
            m_downloadError = QStringLiteral("cannot write the download: %1").arg(m_part.errorString());
            reply->abort();
        }
    });
    connect(reply, &QNetworkReply::downloadProgress, this, [this, size = deb.size](qint64 received, qint64) {
        const uint percent = static_cast<uint>(qBound(qint64(0), received * 100 / size, qint64(100)));
        if (!m_finished && m_stage == QLatin1String("downloading"))
            Q_EMIT progress(m_stage, percent, static_cast<quint64>(qMax<qint64>(received, 0)), static_cast<quint64>(size));
    });
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        reply->deleteLater();
        m_part.close();
        if (m_cancelled) {
            QFile::remove(m_part.fileName());
            finish(false, QStringLiteral("cancelled"), QStringLiteral("cancelled"));
            return;
        }
        if (m_downloadFailed) {
            QFile::remove(m_part.fileName());
            finish(false, m_downloadErrorCode, m_downloadError);
            return;
        }
        if (reply->error() != QNetworkReply::NoError) {
            QFile::remove(m_part.fileName());
            finish(false, QStringLiteral("download-failed"), reply->errorString());
            return;
        }
        verifyDeb();
    });
}

void JobRunner::verifyDeb()
{
    m_cancellable = false;
    const DebInfo &deb = *m_ctx.app.package.deb;
    setStage(QStringLiteral("verifying"));
    const QString partPath = m_part.fileName();
    QFile file(partPath);
    bool ok = file.open(QIODevice::ReadOnly) && file.size() == deb.size;
    // Hashing a large file blocks the event loop for a moment; D-Bus calls wait.
    ok = ok && sha256Hex(file) == deb.sha256;
    file.close();
    if (!ok) {
        QFile::remove(partPath);
        finish(false, QStringLiteral("hash-mismatch"), QStringLiteral("the downloaded file does not match the catalog"));
        return;
    }
    QFile::remove(debPath());
    if (!QFile::rename(partPath, debPath())) {
        QFile::remove(partPath);
        finish(false, QStringLiteral("download-failed"), QStringLiteral("cannot store the verified download"));
        return;
    }
    applyProfile();
}

bool JobRunner::applyInstallProfile(QString *error)
{
    if (!m_ctx.detail) {
        *error = QStringLiteral("the verified app description is not available");
        return false;
    }
    const InstallProfile &profile = m_ctx.detail->installProfile;
    // Validate everything first; apply nothing when any item is invalid.
    for (const QString &line : profile.debconf) {
        if (!isValidDebconfLine(line)) {
            *error = QStringLiteral("invalid debconf line");
            return false;
        }
    }
    for (const DefaultFile &file : profile.defaultFiles) {
        if (!isValidDefaultFile(file) || QFileInfo(file.path).isSymLink()) {
            *error = QStringLiteral("invalid /etc/default file: %1").arg(file.path);
            return false;
        }
    }
    if (!profile.debconf.isEmpty()) {
        const QByteArray input = profile.debconf.join(QLatin1Char('\n')).toUtf8() + '\n';
        const ProcessResult result = runProcess(debconfSetSelectionsPath(), {}, input, 30000);
        if (!result.ok()) {
            *error = QStringLiteral("debconf-set-selections failed: %1").arg(QString::fromUtf8(result.err).left(300));
            return false;
        }
    }
    for (const DefaultFile &file : profile.defaultFiles) {
        QString writeError;
        if (!writeFileAtomic(file.path, file.content, perm644(), &writeError)) {
            *error = writeError;
            return false;
        }
    }
    return true;
}

void JobRunner::applyProfile()
{
    m_cancellable = false;
    if (m_cancelled) {
        removeDownloadedDeb();
        finish(false, QStringLiteral("cancelled"), QStringLiteral("cancelled"));
        return;
    }
    setStage(QStringLiteral("installing"));
    QString error;
    if (!applyInstallProfile(&error)) {
        removeDownloadedDeb();
        finish(false, QStringLiteral("install-profile-invalid"), error);
        return;
    }
    installDeb();
}

void JobRunner::installDeb()
{
    m_dirsBefore = listSourceFiles();
    m_installStart = QDateTime::currentDateTime();
    // Not OnlyTrusted: a vendor deb is not signed by an apt key. Its hash comes from the signed catalog.
    m_pk->installFiles({debPath()}, PackageKit::Transaction::TransactionFlagNone,
                       [this](uint percent) { setStage(QStringLiteral("installing"), percent, percent, 100); },
                       [this](const PkResult &result) { afterInstall(result.ok, result.errorCode, result.message); });
}

void JobRunner::afterInstall(bool ok, const QString &errorCode, const QString &message)
{
    // The vendor package must not leave an apt source behind: delete new files without an owner.
    const QStringList after = listSourceFiles();
    for (const QString &path : after) {
        if (m_dirsBefore.contains(path))
            continue;
        // Only files created or changed since the transaction started can be ours.
        if (QFileInfo(path).lastModified() < m_installStart || dpkgOwnsPath(path))
            continue;
        if (QFile::remove(path))
            qWarning().noquote() << "removed unowned apt source file left by" << m_ctx.app.id << ":" << path;
        else
            qWarning().noquote() << "cannot remove unowned apt source file" << path;
    }
    removeDownloadedDeb();
    finish(ok, errorCode, message);
}

void JobRunner::removeDownloadedDeb()
{
    if (!m_ctx.keepPackage && m_ctx.app.package.deb)
        QFile::remove(debPath());
}

} // namespace AginoStore
