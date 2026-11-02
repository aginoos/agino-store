// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import org.deepin.dtk 1.0 as D

// Popular / New toggle of the home page panels.
D.ButtonBox {
    id: root

    property string mode: "hot"
    signal picked(string mode)

    // TODO(verify): ButtonBox takes checkable buttons as direct children.
    D.ToolButton {
        text: qsTr("Popular")
        checkable: true
        checked: root.mode === "hot"
        onClicked: root.picked("hot")
    }
    D.ToolButton {
        text: qsTr("New")
        checkable: true
        checked: root.mode === "new"
        onClicked: root.picked("new")
    }
}
