// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Compact line of the home page category panels.
Control {
    id: row

    property string appId
    readonly property var info: Store.app(appId)

    topPadding: 8
    bottomPadding: 8

    background: Item {
        Rectangle {
            width: parent.width
            height: 1
            color: Surface.line(row.palette)
        }
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: Router.openApp(row.appId)
        }
    }

    contentItem: RowLayout {
        spacing: 10

        AppIcon {
            appId: row.appId
            size: 36
        }
        ColumnLayout {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            spacing: 0
            D.Label {
                Layout.fillWidth: true
                text: row.info.name
                font.weight: Font.Medium
                elide: Text.ElideRight
            }
            Muted {
                Layout.fillWidth: true
                text: row.info.summary
            }
        }
        ActionButtons { appId: row.appId }
    }
}
