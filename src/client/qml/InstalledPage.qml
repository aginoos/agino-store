// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Updates (store-managed apps) and installed apps.
PageBase {
    id: page

    property int tab: 0

    AppListModel {
        id: updates
        stateFilter: "update"
        sort: "name"
    }
    AppListModel {
        id: installed
        stateFilter: "installed"
        sort: "name"
    }

    readonly property real updateBytes: {
        // Reading the counter inside the expression keeps the dependency (a bare
        // statement can be dropped by the QML compiler).
        return Store.stateTick >= 0 ? Store.updatesDownloadBytes() : 0
    }

    RowLayout {
        Layout.fillWidth: true
        TabHeader {
            Layout.fillWidth: true
            labels: [qsTr("Updates (%1)").arg(updates.count), qsTr("Installed (%1)").arg(installed.count)]
            current: page.tab
            onPicked: function(i) { page.tab = i }
        }
        D.Button {
            text: qsTr("System updates")
            onClicked: Store.openSystemUpdates()
        }
    }

    // Updates tab
    RowLayout {
        Layout.fillWidth: true
        visible: page.tab === 0 && updates.count > 0
        Muted {
            Layout.fillWidth: true
            text: page.updateBytes > 0 ? "%1 · %2".arg(updates.count).arg(Store.formatBytes(page.updateBytes))
                                       : String(updates.count)
        }
        D.RecommandButton {
            text: qsTr("Update all")
            onClicked: Store.requestUpdateAll()
        }
    }

    ListColumn {
        visible: page.tab === 0 && updates.count > 0
        Layout.fillWidth: true
        listModel: updates
        delegate: ListItem {
            appId: model.appId
            separator: model.index > 0
            sub: "%1 → %2".arg(model.installedVersion).arg(model.version)
                 + (model.downloadSize > 0 ? " · " + Store.formatBytes(model.downloadSize) : "")
            ActionButtons { appId: model.appId }
        }
    }

    // Installed tab
    ListColumn {
        visible: page.tab === 1 && installed.count > 0
        Layout.fillWidth: true
        listModel: installed
        delegate: ListItem {
            appId: model.appId
            separator: model.index > 0
            sub: model.installedVersion + (model.installedSize > 0 ? " · " + Store.formatBytes(model.installedSize) : "")
            Chip {
                accent: model.source !== "personal"
                text: model.source === "system" ? qsTr("Updates with system") : qsTr("Store update")
            }
            D.Button {
                visible: model.state !== "busy"
                text: qsTr("Uninstall")
                onClicked: Store.requestAction(model.appId, "remove")
            }
        }
    }

    Muted {
        Layout.alignment: Qt.AlignHCenter
        Layout.topMargin: 40
        visible: (page.tab === 0 && updates.count === 0) || (page.tab === 1 && installed.count === 0)
        text: page.tab === 0 ? "✓" : qsTr("Nothing installed yet")
        font.pixelSize: page.tab === 0 ? 28 : 14
    }
}
