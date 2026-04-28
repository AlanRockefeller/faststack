import QtQuick 2.15
import QtQuick.Layouts 1.15

Item {
    id: root

    property var rData: []
    property var gData: []
    property var bData: []
    property int rClip: 0
    property int gClip: 0
    property int bClip: 0
    property int rPreClip: 0
    property int gPreClip: 0
    property int bPreClip: 0
    property color gridLineColor: "#2e2e2e"

    onRDataChanged: canvas.requestPaint()
    onGDataChanged: canvas.requestPaint()
    onBDataChanged: canvas.requestPaint()
    onGridLineColorChanged: canvas.requestPaint()

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Canvas {
            id: canvas

            Layout.fillWidth: true
            Layout.fillHeight: true

            onAvailableChanged: { if (available) requestPaint() }
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()

            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)

                // Background
                ctx.fillStyle = "#0a0a0a"
                ctx.fillRect(0, 0, width, height)

                // Grid lines (4 horizontal divisions)
                ctx.strokeStyle = root.gridLineColor
                ctx.lineWidth = 1
                for (var i = 1; i < 4; i++) {
                    ctx.beginPath()
                    ctx.moveTo(0, i * height / 4)
                    ctx.lineTo(width, i * height / 4)
                    ctx.stroke()
                }

                // Danger zone (rightmost ~2%)
                ctx.fillStyle = "rgba(255, 0, 0, 0.12)"
                var dz = (250 / 255) * width
                ctx.fillRect(dz, 0, width - dz, height)

                // Draw channels: B first (back), G, R (front)
                var channels = [
                    { data: root.bData, fill: "rgba(60, 80, 220, 0.40)", stroke: "rgba(80, 100, 230, 0.85)" },
                    { data: root.gData, fill: "rgba(60, 180, 60, 0.38)", stroke: "rgba(80, 200, 80, 0.80)" },
                    { data: root.rData, fill: "rgba(200, 60, 60, 0.50)", stroke: "rgba(220, 80, 80, 0.90)" },
                ]

                for (var ci = 0; ci < channels.length; ci++) {
                    var ch = channels[ci]
                    var data = ch.data
                    if (!data || data.length === 0) continue

                    var len = data.length
                    var maxVal = 0
                    for (var j = 0; j < len; j++) maxVal = Math.max(maxVal, data[j])
                    if (maxVal === 0) continue

                    ctx.beginPath()
                    ctx.moveTo(0, height)

                    if (width >= len) {
                        for (var k = 0; k < len; k++) {
                            var x = len > 1 ? (k / (len - 1)) * width : width / 2
                            ctx.lineTo(x, height - (data[k] / maxVal) * height)
                        }
                    } else {
                        // Max-pool downsampling (same as SingleChannelHistogram)
                        for (var px = 0; px < width; px++) {
                            var binStart = Math.floor((px / width) * len)
                            var binEnd = Math.ceil(((px + 1) / width) * len)
                            binStart = Math.max(0, Math.min(len - 1, binStart))
                            binEnd = Math.max(binStart + 1, Math.min(len, binEnd))
                            var localMax = 0
                            for (var b = binStart; b < binEnd; b++) {
                                if (b < len) localMax = Math.max(localMax, data[b])
                            }
                            ctx.lineTo(px, height - (localMax / maxVal) * height)
                        }
                    }

                    ctx.lineTo(width, height)
                    ctx.closePath()
                    ctx.fillStyle = ch.fill
                    ctx.fill()
                    ctx.strokeStyle = ch.stroke
                    ctx.lineWidth = 1.2
                    ctx.stroke()
                }
            }
        }

        // Stat bar
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 20
            color: "#1a1a1a"

            Row {
                anchors.centerIn: parent
                spacing: 14

                Row {
                    spacing: 4
                    Text {
                        text: "R P:" + root.rPreClip
                        font.pixelSize: 10
                        font.family: "IBM Plex Mono"
                        color: "#804040"
                    }
                    Text {
                        text: "C:" + root.rClip
                        font.pixelSize: 10
                        font.family: "IBM Plex Mono"
                        font.bold: root.rClip > 0
                        color: root.rClip > 0 ? "#ff6060" : "#804040"
                    }
                }

                Row {
                    spacing: 4
                    Text {
                        text: "G P:" + root.gPreClip
                        font.pixelSize: 10
                        font.family: "IBM Plex Mono"
                        color: "#407040"
                    }
                    Text {
                        text: "C:" + root.gClip
                        font.pixelSize: 10
                        font.family: "IBM Plex Mono"
                        font.bold: root.gClip > 0
                        color: root.gClip > 0 ? "#60ff60" : "#407040"
                    }
                }

                Row {
                    spacing: 4
                    Text {
                        text: "B P:" + root.bPreClip
                        font.pixelSize: 10
                        font.family: "IBM Plex Mono"
                        color: "#404080"
                    }
                    Text {
                        text: "C:" + root.bClip
                        font.pixelSize: 10
                        font.family: "IBM Plex Mono"
                        font.bold: root.bClip > 0
                        color: root.bClip > 0 ? "#8080ff" : "#404080"
                    }
                }
            }
        }
    }
}
