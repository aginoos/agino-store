// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// App details: header with the action, screenshots, description, release notes, facts.
PageBase {
    id: page

    readonly property string appId: Router.appId
    readonly property var info: {
        // Reading the counter inside the expression keeps the dependency (a bare
        // statement can be dropped by the QML compiler).
        return Store.stateTick >= 0 ? Store.app(appId) : ({})
    }
    // Store.revision is read inside the expression so a new catalog re-evaluates it.
    readonly property var detail: Store.revision !== undefined ? Store.appDetail(appId) : ({})
    readonly property var screenshots: detail.screenshots ? detail.screenshots : []
    readonly property var paragraphs: detail.description ? detail.description : []
    readonly property var notes: detail.releaseNotes ? detail.releaseNotes : []

    readonly property string categoryName: {
        const ids = info.categories ? info.categories : []
        const found = Store.categories.filter(function(c) { return ids.indexOf(c.id) >= 0 })
        return found.length > 0 ? found[0].name : ""
    }

    readonly property var rows: {
        const out = []
        const add = function(label, value, mono) {
            if (value !== undefined && value !== null && value !== "")
                out.push({ "label": label, "value": String(value), "mono": mono === true })
        }
        add(qsTr("Version"), info.version, true)
        if (info.downloadSize > 0)
            add(qsTr("Download size"), Store.formatBytes(info.downloadSize))
        if (info.installedSize > 0)
            add(qsTr("Installed size"), Store.formatBytes(info.installedSize))
        add(qsTr("Developer"), detail.developer)
        add(qsTr("License"), info.proprietary ? qsTr("Proprietary") : info.license)
        add(qsTr("Source"), Store.sourceText(appId))
        add(qsTr("Maintainer"), detail.maintainer, true)
        add(qsTr("Website"), detail.homepage)
        add(qsTr("Updated"), info.updated)
        if (info.source !== "system" && info.checksum)
            add(qsTr("Package checksum"),
                "sha256 " + info.checksum.slice(0, 12) + "…" + info.checksum.slice(-8), true)
        return out
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: 18

        AppIcon {
            Layout.alignment: Qt.AlignTop
            appId: page.appId
            size: 88
        }
        ColumnLayout {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            spacing: 2
            Heading {
                Layout.fillWidth: true
                text: page.info.name ? page.info.name : ""
                font.pixelSize: 22
            }
            Muted {
                Layout.fillWidth: true
                visible: text !== ""
                text: page.detail.developer ? page.detail.developer : ""
                font: D.DTK.fontManager.t7
            }
            Row {
                Layout.topMargin: 6
                spacing: 6
                SourceChips { appId: page.appId }
                Chip {
                    visible: page.categoryName !== ""
                    text: page.categoryName
                }
            }
        }
        ColumnLayout {
            Layout.alignment: Qt.AlignTop | Qt.AlignRight
            spacing: 6
            ActionButtons {
                Layout.alignment: Qt.AlignRight
                appId: page.appId
                big: true
            }
            Muted {
                Layout.alignment: Qt.AlignRight
                visible: page.info.downloadSize > 0
                text: qsTr("Download %1 · Installed size %2")
                      .arg(Store.formatBytes(page.info.downloadSize))
                      .arg(Store.formatBytes(page.info.installedSize))
            }
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        visible: page.screenshots.length > 0
        spacing: 12
        Heading { text: qsTr("Screenshots") }
        Screenshots {
            Layout.fillWidth: true
            Layout.preferredHeight: 200
            model: page.screenshots
        }
    }

    GridLayout {
        Layout.fillWidth: true
        columns: width > 760 ? 2 : 1
        columnSpacing: 24
        rowSpacing: 22

        ColumnLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            spacing: 10

            Heading {
                visible: page.paragraphs.length > 0
                text: qsTr("About")
            }
            Repeater {
                model: page.paragraphs
                D.Label {
                    required property string modelData
                    Layout.fillWidth: true
                    text: modelData
                    wrapMode: Text.Wrap
                }
            }

            Heading {
                Layout.topMargin: 8
                visible: page.notes.length > 0
                text: page.detail.releaseVersion ? qsTr("What’s new in version %1").arg(page.detail.releaseVersion)
                                                 : qsTr("What’s new in this version")
            }
            Repeater {
                model: page.notes
                D.Label {
                    required property string modelData
                    Layout.fillWidth: true
                    text: "•  " + modelData
                    wrapMode: Text.Wrap
                }
            }
        }

        InfoPanel {
            Layout.alignment: Qt.AlignTop
            Layout.preferredWidth: 300
            Layout.fillWidth: parent.columns === 1
            rows: page.rows
        }
    }
}
