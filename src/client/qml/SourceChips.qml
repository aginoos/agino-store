// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick

// Source tag (system repository / official / community) plus "Proprietary".
Row {
    id: root

    property string appId
    readonly property var info: Store.app(appId)

    spacing: 6

    Chip {
        tone: root.info.source === "official" ? "ok" : root.info.source === "personal" ? "warn" : "accent"
        text: root.info.source === "system" ? qsTr("System repo")
              : root.info.source === "official" ? qsTr("Official") : qsTr("Community")
    }
    Chip {
        visible: root.info.proprietary === true
        tone: "closed"
        text: qsTr("Proprietary")
    }
}
