// SPDX-License-Identifier: GPL-3.0-or-later
// Jobs of the download tasks page: one row per helper job, newest last.
#pragma once

#include <QAbstractListModel>
#include <QList>
#include <QString>

class TaskModel : public QAbstractListModel
{
    Q_OBJECT
public:
    enum Role {
        JobIdRole = Qt::UserRole + 1,
        AppIdRole,
        KindRole,       // install, update, remove (empty until the call returns)
        StageRole,      // waiting, downloading, verifying, installing, removing
        PercentRole,
        DoneRole,
        TotalRole,
        FinishedRole,
        OkRole,
        ErrorCodeRole,
        MessageRole,
    };

    struct Task {
        uint jobId = 0;
        QString appId;
        QString kind;
        QString stage = QStringLiteral("waiting");
        uint percent = 0;
        qulonglong done = 0;
        qulonglong total = 0;
        bool finished = false;
        bool ok = false;
        QString errorCode;
        QString message;
        bool handled = false;   // JobFinished already acted on by the Store
    };

    explicit TaskModel(QObject *parent = nullptr);

    int rowCount(const QModelIndex &parent = {}) const override;
    QVariant data(const QModelIndex &index, int role) const override;
    QHash<int, QByteArray> roleNames() const override;

    // Rows are created by whichever of call reply / signal arrives first.
    void started(uint jobId, const QString &appId, const QString &kind);
    void progress(uint jobId, const QString &appId, const QString &stage, uint percent,
                  qulonglong done, qulonglong total);
    void finished(uint jobId, const QString &appId, bool ok, const QString &errorCode,
                  const QString &message);
    void markHandled(uint jobId);

    const Task *find(uint jobId) const;
    bool isBusy(const QString &appId) const;
    int activeCount() const;
    QList<uint> activeJobs() const;

private:
    int indexOf(uint jobId) const;
    int ensure(uint jobId, const QString &appId);
    void touch(int row);

    QList<Task> m_tasks;
};
