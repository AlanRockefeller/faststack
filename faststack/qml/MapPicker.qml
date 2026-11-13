pragma ComponentBehavior: Bound
import QtQuick 2.15
import QtQuick.Controls 2.15

// A small slippy map drawn from cached OpenStreetMap tiles
// (image://osmtile/z/x/y, served by faststack.inat.providers).
// Drag to pan, wheel or +/- to zoom, click to place the marker.
// photoMarkers puts small photos on the map (other observations): hovering
// one shows it larger with its label, a right-click asks to copy its position.
// The right button drags the map too.
Item {
    id: map
    clip: true

    property int zoom: 13
    property real centerLat: 37.8715
    property real centerLon: -122.2730
    property bool hasMarker: false
    property real markerLat: 0
    property real markerLon: 0
    // Radius of the accuracy circle drawn around the marker; 0 hides it.
    property real accuracyMeters: 0
    property color textColor: "white"

    // Photos pinned to the map: [{ lat, lon, source, preview, label, kind }].
    // kind "inat" (already on iNaturalist) gets a green frame, anything else
    // the accent colour.
    property var photoMarkers: []
    property int markerSize: 64
    property int hoveredMarker: -1
    readonly property var hoveredPhoto: hoveredMarker >= 0 && hoveredMarker < photoMarkers.length
                                        ? photoMarkers[hoveredMarker] : null
    onPhotoMarkersChanged: hoveredMarker = -1

    signal picked(real lat, real lon)
    signal markerCopyRequested(var marker)

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

    // The area on screen, for asking what else is in it.
    function viewBounds() {
        return {
            swlat: Math.max(-90, latAt(height)), swlng: Math.max(-180, lonAt(0)),
            nelat: Math.min(90, latAt(0)), nelng: Math.min(180, lonAt(width))
        }
    }
    // The photo marker drawn at this pixel (the topmost one), or -1.
    function markerAt(px, py) {
        var half = markerSize / 2
        for (var i = photoMarkers.length - 1; i >= 0; i--) {
            var dx = px - pixelX(photoMarkers[i].lon)
            var dy = py - pixelY(photoMarkers[i].lat)
            if (Math.abs(dx) <= half && dy <= 4 && dy >= -(markerSize + 8)) return i
        }
        return -1
    }

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

    // Centre in tile units at the current zoom; every tile is placed from it.
    readonly property real centerX: lonToX(centerLon, zoom)
    readonly property real centerY: latToY(centerLat, zoom)

    // Tiles covering the viewport (a one-tile margin so panning shows no gaps).
    // The model is updated in place: a tile that is still wanted keeps its
    // Image (nothing is reloaded on a pan), and after a zoom the tiles of the
    // previous level stay on screen, stretched, underneath the new ones until
    // those have arrived - instead of the whole map blanking on each step.
    ListModel { id: tileModel }
    function refreshTiles() {
        if (width <= 0 || height <= 0) return
        var n = Math.pow(2, zoom)
        var halfW = width / 2 / tileSize
        var halfH = height / 2 / tileSize
        var x0 = Math.floor(centerX - halfW) - 1
        var x1 = Math.floor(centerX + halfW) + 1
        var y0 = Math.max(0, Math.floor(centerY - halfH) - 1)
        var y1 = Math.min(n - 1, Math.floor(centerY + halfH) + 1)
        var wanted = {}
        for (var ty = y0; ty <= y1; ty++)
            for (var tx = x0; tx <= x1; tx++)
                wanted[zoom + "/" + tx + "/" + ty] = { tz: zoom, tx: tx, ty: ty, wx: ((tx % n) + n) % n }
        for (var i = tileModel.count - 1; i >= 0; i--) {
            var tile = tileModel.get(i)
            if (tile.tz === zoom) {
                if (tile.key in wanted) delete wanted[tile.key]   // already there
                else tileModel.remove(i)                          // scrolled out of view
            } else {
                // Another zoom level: worth keeping as a backdrop only if it
                // has its picture; dropping the rest also cancels their loads.
                var item = tileRepeater.itemAt(i)
                if (!item || item.status !== Image.Ready) tileModel.remove(i)
            }
        }
        for (var key in wanted) {
            var w = wanted[key]
            tileModel.append({ key: key, tz: w.tz, tx: w.tx, ty: w.ty, wx: w.wx })
        }
        Qt.callLater(dropBackdrop)
    }
    // Once every tile of the current level has its picture, the stretched
    // tiles of other levels underneath are no longer needed.
    function dropBackdrop() {
        var others = false
        for (var i = 0; i < tileModel.count; i++) {
            if (tileModel.get(i).tz !== zoom) { others = true; continue }
            var item = tileRepeater.itemAt(i)
            if (!item || item.status === Image.Loading) return
        }
        if (!others) return
        for (var j = tileModel.count - 1; j >= 0; j--)
            if (tileModel.get(j).tz !== zoom) tileModel.remove(j)
    }

    onWidthChanged: refreshTiles()
    onHeightChanged: refreshTiles()
    Component.onCompleted: refreshTiles()

    Rectangle { anchors.fill: parent; color: "#1c1c1c" }

    // Tiles and the marker live in one layer that is shifted by the drag
    // offset while the mouse is down, so a pan does not rebuild every tile
    // delegate per mouse move; the centre is committed on release (or once
    // the offset nears the one-tile margin).
    Item {
        id: tileLayer
        x: 0; y: 0
        width: parent.width; height: parent.height

    Repeater {
        id: tileRepeater
        model: tileModel
        delegate: Image {
            required property int tz
            required property int tx
            required property int ty
            required property int wx
            // 1 at the current zoom; 2, 4, ... or 1/2, 1/4, ... for a backdrop tile.
            readonly property real zoomScale: Math.pow(2, map.zoom - tz)
            x: (tx * zoomScale - map.centerX) * map.tileSize + map.width / 2
            y: (ty * zoomScale - map.centerY) * map.tileSize + map.height / 2
            z: tz === map.zoom ? 1 : 0
            width: map.tileSize * zoomScale
            height: map.tileSize * zoomScale
            asynchronous: true
            cache: true
            source: "image://osmtile/" + tz + "/" + wx + "/" + ty
            onStatusChanged: if (status !== Image.Loading) Qt.callLater(map.dropBackdrop)
        }
    }

    // Photo markers: a thumbnail on a short stem that ends at the spot.
    Repeater {
        model: map.photoMarkers
        delegate: Item {
            id: pin
            required property var modelData
            required property int index
            readonly property bool hovered: map.hoveredMarker === index
            readonly property color frameColor: hovered ? "white" : (modelData.kind === "inat" ? "#74ac00" : "#6366f1")
            x: map.pixelX(modelData.lon)
            y: map.pixelY(modelData.lat)
            z: hovered ? 4 : 3
            Rectangle { x: -1; y: -8; width: 2; height: 8; color: pin.frameColor }
            Rectangle { x: -3; y: -3; width: 6; height: 6; radius: 3; color: pin.frameColor; border.color: "black"; border.width: 1 }
            Rectangle {
                x: -map.markerSize / 2
                y: -map.markerSize - 8
                width: map.markerSize; height: map.markerSize
                radius: 4
                color: "#333333"
                border.color: pin.frameColor
                border.width: 2
                Image {
                    anchors.fill: parent
                    anchors.margins: 2
                    source: pin.modelData.source
                    fillMode: Image.PreserveAspectCrop
                    clip: true
                    asynchronous: true
                    sourceSize.width: 128
                    sourceSize.height: 128
                }
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
            // Web Mercator: metres per pixel at this latitude and zoom.
            readonly property real radiusPx: map.accuracyMeters
                / (156543.03392 * Math.cos(map.markerLat * Math.PI / 180) / Math.pow(2, map.zoom))
            visible: map.accuracyMeters > 0 && radiusPx >= 9 && radiusPx < 8000
            width: radiusPx * 2; height: width; radius: radiusPx
            x: -radiusPx; y: -radiusPx
            color: "#302196f3"
            border.color: "#2196f3"; border.width: 2
        }
        Rectangle {
            width: 16; height: 16; radius: 8
            x: -8; y: -8
            color: "#e53935"
            border.color: "white"; border.width: 2
        }
        Rectangle { width: 2; height: 14; x: -1; y: 6; color: "white" }
    }
    }  // tileLayer

    // Crosshair at the centre so the user can see where "centre" is.
    Rectangle { x: map.width / 2 - 10; y: map.height / 2; width: 20; height: 1; color: "#80ffffff" }
    Rectangle { x: map.width / 2; y: map.height / 2 - 10; width: 1; height: 20; color: "#80ffffff" }

    MouseArea {
        id: panArea
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        hoverEnabled: true
        property bool rightPress: false
        onExited: map.hoveredMarker = -1
        property real pressX: 0
        property real pressY: 0
        property real startCx: 0
        property real startCy: 0
        property bool moved: false
        cursorShape: pressed ? Qt.ClosedHandCursor : Qt.CrossCursor

        onPressed: function(mouse) {
            // Either button drags the map; they differ only in what a
            // click without a drag does (place the marker / copy a photo's spot).
            rightPress = mouse.button === Qt.RightButton
            pressX = mouse.x; pressY = mouse.y; moved = false
            startCx = map.lonToX(map.centerLon, map.zoom)
            startCy = map.latToY(map.centerLat, map.zoom)
        }
        function commitPan(dx, dy) {
            map.centerLon = map.xToLon(startCx - dx / map.tileSize, map.zoom)
            map.centerLat = map.yToLat(startCy - dy / map.tileSize, map.zoom)
            startCx = map.lonToX(map.centerLon, map.zoom)
            startCy = map.latToY(map.centerLat, map.zoom)
            tileLayer.x = 0; tileLayer.y = 0
            map.refreshTiles()
        }
        onPositionChanged: function(mouse) {
            if (!pressed) { map.hoveredMarker = map.markerAt(mouse.x, mouse.y); return }
            map.hoveredMarker = -1
            var dx = mouse.x - pressX
            var dy = mouse.y - pressY
            if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true
            if (!moved) return
            if (Math.abs(dx) > map.tileSize * 0.8 || Math.abs(dy) > map.tileSize * 0.8) {
                // Nearing the pre-fetched margin: commit and re-anchor the drag.
                commitPan(dx, dy)
                pressX = mouse.x; pressY = mouse.y
                return
            }
            tileLayer.x = dx
            tileLayer.y = dy
        }
        onReleased: function(mouse) {
            if (moved) {
                commitPan(mouse.x - pressX, mouse.y - pressY)
            } else if (rightPress) {
                var hit = map.markerAt(mouse.x, mouse.y)
                if (hit >= 0) map.markerCopyRequested(map.photoMarkers[hit])
            } else {
                var lat = map.latAt(mouse.y)
                var lon = map.lonAt(mouse.x)
                map.setMarker(lat, lon)
                map.picked(lat, lon)
            }
        }
        onDoubleClicked: function(mouse) { if (mouse.button === Qt.LeftButton) map.zoomAt(1, mouse.x, mouse.y) }
        // One zoom level per wheel notch (120 units). A touchpad or a
        // high-resolution wheel sends many small deltas per gesture; zooming
        // a level on each of them would jump from street to continent.
        property real wheelRemainder: 0
        onWheel: function(wheel) {
            wheel.accepted = true
            if (wheel.angleDelta.y === 0) return  // horizontal scroll
            if ((wheel.angleDelta.y > 0) !== (wheelRemainder > 0)) wheelRemainder = 0
            wheelRemainder += wheel.angleDelta.y
            while (Math.abs(wheelRemainder) >= 120) {
                var step = wheelRemainder > 0 ? 1 : -1
                wheelRemainder -= step * 120
                map.zoomAt(step, wheel.x, wheel.y)
            }
        }
    }

    // The photo marker under the mouse, larger, with what it is.
    Rectangle {
        id: markerPreview
        readonly property var marker: map.hoveredPhoto
        readonly property real pinX: marker ? map.pixelX(marker.lon) : 0
        readonly property real pinY: marker ? map.pixelY(marker.lat) : 0
        readonly property real side: map.markerSize / 2 + 10
        visible: marker !== null
        z: 7
        width: 248
        height: previewColumn.implicitHeight + 12
        x: pinX + side + width <= map.width ? pinX + side : Math.max(4, pinX - side - width)
        y: Math.max(4, Math.min(map.height - height - 4, pinY - map.markerSize / 2 - height / 2))
        radius: 6
        color: "#f0222222"
        border.color: "#666666"
        Column {
            id: previewColumn
            x: 6; y: 6
            width: parent.width - 12
            spacing: 4
            Image {
                width: parent.width
                height: 177
                visible: source != ""
                source: markerPreview.marker && markerPreview.marker.preview ? markerPreview.marker.preview : ""
                fillMode: Image.PreserveAspectFit
                asynchronous: true
            }
            Text {
                width: parent.width
                text: markerPreview.marker ? markerPreview.marker.label : ""
                color: "white"; font.pixelSize: 12; font.bold: true
                wrapMode: Text.Wrap
            }
            Text {
                width: parent.width
                text: markerPreview.marker
                      ? markerPreview.marker.lat.toFixed(5) + ", " + markerPreview.marker.lon.toFixed(5) + " · right-click to copy"
                      : ""
                color: "#aaaaaa"; font.pixelSize: 11
            }
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
