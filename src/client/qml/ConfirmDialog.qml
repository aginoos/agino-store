// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Facts before an install, update or uninstall: sizes, extra components, source.
// TODO(verify): D.Dialog is a styled QtQuick.Controls Dialog; if its default header shows up
// with an empty title, give it `header: null`.
D.Dialog {
    id: dialog

    // See Store::confirmRequested: appIds, action, batch, count, name, source, downloadBytes,
    // installedDelta, extraInstall, extraRemove.
    property var request: ({})

    readonly property string action: request.action ? request.action : ""
    readonly property bool batch: request.batch === true
    readonly property bool removing: action === "remove"
    readonly property string appName: request.name ? request.name : ""
    readonly property var extras: removing ? (request.extraRemove ? request.extraRemove : [])
                                           : (request.extraInstall ? request.extraInstall : [])
    readonly property real download: request.downloadBytes ? request.downloadBytes : 0
    readonly property real delta: request.installedDelta ? request.installedDelta : 0

    readonly property string titleText: {
        if (batch)
            return qsTr("Update %1 apps").arg(request.count)
        if (action === "install")
            return qsTr("Install %1").arg(appName)
        if (action === "update")
            return qsTr("Update %1").arg(appName)
        return qsTr("Uninstall %1").arg(appName)
    }
    readonly property string sizeText: {
        const dl = Store.formatBytes(download)
        const size = Store.formatBytes(Math.abs(delta))
        if (removing)
            return delta < 0 ? qsTr("Frees about %1").arg(size) : ""
        if (action === "install")
            return download > 0 ? qsTr("Downloads %1, uses %2 after install").arg(dl).arg(size)
                                : qsTr("Uses %1 after install").arg(size)
        const change = delta < 0 ? qsTr("frees %1").arg(size) : qsTr("uses %1 more").arg(size)
        return download > 0 ? qsTr("Downloads %1, %2").arg(dl).arg(change) : change
    }

    function show(req) {
        request = req
        open()
    }

    modal: true
    anchors.centerIn: Overlay.overlay
    width: Math.min(460, Overlay.overlay ? Overlay.overlay.width - 32 : 460)
    padding: 20

    contentItem: ColumnLayout {
        spacing: 12

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            AppIcon {
                visible: !dialog.batch
                appId: dialog.request.appIds ? dialog.request.appIds[0] : ""
                size: 40
            }
            Heading {
                Layout.fillWidth: true
                text: dialog.titleText
                elide: Text.ElideRight
            }
        }

        D.Label {
            Layout.fillWidth: true
            visible: text !== ""
            text: dialog.sizeText
            wrapMode: Text.Wrap
        }
        D.Label {
            Layout.fillWidth: true
            visible: !dialog.batch && !dialog.removing && dialog.request.source !== undefined
            text: qsTr("Source: %1").arg(dialog.request.source ? dialog.request.source : "")
            wrapMode: Text.Wrap
        }

        Control {
            id: deps
            Layout.fillWidth: true
            visible: dialog.removing ? dialog.extras.length > 0 : true
            padding: 10
            background: Rectangle {
                radius: 8
                color: Qt.alpha(deps.palette.windowText, 0.06)
            }
            contentItem: D.Label {
                font: D.DTK.fontManager.t8
                wrapMode: Text.Wrap
                text: dialog.extras.length === 0
                      ? qsTr("No extra components needed")
                      : (dialog.removing ? qsTr("Also removes components nothing else needs:")
                                         : qsTr("Also installs components (%1):").arg(dialog.extras.length))
                        + " " + dialog.extras.join(", ")
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.topMargin: 4
            spacing: 8
            Item { Layout.fillWidth: true }
            D.Button {
                text: qsTr("Cancel")
                onClicked: dialog.close()
            }
            D.WarningButton {
                visible: dialog.removing
                text: qsTr("Uninstall")
                onClicked: {
                    Store.confirmAction(dialog.request)
                    dialog.close()
                }
            }
            D.RecommandButton {
                visible: !dialog.removing
                text: dialog.action === "install" ? qsTr("Install") : qsTr("Update")
                onClicked: {
                    Store.confirmAction(dialog.request)
                    dialog.close()
                }
            }
        }
    }
}
