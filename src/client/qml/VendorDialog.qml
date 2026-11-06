// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Install or update of an app whose licence does not allow the store to distribute it
// (distribution "vendor"): the facts and a link to the vendor's website, no helper call.
D.Dialog {
    id: dialog

    // See Store::vendorDownloadRequested: appIds, action, name.
    property var request: ({})

    readonly property string appId: request.appIds ? request.appIds[0] : ""
    readonly property string appName: request.name ? request.name : ""
    // What the store tested in the sandbox: version, architecture, package format and the start of the sha256,
    // so that the file from the vendor can be compared with it.
    readonly property var info: appId !== "" ? Store.app(appId) : ({})
    readonly property string testedText: {
        const parts = []
        if (info.version) parts.push(info.version)
        if (info.arch) parts.push(info.arch)
        parts.push("deb")
        if (info.checksum) parts.push("sha256 " + info.checksum.substring(0, 12))
        return parts.join(" · ")
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
                appId: dialog.appId
                size: 40
            }
            Heading {
                Layout.fillWidth: true
                text: dialog.request.action === "update" ? qsTr("Update %1").arg(dialog.appName)
                                                         : qsTr("Install %1").arg(dialog.appName)
                elide: Text.ElideRight
            }
        }

        D.Label {
            Layout.fillWidth: true
            text: qsTr("This software's licence does not allow the store to distribute it. Please download and install it from the vendor's website.")
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }

        D.Label {
            Layout.fillWidth: true
            visible: dialog.testedText !== ""
            text: qsTr("Version we tested: %1").arg(dialog.testedText)
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.topMargin: 4
            spacing: 8
            Item { Layout.fillWidth: true }
            D.Button {
                text: qsTr("Close")
                onClicked: dialog.close()
            }
            D.RecommandButton {
                text: qsTr("Download from the vendor's website")
                onClicked: {
                    Store.openVendorDownload(dialog.appId)
                    dialog.close()
                }
            }
        }
    }
}
