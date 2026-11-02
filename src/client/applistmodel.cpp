// SPDX-License-Identifier: GPL-3.0-or-later
#include "applistmodel.h"

#include "appsettings.h"
#include "store.h"

#include <algorithm>

using namespace AginoStore;

AppListModel::AppListModel(QObject *parent)
    : QAbstractListModel(parent)
{
    m_rebuildTimer.setSingleShot(true);
    m_rebuildTimer.setInterval(0);
    connect(&m_rebuildTimer, &QTimer::timeout, this, &AppListModel::rebuild);

    Store *store = Store::instance();
    connect(store, &Store::catalogReloaded, this, &AppListModel::scheduleRebuild);
    connect(AppSettings::instance(), &AppSettings::changed, this, &AppListModel::scheduleRebuild);
    // Install state changes the state filter's membership as well as the per-row state.
    connect(store, &Store::stateTickChanged, this, [this]() {
        if (m_stateFilter.isEmpty())
            refreshStates();
        else
            scheduleRebuild();
    });
    scheduleRebuild();
}

int AppListModel::rowCount(const QModelIndex &parent) const
{
    return parent.isValid() ? 0 : int(m_ids.size());
}

QHash<int, QByteArray> AppListModel::roleNames() const
{
    return {
        {AppIdRole, "appId"},
        {NameRole, "name"},
        {SummaryRole, "summary"},
        {SourceRole, "source"},
        {ProprietaryRole, "proprietary"},
        {StateRole, "state"},
        {VersionRole, "version"},
        {InstalledVersionRole, "installedVersion"},
        {DownloadSizeRole, "downloadSize"},
        {InstalledSizeRole, "installedSize"},
    };
}

QVariant AppListModel::data(const QModelIndex &index, int role) const
{
    if (!index.isValid() || index.row() < 0 || index.row() >= m_ids.size())
        return {};
    const QString &id = m_ids.at(index.row());
    if (role == AppIdRole)
        return id;
    const QVariantMap app = Store::instance()->app(id);
    switch (role) {
    case NameRole: return app.value(QStringLiteral("name"));
    case SummaryRole: return app.value(QStringLiteral("summary"));
    case SourceRole: return app.value(QStringLiteral("source"));
    case ProprietaryRole: return app.value(QStringLiteral("proprietary"));
    case StateRole: return app.value(QStringLiteral("state"));
    case VersionRole: return app.value(QStringLiteral("version"));
    case InstalledVersionRole: return app.value(QStringLiteral("installedVersion"));
    case DownloadSizeRole: return app.value(QStringLiteral("downloadSize"));
    case InstalledSizeRole: return app.value(QStringLiteral("installedSize"));
    default: return {};
    }
}

#define APPLIST_SETTER(Type, Name, member, signal)  \
    void AppListModel::set##Name(Type value)        \
    {                                               \
        if (member == value)                        \
            return;                                 \
        member = value;                             \
        emit signal();                              \
        scheduleRebuild();                          \
    }

APPLIST_SETTER(const QString &, Category, m_category, categoryChanged)
APPLIST_SETTER(const QString &, Query, m_query, queryChanged)
APPLIST_SETTER(const QString &, Sort, m_sort, sortChanged)
APPLIST_SETTER(int, Limit, m_limit, limitChanged)
APPLIST_SETTER(const QStringList &, AppIds, m_appIds, appIdsChanged)
APPLIST_SETTER(const QString &, StateFilter, m_stateFilter, stateFilterChanged)

#undef APPLIST_SETTER

void AppListModel::scheduleRebuild()
{
    if (!m_rebuildTimer.isActive())
        m_rebuildTimer.start();
}

void AppListModel::refreshStates()
{
    if (m_ids.isEmpty())
        return;
    emit dataChanged(index(0), index(int(m_ids.size()) - 1),
                     {StateRole, VersionRole, InstalledVersionRole});
}

void AppListModel::rebuild()
{
    Store *store = Store::instance();
    const Catalog &catalog = store->catalog();
    AppSettings *settings = AppSettings::instance();
    const QString locale = store->locale();
    const QString needle = m_query.trimmed().toLower();

    QStringList ids;
    if (!m_appIds.isEmpty()) {
        for (const QString &id : std::as_const(m_appIds)) {
            const AppSummary *a = catalog.app(id);
            if (!a)
                continue;
            if ((a->source == Source::Personal && !settings->showPersonal())
                || (a->proprietary && !settings->showProprietary()))
                continue;
            ids.append(id);
        }
    } else {
        QList<const AppSummary *> picked;
        for (const AppSummary &a : catalog.apps) {
            if (!m_category.isEmpty() && !a.categories.contains(m_category))
                continue;
            if (!m_stateFilter.isEmpty()) {
                const bool wanted = m_stateFilter == QLatin1String("update") ? store->hasUpdate(a.id)
                                                                              : store->isInstalled(a.id);
                if (!wanted)
                    continue;
            } else {
                if (a.source == Source::Personal && !settings->showPersonal())
                    continue;
                if (a.proprietary && !settings->showProprietary())
                    continue;
            }
            if (!needle.isEmpty()) {
                bool hit = a.id.toLower().contains(needle);
                for (auto it = a.name.values.cbegin(); !hit && it != a.name.values.cend(); ++it)
                    hit = it.value().toLower().contains(needle);
                for (auto it = a.summary.values.cbegin(); !hit && it != a.summary.values.cend(); ++it)
                    hit = it.value().toLower().contains(needle);
                if (!hit)
                    continue;
            }
            picked.append(&a);
        }
        const QString sortMode = m_sort;
        std::stable_sort(picked.begin(), picked.end(), [&](const AppSummary *x, const AppSummary *y) {
            if (sortMode == QLatin1String("name"))
                return x->name.pick(locale).localeAwareCompare(y->name.pick(locale)) < 0;
            const qint64 dx = store->downloads(x->id);
            const qint64 dy = store->downloads(y->id);
            if (sortMode == QLatin1String("new")) {
                if (x->updated != y->updated)
                    return x->updated > y->updated;
                return dx > dy;
            }
            if (dx != dy)
                return dx > dy;
            return x->updated > y->updated;
        });
        for (const AppSummary *a : std::as_const(picked))
            ids.append(a->id);
    }
    if (m_limit > 0 && ids.size() > m_limit)
        ids = ids.mid(0, m_limit);

    const int before = int(m_ids.size());
    beginResetModel();
    m_ids = ids;
    endResetModel();
    if (before != m_ids.size())
        emit countChanged();
}
