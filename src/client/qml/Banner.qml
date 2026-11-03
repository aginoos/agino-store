// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Home page banner of one featured collection, with page dots for the others.
Control {
    id: banner

    // Featured entries with banner: true ({title, subtitle, apps}).
    property var items: []
    property int index: 0
    readonly property var current: items.length > 0 ? items[Math.min(index, items.length - 1)] : null

    readonly property bool hasSubtitle: current !== null && !!current.subtitle && current.subtitle !== ""

    padding: 28

    background: Rectangle {
        radius: 16
        clip: true
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: Qt.darker(banner.palette.highlight, 1.2) }
            GradientStop { position: 1.0; color: Qt.lighter(banner.palette.highlight, 1.25) }
        }
        // Decorative circle, partly off-canvas.
        Rectangle {
            x: parent.width - 280
            y: -80
            width: 340
            height: 340
            radius: 170
            color: Qt.rgba(1, 1, 1, 0.08)
        }
    }

    contentItem: ColumnLayout {
        spacing: 12

        GridLayout {
            Layout.fillWidth: true
            Layout.minimumHeight: 150
            columns: width > 560 ? 2 : 1
            columnSpacing: 20
            rowSpacing: 16

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 6
                D.Label {
                    // With no subtitle the title becomes the headline below.
                    visible: text !== ""
                    text: banner.hasSubtitle ? banner.current.title : ""
                    color: Qt.alpha(banner.palette.highlightedText, 0.85)
                    font: D.DTK.fontManager.t8
                }
                D.Label {
                    Layout.fillWidth: true
                    text: banner.current ? (banner.hasSubtitle ? banner.current.subtitle : banner.current.title) : ""
                    color: banner.palette.highlightedText
                    font: Qt.font({ family: D.DTK.fontManager.t2.family, pixelSize: D.DTK.fontManager.t2.pixelSize, weight: Font.DemiBold })
                    wrapMode: Text.Wrap
                }
                D.Button {
                    id: go
                    Layout.topMargin: 8
                    text: qsTr("Explore")
                    // White pill with dark text, whatever the accent.
                    contentItem: Text {
                        text: go.text
                        font.weight: Font.Medium
                        color: "#112233"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        implicitWidth: 88
                        implicitHeight: 32
                        radius: height / 2
                        color: go.down ? "#e6e9ee" : "#ffffff"
                    }
                    onClicked: {
                        const apps = banner.current ? banner.current.apps : []
                        const cats = apps.length > 0 ? Store.app(apps[0]).categories : []
                        if (cats && cats.length > 0)
                            Router.openCategory(cats[0])
                    }
                }
            }

            Row {
                Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                spacing: 10
                Repeater {
                    model: banner.current ? banner.current.apps.slice(0, 5) : []
                    AppIcon {
                        required property string modelData
                        appId: modelData
                        size: 56
                    }
                }
            }
        }

        Row {
            Layout.alignment: Qt.AlignHCenter
            visible: banner.items.length > 1
            spacing: 6
            Repeater {
                model: banner.items.length
                Rectangle {
                    required property int index
                    width: index === banner.index ? 22 : 8
                    height: 8
                    radius: 4
                    color: index === banner.index ? banner.palette.highlightedText
                                                  : Qt.alpha(banner.palette.highlightedText, 0.4)
                    MouseArea {
                        anchors.fill: parent
                        anchors.margins: -4
                        cursorShape: Qt.PointingHandCursor
                        onClicked: banner.index = parent.index
                    }
                }
            }
        }
    }
}
