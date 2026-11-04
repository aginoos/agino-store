// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// One entry of the navigation rail.
Control {
    id: item

    property string label
    property string iconKey
    property bool current: false
    property int badge: 0
    signal clicked()

    readonly property color fg: current ? palette.highlightedText : palette.windowText

    implicitHeight: 34
    leftPadding: 10
    rightPadding: 8

    background: Rectangle {
        radius: 8
        color: item.current ? item.palette.highlight
                            : (area.containsMouse ? Qt.alpha(item.palette.windowText, 0.07) : "transparent")
        MouseArea {
            id: area
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: item.clicked()
        }
    }

    contentItem: RowLayout {
        spacing: 10
        NavIcon {
            key: item.iconKey
            color: item.fg
        }
        D.Label {
            Layout.fillWidth: true
            text: item.label
            textFormat: Text.PlainText
            color: item.fg
            elide: Text.ElideRight
        }
        Rectangle {
            visible: item.badge > 0
            implicitWidth: Math.max(18, badgeText.implicitWidth + 10)
            implicitHeight: 18
            radius: 9
            color: item.current ? item.palette.highlightedText : "#e5484d"
            D.Label {
                id: badgeText
                anchors.centerIn: parent
                text: item.badge
                textFormat: Text.PlainText
                font.pixelSize: 11
                color: item.current ? item.palette.highlight : "#ffffff"
            }
        }
    }
}
