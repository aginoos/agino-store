// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Layouts

PageBase {
    id: page

    AppListModel {
        id: list
        query: Router.query
        sort: "hot"
    }

    Heading {
        text: qsTr("Results for “%1”").arg(Router.query)
    }

    AppGrid {
        Layout.fillWidth: true
        listModel: list
    }

    Muted {
        Layout.alignment: Qt.AlignHCenter
        Layout.topMargin: 30
        visible: list.count === 0
        text: qsTr("No matching apps")
    }
}
