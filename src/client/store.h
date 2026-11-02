// SPDX-License-Identifier: GPL-3.0-or-later
// The client's backend facade for QML: verified catalog, per-app state, assets, helper calls
// and tasks (docs/design.md section 8).
#pragma once

#include "assetcache.h"
#include "catalogcache.h"
#include "helperclient.h"
#include "storeconfig.h"
#include "taskmodel.h"

#include <QAbstractItemModel>
#include <QHash>
#include <QObject>
#include <QTimer>
#include <QVariantList>
#include <QVariantMap>
#include <QtQml/qqmlregistration.h>

class QJSEngine;
class QQmlEngine;

class Store : public QObject
{
    Q_OBJECT
    QML_ELEMENT
    QML_SINGLETON

    // "loading", "ready", "expired" (verified but past its validity) or "unavailable".
    Q_PROPERTY(QString catalogState READ catalogState NOTIFY catalogReloaded)
    Q_PROPERTY(QString error READ error NOTIFY catalogReloaded)
    Q_PROPERTY(QString revision READ revision NOTIFY catalogReloaded)
    Q_PROPERTY(QVariantList categories READ categories NOTIFY catalogReloaded)
    Q_PROPERTY(QVariantList featured READ featured NOTIFY catalogReloaded)
    Q_PROPERTY(QString syncText READ syncText NOTIFY syncTextChanged)
    Q_PROPERTY(bool refreshing READ refreshing NOTIFY refreshingChanged)
    // Bumped whenever install state or the task set changes (not on progress).
    Q_PROPERTY(int stateTick READ stateTick NOTIFY stateTickChanged)
    // Bumped whenever an icon or screenshot finished downloading.
    Q_PROPERTY(int assetTick READ assetTick NOTIFY assetTickChanged)
    Q_PROPERTY(int updatesCount READ updatesCount NOTIFY stateTickChanged)
    Q_PROPERTY(int installedCount READ installedCount NOTIFY stateTickChanged)
    Q_PROPERTY(int activeTasks READ activeTasks NOTIFY stateTickChanged)
    Q_PROPERTY(QAbstractItemModel *tasks READ tasks CONSTANT)

public:
    static Store *instance();
    static Store *create(QQmlEngine *, QJSEngine *);

    QString catalogState() const { return m_state; }
    QString error() const { return m_error; }
    QString revision() const { return m_loaded.manifest.revision; }
    QVariantList categories() const { return m_categories; }
    QVariantList featured() const { return m_featured; }
    QString syncText() const;
    bool refreshing() const { return m_refreshing; }
    int stateTick() const { return m_stateTick; }
    int assetTick() const { return m_assetTick; }
    int updatesCount() const { return int(m_updates.size()); }
    int installedCount() const { return int(m_installed.size()); }
    int activeTasks() const { return m_tasks.activeCount(); }
    QAbstractItemModel *tasks() { return &m_tasks; }

    // For the list model.
    const AginoStore::Catalog &catalog() const { return m_loaded.catalog; }
    qint64 downloads(const QString &id) const { return m_loaded.stats.value(id, 0); }
    QString locale() const { return m_locale; }
    bool isInstalled(const QString &id) const { return m_installed.contains(id); }
    bool hasUpdate(const QString &id) const { return m_updates.contains(id); }

    Q_INVOKABLE void refresh();
    Q_INVOKABLE QVariantMap app(const QString &id) const;
    Q_INVOKABLE QVariantMap appDetail(const QString &id);
    // "none", "installed", "update" or "busy".
    Q_INVOKABLE QString appState(const QString &id) const;
    Q_INVOKABLE QString installedVersion(const QString &id) const;
    Q_INVOKABLE QString sourceText(const QString &id);
    Q_INVOKABLE QString iconUrl(const QString &id);
    Q_INVOKABLE QString assetUrl(const QString &relPath);
    Q_INVOKABLE QString formatBytes(double bytes) const;
    // Sum of the download sizes of the pending updates (deb apps only).
    Q_INVOKABLE qint64 updatesDownloadBytes() const;
    Q_INVOKABLE qint64 assetCacheBytes() const { return m_assets.sizeOnDisk(); }
    Q_INVOKABLE void clearAssetCache();

    // Simulate first, then confirmRequested(); confirmAction() runs the real job(s).
    Q_INVOKABLE void requestAction(const QString &appId, const QString &action);
    Q_INVOKABLE void requestUpdateAll();
    Q_INVOKABLE void confirmAction(const QVariantMap &request);
    Q_INVOKABLE void cancelTask(uint jobId);
    Q_INVOKABLE void launch(const QString &appId);
    Q_INVOKABLE void openSystemUpdates();

signals:
    void catalogReloaded();
    void syncTextChanged();
    void refreshingChanged();
    void stateTickChanged();
    void assetTickChanged();
    // request: appIds, action, batch, name, source, downloadBytes, installedDelta, extraInstall,
    // extraRemove (see storeactions.cpp).
    void confirmRequested(const QVariantMap &request);
    void toast(const QString &text);

private:
    explicit Store(QObject *parent = nullptr);

    void reloadCatalog();
    void applyState(const QString &json);
    void bumpState();
    QString packageName(const QString &appId) const;
    static QString friendlyError(const QString &code, const QString &message);

    void onSimulated(const QString &appId, const QString &action, const QString &json);
    void onJobStarted(uint jobId, const QString &appId, const QString &action);
    void maybeHandleFinished(uint jobId);
    void emitRequest(const QStringList &appIds, const QString &action, qint64 download, qint64 delta,
                     const QStringList &installs, const QStringList &removes);

    struct InstalledInfo {
        QString package;
        QString version;
    };
    struct UpdateInfo {
        QString installed;
        QString available;
    };
    struct Batch {
        bool active = false;
        QStringList ids;
        int remaining = 0;
        qint64 download = 0;
        qint64 delta = 0;
        QStringList installs;
        bool failed = false;   // one simulation failed: finish silently
    };

    StoreConfig m_config;
    CatalogCache m_cache;
    AssetCache m_assets;
    HelperClient m_helper;
    TaskModel m_tasks;
    QString m_locale;

    CatalogCache::Loaded m_loaded;
    QString m_state = QStringLiteral("loading");
    QString m_error;
    QVariantList m_categories;
    QVariantList m_featured;
    QHash<QString, AginoStore::AppDetail> m_details;
    QHash<QString, InstalledInfo> m_installed;
    QHash<QString, UpdateInfo> m_updates;
    QDateTime m_synced;
    QTimer m_syncTimer;
    bool m_refreshing = false;
    bool m_userRefresh = false;
    bool m_haveCatalog = false;
    int m_stateTick = 0;
    int m_assetTick = 0;
    QStringList m_pending;   // "<appId>:<action>" simulations in flight
    Batch m_batch;
};
