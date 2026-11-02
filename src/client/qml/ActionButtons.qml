// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import org.deepin.dtk 1.0 as D

// Install / Update / Open / Uninstall for one app, following its state.
Row {
    id: root

    property string appId
    // Detail page: also offers Uninstall and uses the emphasised install button.
    property bool big: false
    readonly property string st: {
        // Reading the counter inside the expression keeps the dependency (a bare
        // statement can be dropped by the QML compiler).
        return Store.stateTick >= 0 ? Store.appState(appId) : ""
    }

    // DTK buttons default to a wide implicit width that overflows the app cards.
    readonly property int buttonWidth: big ? 120 : 80

    spacing: 8

    D.ToolButton {
        implicitWidth: root.buttonWidth
        visible: root.big && (root.st === "installed" || root.st === "update")
        text: qsTr("Uninstall")
        onClicked: Store.requestAction(root.appId, "remove")
    }
    D.Button {
        implicitWidth: root.buttonWidth
        visible: root.st === "busy"
        enabled: false
        text: qsTr("In progress")
    }
    D.RecommandButton {
        implicitWidth: root.buttonWidth
        visible: root.st === "update"
        text: qsTr("Update")
        onClicked: Store.requestAction(root.appId, "update")
    }
    D.Button {
        implicitWidth: root.buttonWidth
        visible: root.st === "installed"
        text: qsTr("Open")
        onClicked: Store.launch(root.appId)
    }
    D.RecommandButton {
        implicitWidth: root.buttonWidth
        visible: root.st === "none" && root.big
        text: qsTr("Install")
        onClicked: Store.requestAction(root.appId, "install")
    }
    D.Button {
        implicitWidth: root.buttonWidth
        visible: root.st === "none" && !root.big
        text: qsTr("Install")
        onClicked: Store.requestAction(root.appId, "install")
    }
}
