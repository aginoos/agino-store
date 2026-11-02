// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// One helper job on the downloads page.
Control {
    id: row

    property int jobId
    property string appId
    property string kind
    property string stage
    property int percent
    property real done
    property real total
    property bool finished
    property bool ok
    property string message
    property bool separator: false

    readonly property var info: Store.app(appId)
    readonly property bool running: !finished && (stage === "downloading" || stage === "verifying"
                                                  || stage === "installing" || stage === "removing")
    readonly property string stageText: {
        if (finished) {
            if (!ok)
                return qsTr("Failed")
            return kind === "remove" ? qsTr("Removed") : qsTr("Done")
        }
        switch (stage) {
        case "downloading": return qsTr("Downloading")
        case "verifying": return qsTr("Verifying")
        case "installing": return qsTr("Installing")
        case "removing": return qsTr("Removing")
        default: return qsTr("Waiting")
        }
    }
    readonly property string detailText: {
        if (finished)
            return ok ? (info.version ? info.version : "") : message
        if (stage === "downloading" && total > 0)
            return Store.formatBytes(done) + " / " + Store.formatBytes(total)
        if (stage === "installing" || stage === "removing")
            return info.package ? info.package : ""
        if (stage === "waiting" && info.downloadSize > 0)
            return Store.formatBytes(info.downloadSize)
        return ""
    }

    Layout.fillWidth: true
    leftPadding: 14
    rightPadding: 14
    topPadding: 12
    bottomPadding: 12

    background: Rectangle {
        visible: row.separator
        height: 1
        width: parent.width
        color: Qt.alpha(row.palette.windowText, 0.1)
    }

    contentItem: RowLayout {
        spacing: 12

        AppIcon {
            appId: row.appId
            size: 40
        }
        ColumnLayout {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            spacing: 1
            D.Label {
                Layout.fillWidth: true
                text: row.info.name ? row.info.name : row.appId
                font.weight: Font.Medium
                elide: Text.ElideRight
            }
            Muted {
                Layout.fillWidth: true
                visible: text !== ""
                text: row.detailText
            }
            D.ProgressBar {
                Layout.fillWidth: true
                Layout.maximumWidth: 420
                Layout.topMargin: 4
                visible: !row.finished
                from: 0
                to: 100
                value: row.stage === "verifying" ? 100 : row.percent
                indeterminate: (row.stage === "installing" || row.stage === "removing") && row.percent === 0
            }
        }
        D.ToolButton {
            visible: !row.finished && (row.stage === "waiting" || row.stage === "downloading")
            text: qsTr("Cancel")
            onClicked: Store.cancelTask(row.jobId)
        }
        D.Label {
            text: row.stageText
            font: Qt.font({ family: D.DTK.fontManager.t8.family, pixelSize: D.DTK.fontManager.t8.pixelSize, weight: Font.Medium })
            color: row.finished && !row.ok ? row.palette.windowText
                                           : (row.finished ? row.palette.highlight : Qt.alpha(row.palette.windowText, 0.7))
        }
    }
}
