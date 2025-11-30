import QtQuick
import QtQuick.Window
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Window {
    id: histogramWindow
    title: "RGB Histogram"
    width: 750
    height: 450
    minimumWidth: 500
    minimumHeight: 350
    visible: uiState ? uiState.isHistogramVisible : false
    
    // Connections need to be outside the visibility check
    Connections {
        target: uiState
        function onIsHistogramVisibleChanged() {
            histogramWindow.visible = uiState.isHistogramVisible
        }
        function onHistogramDataChanged() {
            if (histogramWindow.visible) {
                // Since data is bound, the components will update automatically
            }
        }
        function onCurrentImageSourceChanged() {
            if (histogramWindow.visible && controller) {
                controller.update_histogram()
            }
        }
    }

    onVisibleChanged: {
        if (visible && controller) {
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

            ColumnLayout {
                anchors.fill: parent
                
                Text {
                    text: channelName
                    color: channelColor
                    font.bold: true
                    font.pixelSize: 14
                    Layout.alignment: Qt.AlignHCenter
                }

                Canvas {
                    id: canvas
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    
                    onPaint: {
                        var ctx = getContext("2d")
                        ctx.clearRect(0, 0, canvas.width, canvas.height)
                        
                        if (!histogramData || histogramData.length === 0) return

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
                            var x = (i / (histogramData.length - 1)) * canvas.width
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
                    spacing: 15

                    Text {
                        text: "Pre-clip: " + preClipCount
                        color: primaryTextColor
                        font.pixelSize: 11
                    }
                    Text {
                        text: "Clipped: " + clipCount
                        color: clipCount > 0 ? "red" : primaryTextColor
                        font.bold: clipCount > 0
                        font.pixelSize: 11
                    }
                }
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: 15
        spacing: 15

        Loader {
            id: redLoader
            Layout.fillWidth: true
            Layout.fillHeight: true
            sourceComponent: singleChannelHistogram
            onLoaded: {
                item.channelName = "Red"
                item.channelColor = "#e15050"
            }
            Connections {
                target: uiState
                function onHistogramDataChanged() {
                    if (redLoader.item && uiState.histogramData) {
                        redLoader.item.histogramData = uiState.histogramData.r_hist
                        redLoader.item.clipCount = uiState.histogramData.r_clip
                        redLoader.item.preClipCount = uiState.histogramData.r_preclip
                        // Access canvas through item: item.children[0].children[1] is fragile
                        redLoader.item.children[0].children[1].requestPaint()
                    }
                }
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
            }
            Connections {
                target: uiState
                function onHistogramDataChanged() {
                    if (greenLoader.item && uiState.histogramData) {
                        greenLoader.item.histogramData = uiState.histogramData.g_hist
                        greenLoader.item.clipCount = uiState.histogramData.g_clip
                        greenLoader.item.preClipCount = uiState.histogramData.g_preclip
                        greenLoader.item.children[0].children[1].requestPaint()
                    }
                }
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
            }
            Connections {
                target: uiState
                function onHistogramDataChanged() {
                    if (blueLoader.item && uiState.histogramData) {
                        blueLoader.item.histogramData = uiState.histogramData.b_hist
                        blueLoader.item.clipCount = uiState.histogramData.b_clip
                        blueLoader.item.preClipCount = uiState.histogramData.b_preclip
                        blueLoader.item.children[0].children[1].requestPaint()
                    }
                }
            }
        }
    }
}