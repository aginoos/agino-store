// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Shapes

// Stroked 20x20 line icon from an SVG path (see Icons.qml).
Shape {
    id: icon

    property string key
    property color color

    implicitWidth: 20
    implicitHeight: 20
    layer.enabled: true
    layer.samples: 4

    ShapePath {
        strokeColor: icon.color
        strokeWidth: 1.5
        fillColor: "transparent"
        capStyle: ShapePath.RoundCap
        joinStyle: ShapePath.RoundJoin
        PathSvg { path: Icons.forKey(icon.key) }
    }
}
