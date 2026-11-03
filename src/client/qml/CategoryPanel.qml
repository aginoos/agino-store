// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Home page panel of one category: five apps, popular or new.
Control {
    id: panel

    property string categoryId
    property string title
    property string mode: "hot"

    visible: list.count > 0
    leftPadding: 14
    rightPadding: 14
    topPadding: 12
    bottomPadding: 4

    background: Rectangle {
        radius: 12
        color: Surface.card(panel.palette)
        border.width: 1
        border.color: Surface.line(panel.palette)
    }

    AppListModel {
        id: list
        category: panel.categoryId
        sort: panel.mode
        limit: 5
    }

    contentItem: ColumnLayout {
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.bottomMargin: 8
            spacing: 10
            Heading {
                Layout.fillWidth: true
                text: panel.title
                font: Qt.font({ family: D.DTK.fontManager.t6.family, pixelSize: D.DTK.fontManager.t6.pixelSize, weight: Font.DemiBold })
            }
            SegmentedSwitch {
                mode: panel.mode
                onPicked: function(m) { panel.mode = m }
            }
            D.ToolButton {
                text: qsTr("See all")
                onClicked: Router.openCategory(panel.categoryId)
            }
        }

        Repeater {
            model: list
            AppRow {
                appId: model.appId
                Layout.fillWidth: true
            }
        }
    }
}
