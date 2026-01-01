import QtQuick
import QtQuick.Window
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Window {
    id: histogramWindow
    title: "RGB Histogram"
    width: 750
    height: 450
    minimumWidth: 100
    minimumHeight: 50
    visible: uiState ? uiState.isHistogramVisible : false

    FocusScope {
        id: histogramKeyScope
        anchors.fill: parent
        focus: histogramWindow.visible

        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_H && controller) {
                controller.toggle_histogram()
                event.accepted = true // Only accept if H is pressed
            }
            // For other keys, event.accepted remains false, allowing propagation
        }
    }
    
    // Connections need to be outside the visibility check
    Connections {
        target: uiState
        function onHistogramDataChanged() {
            if (histogramWindow.visible) {
                // Since data is bound, the components will update automatically
            }
        }
        function onCurrentImageSourceChanged() {
            if (histogramWindow.visible && controller) {
                // Get zoom/pan info from main image view
                var zoom = 1.0
                var panX = 0.0
                var panY = 0.0
                var imageScale = 1.0
                
                // Try to get zoom/pan from Components (if accessible)
                // For now, just call without params - Components will handle it
                controller.update_histogram()
            }
        }
    }

    onVisibleChanged: {
        if (visible && controller) {
            histogramKeyScope.forceActiveFocus()
            controller.update_histogram()
        }
    }

    // --- Injected Properties ---
    // These are set by Main.qml to decouple the component from global state
    property color windowBackgroundColor: "#f4f4f4"
    property color primaryTextColor: "#222222"
    property color gridLineColor: "#dcdcdc"
    property color dangerColor: Qt.rgba(1, 0, 0, 0.25)

    color: windowBackgroundColor


    Component {
        id: singleChannelHistogram

        Item {
            property string channelName: "Channel"
            property color channelColor: "white"
            property var histogramData: []
            property int clipCount: 0
            property int preClipCount: 0

            onHistogramDataChanged: {
                if (canvas && canvas.available) canvas.requestPaint()
            }

            ColumnLayout {
                anchors.fill: parent
                spacing: 2
                
                Text {
                    text: channelName
                    color: channelColor
                    font.bold: true
                    font.pixelSize: Math.max(10, Math.min(14, histogramWindow.height / 30))
                    Layout.alignment: Qt.AlignHCenter
                    visible: histogramWindow.height > 100
                }

                Canvas {
                    id: canvas
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    onAvailableChanged: {
                        if (available) requestPaint()
                    }

                    onPaint: {
                        var ctx = getContext("2d")
                        ctx.clearRect(0, 0, canvas.width, canvas.height)
                        
                        // Handle null or empty data gracefully
                        if (!histogramData || histogramData.length === undefined || histogramData.length === 0) return

                        // --- Draw Grid ---
                        ctx.strokeStyle = gridLineColor
                        ctx.lineWidth = 1
                        for (var i = 1; i < 4; i++) {
                            var y = i * canvas.height / 4
                            ctx.beginPath()
                            ctx.moveTo(0, y)
                            ctx.lineTo(canvas.width, y)
                            ctx.stroke()
                        }
                        
                        // --- Draw Danger Zone ---
                        var dangerZoneStart = (250 / 255) * canvas.width
                        ctx.fillStyle = dangerColor
                        ctx.fillRect(dangerZoneStart, 0, canvas.width - dangerZoneStart, canvas.height)

                        // --- Prepare data for drawing ---
                        var maxVal = 0
                        for (i = 0; i < histogramData.length; i++) {
                            maxVal = Math.max(maxVal, histogramData[i])
                        }
                        if (maxVal === 0) return
                        
                        // --- Draw Histogram Path ---
                        ctx.beginPath()
                        ctx.moveTo(0, canvas.height)
                        
                        for (i = 0; i < histogramData.length; i++) {
                            var x = histogramData.length > 1 ? (i / (histogramData.length - 1)) * canvas.width : canvas.width / 2
                            var y = canvas.height - (histogramData[i] / maxVal) * canvas.height
                            ctx.lineTo(x, y)
                        }
                        
                        ctx.lineTo(canvas.width, canvas.height)
                        ctx.closePath()

                        // Create gradient fill
                        var gradient = ctx.createLinearGradient(0, 0, 0, canvas.height)
                        var transparentColor = Qt.color(channelColor)
                        transparentColor.a = 0.0
                        var semiTransparentColor = Qt.color(channelColor)
                        semiTransparentColor.a = 0.4
                        
                        gradient.addColorStop(0, semiTransparentColor)
                        gradient.addColorStop(1, transparentColor)
                        
                        ctx.fillStyle = gradient
                        ctx.fill()
                        
                        // Draw outline
                        ctx.strokeStyle = channelColor
                        ctx.lineWidth = 1.5
                        ctx.stroke()
                    }
                }

                RowLayout {
                    Layout.alignment: Qt.AlignHCenter
                    spacing: 5
                    visible: histogramWindow.height > 80
                    
                    Text {
                        text: "P:" + preClipCount
                        color: primaryTextColor
                        font.pixelSize: Math.max(8, Math.min(11, histogramWindow.height / 40))
                        visible: histogramWindow.width > 400
                    }
                    Text {
                        text: (histogramWindow.width > 400 ? "Clipped: " : "C:") + clipCount
                        color: clipCount > 0 ? "red" : primaryTextColor
                        font.bold: clipCount > 0
                        font.pixelSize: Math.max(8, Math.min(11, histogramWindow.height / 40))
                    }
                }
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: histogramWindow.width > 200 ? 15 : 2
        spacing: histogramWindow.width > 200 ? 15 : 2

        Loader {
            id: redLoader
            Layout.fillWidth: true
            Layout.fillHeight: true
            sourceComponent: singleChannelHistogram
            onLoaded: {
                item.channelName = "Red"
                item.channelColor = "#e15050"
                
                item.histogramData = Qt.binding(function() {
                    return uiState && uiState.histogramData ? (uiState.histogramData["r"] || []) : []
                })
                item.clipCount = Qt.binding(function() {
                    return uiState && uiState.histogramData ? (uiState.histogramData["r_clip"] || 0) : 0
                })
                item.preClipCount = Qt.binding(function() {
                    return uiState && uiState.histogramData ? (uiState.histogramData["r_preclip"] || 0) : 0
                })
            }
        }
        
        Loader {
            id: greenLoader
            Layout.fillWidth: true
            Layout.fillHeight: true
            sourceComponent: singleChannelHistogram
            onLoaded: {
                item.channelName = "Green"
                item.channelColor = "#50e150"

                item.histogramData = Qt.binding(function() {
                    return uiState && uiState.histogramData ? (uiState.histogramData["g"] || []) : []
                })
                item.clipCount = Qt.binding(function() {
                    return uiState && uiState.histogramData ? (uiState.histogramData["g_clip"] || 0) : 0
                })
                item.preClipCount = Qt.binding(function() {
                    return uiState && uiState.histogramData ? (uiState.histogramData["g_preclip"] || 0) : 0
                })
            }
        }

        Loader {
            id: blueLoader
            Layout.fillWidth: true
            Layout.fillHeight: true
            sourceComponent: singleChannelHistogram
            onLoaded: {
                item.channelName = "Blue"
                item.channelColor = "#5050e1"

                item.histogramData = Qt.binding(function() {
                    return uiState && uiState.histogramData ? (uiState.histogramData["b"] || []) : []
                })
                item.clipCount = Qt.binding(function() {
                    return uiState && uiState.histogramData ? (uiState.histogramData["b_clip"] || 0) : 0
                })
                item.preClipCount = Qt.binding(function() {
                    return uiState && uiState.histogramData ? (uiState.histogramData["b_preclip"] || 0) : 0
                })
            }
        }
    }
}
