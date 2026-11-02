// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Shown when the cached catalog is missing or fails verification.
Item {
    ColumnLayout {
        anchors.centerIn: parent
        spacing: 12

        Heading {
            Layout.alignment: Qt.AlignHCenter
            text: qsTr("Catalog unavailable")
        }
        Muted {
            Layout.alignment: Qt.AlignHCenter
            visible: text !== ""
            text: Store.error
        }
        D.RecommandButton {
            Layout.alignment: Qt.AlignHCenter
            text: qsTr("Refresh now")
            enabled: !Store.refreshing
            onClicked: Store.refresh()
        }
    }
}
