// SPDX-License-Identifier: GPL-3.0-or-later
// Filtered, sorted view of the catalog's apps for the QML lists.
#pragma once

#include <QAbstractListModel>
#include <QStringList>
#include <QTimer>
#include <QtQml/qqmlregistration.h>

class AppListModel : public QAbstractListModel
{
    Q_OBJECT
    QML_ELEMENT

    // Empty = every category.
    Q_PROPERTY(QString category READ category WRITE setCategory NOTIFY categoryChanged)
    Q_PROPERTY(QString query READ query WRITE setQuery NOTIFY queryChanged)
    // "hot" (downloads), "new" (updated date) or "name".
    Q_PROPERTY(QString sort READ sort WRITE setSort NOTIFY sortChanged)
    // 0 = no limit.
    Q_PROPERTY(int limit READ limit WRITE setLimit NOTIFY limitChanged)
    // Non-empty: exactly these apps, in this order (ignores category, query and sort).
    Q_PROPERTY(QStringList appIds READ appIds WRITE setAppIds NOTIFY appIdsChanged)
    // "", "update" or "installed" (installed includes apps with an update). The two
    // visibility settings do not apply to these.
    Q_PROPERTY(QString stateFilter READ stateFilter WRITE setStateFilter NOTIFY stateFilterChanged)
    Q_PROPERTY(int count READ count NOTIFY countChanged)

public:
    enum Role {
        AppIdRole = Qt::UserRole + 1,
        NameRole,
        SummaryRole,
        SourceRole,
        ProprietaryRole,
        StateRole,
        VersionRole,
        InstalledVersionRole,
        DownloadSizeRole,
        InstalledSizeRole,
    };

    explicit AppListModel(QObject *parent = nullptr);

    int rowCount(const QModelIndex &parent = {}) const override;
    QVariant data(const QModelIndex &index, int role) const override;
    QHash<int, QByteArray> roleNames() const override;

    QString category() const { return m_category; }
    QString query() const { return m_query; }
    QString sort() const { return m_sort; }
    int limit() const { return m_limit; }
    QStringList appIds() const { return m_appIds; }
    QString stateFilter() const { return m_stateFilter; }
    int count() const { return int(m_ids.size()); }

    void setCategory(const QString &value);
    void setQuery(const QString &value);
    void setSort(const QString &value);
    void setLimit(int value);
    void setAppIds(const QStringList &value);
    void setStateFilter(const QString &value);

signals:
    void categoryChanged();
    void queryChanged();
    void sortChanged();
    void limitChanged();
    void appIdsChanged();
    void stateFilterChanged();
    void countChanged();

private:
    void scheduleRebuild();
    void rebuild();
    void refreshStates();

    QString m_category;
    QString m_query;
    QString m_sort = QStringLiteral("hot");
    int m_limit = 0;
    QStringList m_appIds;
    QString m_stateFilter;
    QStringList m_ids;
    QTimer m_rebuildTimer;
};
