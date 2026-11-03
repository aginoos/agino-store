// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Jobs of this session: download, verify, install or remove.
PageBase {
    id: page

    Heading {
        text: qsTr("Downloads")
    }

    Control {
        id: box
        Layout.fillWidth: true
        visible: rows.count > 0
        padding: 0

        background: Rectangle {
            radius: 12
            color: Surface.card(box.palette)
            border.width: 1
            border.color: Surface.line(box.palette)
        }
        contentItem: ColumnLayout {
            spacing: 0
            Repeater {
                id: rows
                model: Store.tasks
                TaskRow {
                    jobId: model.jobId
                    appId: model.appId
                    kind: model.kind
                    stage: model.stage
                    percent: model.percent
                    done: model.done
                    total: model.total
                    finished: model.finished
                    ok: model.ok
                    message: model.message
                    separator: model.index > 0
                }
            }
        }
    }

    Muted {
        Layout.alignment: Qt.AlignHCenter
        Layout.topMargin: 40
        visible: rows.count === 0
        text: qsTr("Nothing is downloading")
    }
}
