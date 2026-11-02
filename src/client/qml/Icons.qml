// SPDX-License-Identifier: GPL-3.0-or-later
pragma Singleton
import QtQuick

// SVG path data (20x20 grid, stroked) for the navigation rail.
QtObject {
    readonly property var paths: ({
        "home": "M3 9.5 10 4l7 5.5V16a1 1 0 0 1-1 1h-3.5v-4.5h-5V17H4a1 1 0 0 1-1-1z",
        "office": "M6 3h6l4 4v10H6zM12 3v4h4M8.5 11h5M8.5 14h5",
        "chat": "M4 5h12v8H9l-3.5 3v-3H4z",
        "web": "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM3 10h14M10 3c2.2 2 2.2 12 0 14M10 3c-2.2 2-2.2 12 0 14",
        "dev": "m7 6-4 4 4 4M13 6l4 4-4 4",
        "media": "M5 4h10a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zM8.5 7.5v5l4-2.5z",
        "graphics": "M4 16c3 0 3-3 5-3l5-7a1.6 1.6 0 0 0-2.5-2l-6 6c-.5 2-1.5 3-1.5 6z",
        "games": "M6 7h8a3 3 0 0 1 3 3v2.5a2.5 2.5 0 0 1-4.5 1.5L12 13H8l-.5 1A2.5 2.5 0 0 1 3 12.5V10a3 3 0 0 1 3-3zM6.5 9.5v2.5M5.2 10.8h2.6",
        "education": "M10 4 2.5 7.5 10 11l7.5-3.5zM5.5 9v4c1.5 1.5 3 2 4.5 2s3-.5 4.5-2V9M17.5 7.5V12",
        "science": "M8 3h4M8.5 3v5l-4.3 7.3A1.1 1.1 0 0 0 5.2 17h9.6a1.1 1.1 0 0 0 1-1.7L11.5 8V3M6.5 12.5h7",
        "tools": "M12.5 3.5a3.5 3.5 0 0 0-3.3 4.6L3.8 13.5l2.7 2.7 5.4-5.4a3.5 3.5 0 0 0 4.6-3.3l-2 2-2.4-.6-.6-2.4z",
        "installed": "M3.5 6.5 10 3l6.5 3.5v7L10 17l-6.5-3.5zM3.5 6.5 10 10l6.5-3.5M10 10v7",
        "updates": "M15.5 8A6 6 0 0 0 4.6 7M4.5 12a6 6 0 0 0 10.9 1M4.5 3.5V7H8M15.5 16.5V13H12",
        "tasks": "M10 3v9M6.5 8.5 10 12l3.5-3.5M4 15.5h12",
        "generic": "M4 4h5v5H4zM11 4h5v5h-5zM4 11h5v5H4zM11 11h5v5h-5z"
    })

    function forKey(key) {
        return paths[key] !== undefined ? paths[key] : paths["generic"]
    }
}
