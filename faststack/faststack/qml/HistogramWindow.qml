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
                event.accepted = true
            }
        }
    }
    
    Connections {
        target: uiState
        function onCurrentImageSourceChanged() {
            if (histogramWindow.visible && controller) {
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
    property color windowBackgroundColor: "#f4f4f4"
    property color primaryTextColor: "#222222"
    property color gridLineColor: "#dcdcdc"
    property color dangerColor: Qt.rgba(1, 0, 0, 0.25)

    color: windowBackgroundColor

    RowLayout {
        anchors.fill: parent
        anchors.margins: histogramWindow.width > 200 ? 15 : 2
        spacing: histogramWindow.width > 200 ? 15 : 2

        SingleChannelHistogram {
            Layout.fillWidth: true
            Layout.fillHeight: true
            
            channelName: "Red"
            channelColor: "#e15050"
            gridLineColor: histogramWindow.gridLineColor
            dangerColor: histogramWindow.dangerColor
            textColor: histogramWindow.primaryTextColor
            
            histogramData: uiState && uiState.histogramData ? (uiState.histogramData["r"] || []) : []
            clipCount: uiState && uiState.histogramData ? (uiState.histogramData["r_clip"] || 0) : 0
            preClipCount: uiState && uiState.histogramData ? (uiState.histogramData["r_preclip"] || 0) : 0
        }
        
        SingleChannelHistogram {
            Layout.fillWidth: true
            Layout.fillHeight: true
            
            channelName: "Green"
            channelColor: "#50e150"
            gridLineColor: histogramWindow.gridLineColor
            dangerColor: histogramWindow.dangerColor
            textColor: histogramWindow.primaryTextColor
            
            histogramData: uiState && uiState.histogramData ? (uiState.histogramData["g"] || []) : []
            clipCount: uiState && uiState.histogramData ? (uiState.histogramData["g_clip"] || 0) : 0
            preClipCount: uiState && uiState.histogramData ? (uiState.histogramData["g_preclip"] || 0) : 0
        }

        SingleChannelHistogram {
            Layout.fillWidth: true
            Layout.fillHeight: true
            
            channelName: "Blue"
            channelColor: "#5050e1"
            gridLineColor: histogramWindow.gridLineColor
            dangerColor: histogramWindow.dangerColor
            textColor: histogramWindow.primaryTextColor
            
            histogramData: uiState && uiState.histogramData ? (uiState.histogramData["b"] || []) : []
            clipCount: uiState && uiState.histogramData ? (uiState.histogramData["b_clip"] || 0) : 0
            preClipCount: uiState && uiState.histogramData ? (uiState.histogramData["b_preclip"] || 0) : 0
        }
    }
}
