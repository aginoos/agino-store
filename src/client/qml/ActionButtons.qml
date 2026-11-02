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
        Store.stateTick
        return Store.appState(appId)
    }

    spacing: 8

    D.ToolButton {
        visible: root.big && (root.st === "installed" || root.st === "update")
        text: qsTr("Uninstall")
        onClicked: Store.requestAction(root.appId, "remove")
    }
    D.Button {
        visible: root.st === "busy"
        enabled: false
        text: qsTr("In progress")
    }
    D.RecommandButton {
        visible: root.st === "update"
        text: qsTr("Update")
        onClicked: Store.requestAction(root.appId, "update")
    }
    D.Button {
        visible: root.st === "installed"
        text: qsTr("Open")
        onClicked: Store.launch(root.appId)
    }
    D.RecommandButton {
        visible: root.st === "none" && root.big
        text: qsTr("Install")
        onClicked: Store.requestAction(root.appId, "install")
    }
    D.Button {
        visible: root.st === "none" && !root.big
        text: qsTr("Install")
        onClicked: Store.requestAction(root.appId, "install")
    }
}
