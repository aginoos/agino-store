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
    // True only when `sub` is built by the caller as StyledText markup; every dynamic part of it
    // must go through escapeMarkup(). Plain text (the default) can never turn into rich text.
    property bool subStyled: false
    property bool separator: false
    default property alias actions: actionRow.data
    readonly property var info: Store.app(appId)

    function escapeMarkup(s) {
        return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    }

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
            color: Surface.line(item.palette)
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
                textFormat: Text.PlainText
                font.weight: Font.Medium
                elide: Text.ElideRight
            }
            Muted {
                Layout.fillWidth: true
                text: item.sub
                textFormat: item.subStyled ? Text.StyledText : Text.PlainText
            }
        }
        RowLayout {
            id: actionRow
            spacing: 10
        }
    }
}
