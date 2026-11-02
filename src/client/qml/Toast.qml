// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Short message at the bottom of the window, with a shortcut to the installed list.
Control {
    id: toast

    property string text

    function show(message) {
        text = message
        visible = true
        hideTimer.restart()
    }

    visible: false
    z: 10
    leftPadding: 16
    rightPadding: 16
    topPadding: 8
    bottomPadding: 8

    Timer {
        id: hideTimer
        interval: 4000
        onTriggered: toast.visible = false
    }

    background: Rectangle {
        radius: 10
        color: toast.palette.windowText
    }
    contentItem: RowLayout {
        spacing: 14
        D.Label {
            text: toast.text
            color: toast.palette.window
            elide: Text.ElideRight
            Layout.maximumWidth: 480
        }
        D.Label {
            text: qsTr("Installed")
            color: toast.palette.window
            font.underline: true
            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    toast.visible = false
                    Router.go("installed")
                }
            }
        }
    }
}
