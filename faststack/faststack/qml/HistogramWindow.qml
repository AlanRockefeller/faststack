import QtQuick
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Window {
    id: histogramWindow
    title: "RGB Histogram"
    width: 600
    height: 400
    minimumWidth: 400
    minimumHeight: 300
    visible: uiState && uiState.isHistogramVisible
    
    property bool isDarkTheme: uiState ? uiState.theme === 0 : true
    property color backgroundColor: isDarkTheme ? "#2b2b2b" : "#ffffff"
    property color textColor: isDarkTheme ? "white" : "black"
    
    color: backgroundColor
    
    onVisibleChanged: {
        if (visible && controller) {
            controller.update_histogram()
        }
    }
    
    Connections {
        target: uiState
        function onHistogramDataChanged() {
            if (histogramWindow.visible) {
                histogramCanvas.requestPaint()
            }
        }
        function onCurrentImageSourceChanged() {
            if (histogramWindow.visible && controller) {
                controller.update_histogram()
            }
        }
    }
    
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 10
        spacing: 10
        
        Text {
            text: "RGB Histogram"
            font.bold: true
            font.pixelSize: 14
            color: histogramWindow.textColor
            Layout.alignment: Qt.AlignHCenter
        }
        
        Canvas {
            id: histogramCanvas
            Layout.fillWidth: true
            Layout.fillHeight: true
            
            onPaint: {
                var ctx = getContext("2d")
                var width = histogramCanvas.width
                var height = histogramCanvas.height
                
                // Clear canvas
                ctx.fillStyle = histogramWindow.backgroundColor
                ctx.fillRect(0, 0, width, height)
                
                if (!uiState || !uiState.histogramData) {
                    return
                }
                
                var data = uiState.histogramData
                var rData = data.r || []
                var gData = data.g || []
                var bData = data.b || []
                
                if (rData.length === 0) {
                    return
                }
                
                // Find max value for normalization
                var maxValue = 0
                for (var i = 0; i < 256; i++) {
                    maxValue = Math.max(maxValue, rData[i] || 0)
                    maxValue = Math.max(maxValue, gData[i] || 0)
                    maxValue = Math.max(maxValue, bData[i] || 0)
                }
                
                if (maxValue === 0) {
                    return
                }
                
                // Draw grid lines
                ctx.strokeStyle = histogramWindow.isDarkTheme ? "#555555" : "#cccccc"
                ctx.lineWidth = 1
                for (var gridY = 0; gridY <= 4; gridY++) {
                    var y = (height - 40) * (gridY / 4) + 20
                    ctx.beginPath()
                    ctx.moveTo(20, y)
                    ctx.lineTo(width - 20, y)
                    ctx.stroke()
                }
                
                // Draw histogram bars
                var barWidth = (width - 40) / 256
                
                // Draw Red channel
                ctx.fillStyle = "rgba(255, 0, 0, 0.6)"
                for (var i = 0; i < 256; i++) {
                    var value = (rData[i] || 0) / maxValue
                    var barHeight = (height - 40) * value
                    var x = 20 + i * barWidth
                    var y = height - 20 - barHeight
                    ctx.fillRect(x, y, barWidth - 1, barHeight)
                }
                
                // Draw Green channel
                ctx.fillStyle = "rgba(0, 255, 0, 0.6)"
                for (var i = 0; i < 256; i++) {
                    var value = (gData[i] || 0) / maxValue
                    var barHeight = (height - 40) * value
                    var x = 20 + i * barWidth
                    var y = height - 20 - barHeight
                    ctx.fillRect(x, y, barWidth - 1, barHeight)
                }
                
                // Draw Blue channel
                ctx.fillStyle = "rgba(0, 0, 255, 0.6)"
                for (var i = 0; i < 256; i++) {
                    var value = (bData[i] || 0) / maxValue
                    var barHeight = (height - 40) * value
                    var x = 20 + i * barWidth
                    var y = height - 20 - barHeight
                    ctx.fillRect(x, y, barWidth - 1, barHeight)
                }
                
                // Draw axis labels
                ctx.fillStyle = histogramWindow.textColor
                ctx.font = "10px sans-serif"
                ctx.textAlign = "center"
                
                // X-axis labels (0, 64, 128, 192, 255)
                for (var labelX = 0; labelX <= 4; labelX++) {
                    var value = labelX * 64
                    if (labelX === 4) value = 255
                    var x = 20 + (value / 255) * (width - 40)
                    ctx.fillText(value.toString(), x, height - 5)
                }
                
                // Y-axis label
                ctx.save()
                ctx.translate(10, height / 2)
                ctx.rotate(-Math.PI / 2)
                ctx.textAlign = "center"
                ctx.fillText("Pixel Count", 0, 0)
                ctx.restore()
            }
        }
        
        RowLayout {
            Layout.alignment: Qt.AlignHCenter
            spacing: 20
            
            Rectangle {
                width: 20
                height: 20
                color: "red"
                opacity: 0.6
                border.color: histogramWindow.textColor
                border.width: 1
            }
            Text {
                text: "Red"
                color: histogramWindow.textColor
                font.pixelSize: 12
            }
            
            Rectangle {
                width: 20
                height: 20
                color: "green"
                opacity: 0.6
                border.color: histogramWindow.textColor
                border.width: 1
            }
            Text {
                text: "Green"
                color: histogramWindow.textColor
                font.pixelSize: 12
            }
            
            Rectangle {
                width: 20
                height: 20
                color: "blue"
                opacity: 0.6
                border.color: histogramWindow.textColor
                border.width: 1
            }
            Text {
                text: "Blue"
                color: histogramWindow.textColor
                font.pixelSize: 12
            }
        }
    }
}
