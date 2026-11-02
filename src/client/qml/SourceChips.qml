// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick

// Source tag (system repository / official / community) plus "Proprietary".
Row {
    id: root

    property string appId
    readonly property var info: Store.app(appId)

    spacing: 6

    Chip {
        accent: root.info.source !== "personal"
        text: root.info.source === "system" ? qsTr("System repo")
              : root.info.source === "official" ? qsTr("Official") : qsTr("Community")
    }
    Chip {
        visible: root.info.proprietary === true
        text: qsTr("Proprietary")
    }
}
