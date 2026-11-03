// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// App tile of the grid views: icon, name, summary, tags and the action button.
Control {
    id: card

    property string appId
    readonly property var info: Store.app(appId)

    padding: 14

    background: Rectangle {
        radius: 12
        color: Surface.card(card.palette)
        border.width: 1
        border.color: area.containsMouse ? Qt.alpha(card.palette.highlight, 0.55)
                                         : Surface.line(card.palette)
        MouseArea {
            id: area
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: Router.openApp(card.appId)
        }
    }

    contentItem: GridLayout {
        columns: 2
        columnSpacing: 12
        rowSpacing: 4

        AppIcon {
            Layout.rowSpan: 2
            Layout.alignment: Qt.AlignTop
            appId: card.appId
            size: 48
        }
        D.Label {
            Layout.fillWidth: true
            text: card.info.name
            font.weight: Font.Medium
            elide: Text.ElideRight
        }
        Muted {
            Layout.fillWidth: true
            Layout.minimumHeight: font.pixelSize > 0 ? Math.ceil(font.pixelSize * 2.6) : 32
            text: card.info.summary
            wrapMode: Text.Wrap
            maximumLineCount: 2
            verticalAlignment: Text.AlignTop
        }
        RowLayout {
            Layout.columnSpan: 2
            Layout.fillWidth: true
            Layout.topMargin: 8
            SourceChips { appId: card.appId }
            Item { Layout.fillWidth: true }
            ActionButtons { appId: card.appId }
        }
    }
}
