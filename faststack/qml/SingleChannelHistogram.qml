import QtQuick
import QtQuick.Layouts 1.15

Item {
    id: root
    property string channelName: "Channel"
    property color channelColor: "white"
    property var histogramData: []
    property int clipCount: 0
    property int preClipCount: 0
    property color gridLineColor: "#50ffffff" // Default semi-transparent white
    property color dangerColor: Qt.rgba(1, 0, 0, 0.25)
    property color textColor: "white"
    
    // Allow minimal mode (hide text)
    property bool minimal: false

    onHistogramDataChanged: {
        if (canvas && canvas.available) canvas.requestPaint()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 2
        
        Text {
            text: root.channelName
            color: root.channelColor
            font.bold: true
            // Dynamic font size based on height, but capped
            font.pixelSize: Math.max(10, Math.min(14, root.height / 10))
            Layout.alignment: Qt.AlignHCenter
            visible: !root.minimal && root.height > 100
        }

        Canvas {
            id: canvas
            Layout.fillWidth: true
            Layout.fillHeight: true

            onAvailableChanged: {
                if (available) requestPaint()
            }
            
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()

            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, canvas.width, canvas.height)
                
                // Handle null or empty data gracefully
                if (!root.histogramData || root.histogramData.length === undefined || root.histogramData.length === 0) return

                // --- Draw Grid ---
                ctx.strokeStyle = root.gridLineColor
                ctx.lineWidth = 1
                for (var i = 1; i < 4; i++) {
                    var y = i * canvas.height / 4
                    ctx.beginPath()
                    ctx.moveTo(0, y)
                    ctx.lineTo(canvas.width, y)
                    ctx.stroke()
                }
                
                // --- Draw Danger Zone ---
                // The rightmost ~2% (250-255)
                var dangerZoneStart = (250 / 255) * canvas.width
                ctx.fillStyle = root.dangerColor
                ctx.fillRect(dangerZoneStart, 0, canvas.width - dangerZoneStart, canvas.height)

                // --- Prepare data for drawing ---
                var maxVal = 0
                for (i = 0; i < root.histogramData.length; i++) {
                    maxVal = Math.max(maxVal, root.histogramData[i])
                }
                if (maxVal === 0) return
                
                // --- Draw Histogram Path ---
                ctx.beginPath()
                ctx.moveTo(0, canvas.height)
                
                var len = root.histogramData.length
                for (i = 0; i < len; i++) {
                    var x = len > 1 ? (i / (len - 1)) * canvas.width : canvas.width / 2
                    var y = canvas.height - (root.histogramData[i] / maxVal) * canvas.height
                    ctx.lineTo(x, y)
                }
                
                ctx.lineTo(canvas.width, canvas.height)
                ctx.closePath()

                // Create gradient fill
                var gradient = ctx.createLinearGradient(0, 0, 0, canvas.height)
                var transparentColor = Qt.color(root.channelColor)
                transparentColor.a = 0.0
                var semiTransparentColor = Qt.color(root.channelColor)
                semiTransparentColor.a = 0.4
                
                gradient.addColorStop(0, semiTransparentColor)
                gradient.addColorStop(1, transparentColor)
                
                ctx.fillStyle = gradient
                ctx.fill()
                
                // Draw outline
                ctx.strokeStyle = root.channelColor
                ctx.lineWidth = 1.5
                ctx.stroke()
            }
        }

        RowLayout {
            Layout.alignment: Qt.AlignHCenter
            spacing: 5
            visible: !root.minimal && root.height > 80
            
            Text {
                text: "P:" + root.preClipCount
                color: root.textColor
                font.pixelSize: Math.max(8, Math.min(11, root.height / 15))
                visible: root.width > 120
            }
            Text {
                text: (root.width > 120 ? "Clipped: " : "C:") + root.clipCount
                color: root.clipCount > 0 ? "red" : root.textColor
                font.bold: root.clipCount > 0
                font.pixelSize: Math.max(8, Math.min(11, root.height / 15))
            }
        }
    }
}
