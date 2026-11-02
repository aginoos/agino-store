// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Text tabs with an accent underline on the current one.
Control {
    id: tabs

    property var labels: []
    property int current: 0
    signal picked(int index)

    implicitHeight: 38

    background: Rectangle {
        color: "transparent"
        Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: 1
            color: Qt.alpha(tabs.palette.windowText, 0.12)
        }
    }

    contentItem: RowLayout {
        spacing: 22
        Repeater {
            model: tabs.labels
            Control {
                id: tab
                required property int index
                required property string modelData
                readonly property bool selected: index === tabs.current

                Layout.fillHeight: true
                topPadding: 8
                bottomPadding: 8

                contentItem: D.Label {
                    text: tab.modelData
                    font: Qt.font({ family: D.DTK.fontManager.t6.family, pixelSize: D.DTK.fontManager.t6.pixelSize, weight: tab.selected ? Font.Medium : Font.Normal })
                    color: tab.selected ? tab.palette.windowText : Qt.alpha(tab.palette.windowText, 0.62)
                }
                background: Item {
                    Rectangle {
                        visible: tab.selected
                        anchors.bottom: parent.bottom
                        width: parent.width
                        height: 2
                        color: tab.palette.highlight
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: tabs.picked(tab.index)
                    }
                }
            }
        }
        Item { Layout.fillWidth: true }
    }
}
