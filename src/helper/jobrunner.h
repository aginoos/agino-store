// SPDX-License-Identifier: GPL-3.0-or-later
// Runs one install / update / remove job (docs/design.md section 7).
#pragma once

#include "catalog.h"
#include "config.h"
#include "pkclient.h"

#include <QDateTime>
#include <QFile>
#include <QObject>
#include <QPointer>
#include <QStringList>

#include <optional>

class QNetworkAccessManager;
class QNetworkReply;

namespace AginoStore {

class PkClient;

enum class JobAction { Install, Update, Remove };

// A language package of the app and what PackageKit knows about it.
struct ResolvedL10n {
    QString name;
    PkResolved resolved;
};

struct JobContext {
    HelperConfig config;
    AppSummary app;
    std::optional<AppDetail> detail;     // required for deb installs / updates
    JobAction action = JobAction::Install;
    bool keepPackage = false;
    QString lang;                        // system locale name from the client (validated), e.g. zh_CN
};

class JobRunner : public QObject
{
    Q_OBJECT
public:
    JobRunner(JobContext context, QNetworkAccessManager *nam, PkClient *pk, QObject *parent = nullptr);

    void start();
    // Only effective before installation starts; see canCancel().
    void cancel();
    bool canCancel() const { return m_cancellable; }
    // "waiting" is handled by the service; this is the stage of the running job.
    QString stage() const { return m_stage; }

Q_SIGNALS:
    void progress(const QString &stage, uint percent, quint64 done, quint64 total);
    void finished(bool ok, const QString &errorCode, const QString &message);

private:
    void setStage(const QString &stage, uint percent = 0, quint64 done = 0, quint64 total = 0);
    void finish(bool ok, const QString &errorCode = QString(), const QString &message = QString());

    void runRemove();
    void runMilkywayInstall();
    QStringList l10nPackages() const;
    void installResolved(bool update, const PkResolved &app, const QList<ResolvedL10n> &l10n);
    void runDeb();
    void downloadDeb();
    void verifyDeb();
    void applyProfile();
    void installDeb();
    void afterInstall(bool ok, const QString &errorCode, const QString &message);
    bool applyInstallProfile(QString *error);
    void removeDownloadedDeb();
    QString debPath() const;

    JobContext m_ctx;
    QNetworkAccessManager *m_nam;
    PkClient *m_pk;
    QString m_stage = QStringLiteral("waiting");
    bool m_cancellable = true;
    bool m_cancelled = false;
    bool m_finished = false;

    QPointer<QNetworkReply> m_reply;
    QFile m_part;
    qint64 m_written = 0;
    bool m_downloadFailed = false;
    QString m_downloadError;
    QString m_downloadErrorCode;

    QDateTime m_installStart;     // when PackageKit was asked to install
    QStringList m_dirsBefore;     // files in the apt source related directories before installing
};

} // namespace AginoStore
