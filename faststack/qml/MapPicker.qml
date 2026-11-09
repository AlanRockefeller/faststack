pragma ComponentBehavior: Bound
import QtQuick 2.15
import QtQuick.Controls 2.15

// A small slippy map drawn from cached OpenStreetMap tiles
// (image://osmtile/z/x/y, served by faststack.inat.providers).
// Drag to pan, wheel or +/- to zoom, click to place the marker.
Item {
    id: map
    clip: true

    property int zoom: 13
    property real centerLat: 37.8715
    property real centerLon: -122.2730
    property bool hasMarker: false
    property real markerLat: 0
    property real markerLon: 0
    property color textColor: "white"

    signal picked(real lat, real lon)

    readonly property int tileSize: 256
    readonly property int minZoom: 2
    readonly property int maxZoom: 19

    function lonToX(lon, z) { return (lon + 180) / 360 * Math.pow(2, z) }
    function latToY(lat, z) {
        var clamped = Math.max(-85.05112878, Math.min(85.05112878, lat))
        var rad = clamped * Math.PI / 180
        return (1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * Math.pow(2, z)
    }
    function xToLon(x, z) { return x / Math.pow(2, z) * 360 - 180 }
    function yToLat(y, z) {
        var n = Math.PI - 2 * Math.PI * y / Math.pow(2, z)
        return 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)))
    }

    // Pixel position of a lon/lat relative to the map's top-left corner.
    function pixelX(lon) { return (lonToX(lon, zoom) - lonToX(centerLon, zoom)) * tileSize + width / 2 }
    function pixelY(lat) { return (latToY(lat, zoom) - latToY(centerLat, zoom)) * tileSize + height / 2 }
    function lonAt(px) { return xToLon(lonToX(centerLon, zoom) + (px - width / 2) / tileSize, zoom) }
    function latAt(py) { return yToLat(latToY(centerLat, zoom) + (py - height / 2) / tileSize, zoom) }

    function centerOn(lat, lon, z) {
        centerLat = lat
        centerLon = lon
        if (z !== undefined) zoom = Math.max(minZoom, Math.min(maxZoom, z))
        refreshTiles()
    }

    function setMarker(lat, lon) {
        hasMarker = true
        markerLat = lat
        markerLon = lon
    }

    function zoomAt(delta, px, py) {
        var newZoom = Math.max(minZoom, Math.min(maxZoom, zoom + delta))
        if (newZoom === zoom) return
        // Keep the point under the cursor fixed.
        var lon = lonAt(px)
        var lat = latAt(py)
        zoom = newZoom
        var cx = lonToX(lon, zoom) - (px - width / 2) / tileSize
        var cy = latToY(lat, zoom) - (py - height / 2) / tileSize
        centerLon = xToLon(cx, zoom)
        centerLat = yToLat(cy, zoom)
        refreshTiles()
    }

    // Tiles covering the viewport (a one-tile margin so panning shows no gaps).
    property var tileList: []
    function refreshTiles() {
        if (width <= 0 || height <= 0) return
        var n = Math.pow(2, zoom)
        var cx = lonToX(centerLon, zoom)
        var cy = latToY(centerLat, zoom)
        var halfW = width / 2 / tileSize
        var halfH = height / 2 / tileSize
        var x0 = Math.floor(cx - halfW) - 1
        var x1 = Math.floor(cx + halfW) + 1
        var y0 = Math.max(0, Math.floor(cy - halfH) - 1)
        var y1 = Math.min(n - 1, Math.floor(cy + halfH) + 1)
        var list = []
        for (var ty = y0; ty <= y1; ty++) {
            for (var tx = x0; tx <= x1; tx++) {
                var wrapped = ((tx % n) + n) % n
                list.push({ z: zoom, x: wrapped, y: ty, px: (tx - cx) * tileSize + width / 2, py: (ty - cy) * tileSize + height / 2 })
            }
        }
        tileList = list
    }

    onWidthChanged: refreshTiles()
    onHeightChanged: refreshTiles()
    Component.onCompleted: refreshTiles()

    Rectangle { anchors.fill: parent; color: "#1c1c1c" }

    Repeater {
        model: map.tileList
        delegate: Image {
            required property var modelData
            x: modelData.px
            y: modelData.py
            width: map.tileSize
            height: map.tileSize
            asynchronous: true
            cache: true
            source: "image://osmtile/" + modelData.z + "/" + modelData.x + "/" + modelData.y
            Rectangle {
                anchors.fill: parent
                color: "#262626"
                visible: parent.status !== Image.Ready
                Text { anchors.centerIn: parent; text: "…"; color: "#555555" }
            }
        }
    }

    // Marker
    Item {
        visible: map.hasMarker
        x: map.pixelX(map.markerLon)
        y: map.pixelY(map.markerLat)
        z: 5
        Rectangle {
            width: 16; height: 16; radius: 8
            x: -8; y: -8
            color: "#e53935"
            border.color: "white"; border.width: 2
        }
        Rectangle { width: 2; height: 14; x: -1; y: 6; color: "white" }
    }

    // Crosshair at the centre so the user can see where "centre" is.
    Rectangle { x: map.width / 2 - 10; y: map.height / 2; width: 20; height: 1; color: "#80ffffff" }
    Rectangle { x: map.width / 2; y: map.height / 2 - 10; width: 1; height: 20; color: "#80ffffff" }

    MouseArea {
        id: panArea
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton
        hoverEnabled: true
        property real pressX: 0
        property real pressY: 0
        property real startCx: 0
        property real startCy: 0
        property bool moved: false
        cursorShape: pressed ? Qt.ClosedHandCursor : Qt.CrossCursor

        onPressed: function(mouse) {
            pressX = mouse.x; pressY = mouse.y; moved = false
            startCx = map.lonToX(map.centerLon, map.zoom)
            startCy = map.latToY(map.centerLat, map.zoom)
        }
        onPositionChanged: function(mouse) {
            if (!pressed) return
            var dx = mouse.x - pressX
            var dy = mouse.y - pressY
            if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true
            if (!moved) return
            map.centerLon = map.xToLon(startCx - dx / map.tileSize, map.zoom)
            map.centerLat = map.yToLat(startCy - dy / map.tileSize, map.zoom)
            map.refreshTiles()
        }
        onReleased: function(mouse) {
            if (!moved) {
                var lat = map.latAt(mouse.y)
                var lon = map.lonAt(mouse.x)
                map.setMarker(lat, lon)
                map.picked(lat, lon)
            }
        }
        onDoubleClicked: function(mouse) { map.zoomAt(1, mouse.x, mouse.y) }
        onWheel: function(wheel) {
            var step = wheel.angleDelta.y > 0 ? 1 : -1
            map.zoomAt(step, wheel.x, wheel.y)
            wheel.accepted = true
        }
    }

    // Zoom buttons
    Column {
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 8
        spacing: 4
        z: 6
        Repeater {
            model: [{ label: "+", delta: 1 }, { label: "−", delta: -1 }]
            delegate: Rectangle {
                required property var modelData
                width: 28; height: 28; radius: 4
                color: zoomMouse.containsMouse ? "#555555" : "#333333"
                border.color: "#666666"
                Text { anchors.centerIn: parent; text: parent.modelData.label; color: "white"; font.pixelSize: 16 }
                MouseArea {
                    id: zoomMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: map.zoomAt(parent.modelData.delta, map.width / 2, map.height / 2)
                }
            }
        }
    }

    // Attribution (required by the OSM tile usage policy)
    Rectangle {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        width: attribution.width + 8
        height: attribution.height + 4
        color: "#a0000000"
        z: 6
        Text {
            id: attribution
            anchors.centerIn: parent
            text: "© OpenStreetMap contributors"
            color: "#dddddd"
            font.pixelSize: 10
        }
    }

    Rectangle {
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        width: coordText.width + 8
        height: coordText.height + 4
        color: "#a0000000"
        z: 6
        Text {
            id: coordText
            anchors.centerIn: parent
            color: "#dddddd"
            font.pixelSize: 10
            text: "z" + map.zoom + "  " + (map.hasMarker
                  ? map.markerLat.toFixed(5) + ", " + map.markerLon.toFixed(5)
                  : "click to place the observation")
        }
    }
}
