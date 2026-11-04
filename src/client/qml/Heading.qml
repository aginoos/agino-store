// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import org.deepin.dtk 1.0 as D

D.Label {
    font: Qt.font({ family: D.DTK.fontManager.t5.family, pixelSize: 17, weight: Font.DemiBold })
    elide: Text.ElideRight
    // Titles come from the catalog and from the user's search: always plain text.
    textFormat: Text.PlainText
}
