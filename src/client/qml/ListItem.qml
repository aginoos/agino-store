// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Row of a ListColumn: icon, name, one detail line; children land on the right.
Control {
    id: item

    property string appId
    property string sub
    property bool separator: false
    default property alias actions: actionRow.data
    readonly property var info: Store.app(appId)

    Layout.fillWidth: true
    leftPadding: 14
    rightPadding: 14
    topPadding: 12
    bottomPadding: 12

    background: Item {
        Rectangle {
            visible: item.separator
            width: parent.width
            height: 1
            color: Qt.alpha(item.palette.windowText, 0.1)
        }
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: Router.openApp(item.appId)
        }
    }

    contentItem: RowLayout {
        spacing: 12

        AppIcon {
            appId: item.appId
            size: 40
        }
        ColumnLayout {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            spacing: 1
            D.Label {
                Layout.fillWidth: true
                text: item.info.name ? item.info.name : ""
                font.weight: Font.Medium
                elide: Text.ElideRight
            }
            Muted {
                Layout.fillWidth: true
                text: item.sub
            }
        }
        RowLayout {
            id: actionRow
            spacing: 10
        }
    }
}
