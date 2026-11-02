// SPDX-License-Identifier: GPL-3.0-or-later
#include "taskmodel.h"

TaskModel::TaskModel(QObject *parent)
    : QAbstractListModel(parent)
{
}

int TaskModel::rowCount(const QModelIndex &parent) const
{
    return parent.isValid() ? 0 : int(m_tasks.size());
}

QVariant TaskModel::data(const QModelIndex &index, int role) const
{
    if (!index.isValid() || index.row() < 0 || index.row() >= m_tasks.size())
        return {};
    const Task &t = m_tasks.at(index.row());
    switch (role) {
    case JobIdRole: return t.jobId;
    case AppIdRole: return t.appId;
    case KindRole: return t.kind;
    case StageRole: return t.stage;
    case PercentRole: return t.percent;
    case DoneRole: return t.done;
    case TotalRole: return t.total;
    case FinishedRole: return t.finished;
    case OkRole: return t.ok;
    case ErrorCodeRole: return t.errorCode;
    case MessageRole: return t.message;
    default: return {};
    }
}

QHash<int, QByteArray> TaskModel::roleNames() const
{
    return {
        {JobIdRole, "jobId"},       {AppIdRole, "appId"},       {KindRole, "kind"},
        {StageRole, "stage"},       {PercentRole, "percent"},   {DoneRole, "done"},
        {TotalRole, "total"},       {FinishedRole, "finished"}, {OkRole, "ok"},
        {ErrorCodeRole, "errorCode"}, {MessageRole, "message"},
    };
}

int TaskModel::indexOf(uint jobId) const
{
    for (int i = 0; i < m_tasks.size(); ++i) {
        if (m_tasks.at(i).jobId == jobId)
            return i;
    }
    return -1;
}

int TaskModel::ensure(uint jobId, const QString &appId)
{
    int row = indexOf(jobId);
    if (row >= 0)
        return row;
    row = int(m_tasks.size());
    beginInsertRows({}, row, row);
    Task t;
    t.jobId = jobId;
    t.appId = appId;
    m_tasks.append(t);
    endInsertRows();
    return row;
}

void TaskModel::touch(int row)
{
    emit dataChanged(index(row), index(row));
}

void TaskModel::started(uint jobId, const QString &appId, const QString &kind)
{
    const int row = ensure(jobId, appId);
    m_tasks[row].kind = kind;
    if (m_tasks[row].appId.isEmpty())
        m_tasks[row].appId = appId;
    touch(row);
}

void TaskModel::progress(uint jobId, const QString &appId, const QString &stage, uint percent,
                         qulonglong done, qulonglong total)
{
    const int row = ensure(jobId, appId);
    Task &t = m_tasks[row];
    if (t.finished)
        return;
    t.stage = stage;
    t.percent = percent;
    t.done = done;
    t.total = total;
    touch(row);
}

void TaskModel::finished(uint jobId, const QString &appId, bool ok, const QString &errorCode,
                         const QString &message)
{
    const int row = ensure(jobId, appId);
    Task &t = m_tasks[row];
    t.finished = true;
    t.ok = ok;
    t.errorCode = errorCode;
    t.message = message;
    t.percent = ok ? 100 : t.percent;
    touch(row);
}

void TaskModel::markHandled(uint jobId)
{
    const int row = indexOf(jobId);
    if (row >= 0)
        m_tasks[row].handled = true;
}

const TaskModel::Task *TaskModel::find(uint jobId) const
{
    const int row = indexOf(jobId);
    return row >= 0 ? &m_tasks.at(row) : nullptr;
}

bool TaskModel::isBusy(const QString &appId) const
{
    for (const Task &t : m_tasks) {
        if (!t.finished && t.appId == appId)
            return true;
    }
    return false;
}

int TaskModel::activeCount() const
{
    int n = 0;
    for (const Task &t : m_tasks)
        n += t.finished ? 0 : 1;
    return n;
}

QList<uint> TaskModel::activeJobs() const
{
    QList<uint> jobs;
    for (const Task &t : m_tasks) {
        if (!t.finished)
            jobs.append(t.jobId);
    }
    return jobs;
}
