// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import org.deepin.dtk 1.0 as D

// Secondary text in a softer shade of the palette's text colour.
D.Label {
    color: Qt.alpha(palette.windowText, 0.62)
    font: D.DTK.fontManager.t8
    elide: Text.ElideRight
    // Catalog and PackageKit strings are untrusted: never let Qt guess rich text (remote <img>, spoofed UI).
    textFormat: Text.PlainText
}
