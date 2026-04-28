import QtQuick
import QtQuick.Window
import QtQuick.Layouts 1.15
import QtCore

Window {
    id: histogramWindow
    title: "RGB Histogram"
    width: 750
    height: 450
    minimumWidth: 100
    minimumHeight: 50
    property var uiStateRef: typeof uiState !== "undefined" ? uiState : null
    property var controllerRef: typeof controller !== "undefined" ? controller : null
    Settings {
        id: histSettings
        category: "histogram"
        property bool overlaidMode: true
    }

    visible: histogramWindow.uiStateRef ? histogramWindow.uiStateRef.isHistogramVisible : false

    FocusScope {
        id: histogramKeyScope
        anchors.fill: parent
        focus: histogramWindow.visible

        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_H && histogramWindow.controllerRef) {
                histogramWindow.controllerRef.toggle_histogram()
                event.accepted = true
            } else if (histogramWindow.controllerRef) {
                // Forward unhandled keys (e.g. arrow keys) to controller
                histogramWindow.controllerRef.handle_key_from_histogram(event.key, event.modifiers, event.text)
                event.accepted = true
            }
        }
    }
    
    Connections {
        target: histogramWindow.uiStateRef
        function onCurrentImageSourceChanged() {
            if (histogramWindow.visible && histogramWindow.controllerRef) {
                histogramWindow.controllerRef.update_histogram()
            }
        }
    }

    onVisibleChanged: {
        if (visible && histogramWindow.controllerRef) {
            histogramKeyScope.forceActiveFocus()
            histogramWindow.controllerRef.update_histogram()
        }
    }

    // --- Injected Properties ---
    property color windowBackgroundColor: "#f4f4f4"
    property color primaryTextColor: "#222222"
    property color gridLineColor: "#dcdcdc"
    property color dangerColor: Qt.rgba(1, 0, 0, 0.25)

    color: windowBackgroundColor

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: histogramWindow.width > 200 ? 15 : 2
        spacing: histogramWindow.width > 200 ? 8 : 2

        // Histogram toggle button
        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            Row {
                spacing: 0
                Rectangle {
                    width: 70; height: 20
                    radius: 3
                    color: histSettings.overlaidMode ? "#2c2c2c" : "transparent"
                    border.color: "#3a3a3a"; border.width: 1
                    Text {
                        anchors.centerIn: parent
                        text: "Overlaid"
                        font.pixelSize: 10
                        color: histSettings.overlaidMode ? "#e8e6e3" : "#6b6764"
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: {
                            histSettings.overlaidMode = true
                        }
                    }
                }
                Rectangle {
                    width: 70; height: 20
                    radius: 3
                    color: !histSettings.overlaidMode ? "#2c2c2c" : "transparent"
                    border.color: "#3a3a3a"; border.width: 1
                    Text {
                        anchors.centerIn: parent
                        text: "Channels"
                        font.pixelSize: 10
                        color: !histSettings.overlaidMode ? "#e8e6e3" : "#6b6764"
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: {
                            histSettings.overlaidMode = false
                        }
                    }
                }
            }
        }

        // Histogram display (overlaid or 3-channel)
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            OverlaidHistogram {
                anchors.fill: parent
                visible: histSettings.overlaidMode
                rData: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["r"] || []) : []
                gData: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["g"] || []) : []
                bData: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["b"] || []) : []
                rClip: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["r_clip"] || 0) : 0
                gClip: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["g_clip"] || 0) : 0
                bClip: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["b_clip"] || 0) : 0
                gridLineColor: histogramWindow.gridLineColor
            }

            RowLayout {
                anchors.fill: parent
                visible: !histSettings.overlaidMode
                spacing: histogramWindow.width > 200 ? 15 : 2

                SingleChannelHistogram {
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    channelName: "Red"
                    channelColor: "#e15050"
                    gridLineColor: histogramWindow.gridLineColor
                    dangerColor: histogramWindow.dangerColor
                    textColor: histogramWindow.primaryTextColor

                    histogramData: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["r"] || []) : []
                    clipCount: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["r_clip"] || 0) : 0
                    preClipCount: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["r_preclip"] || 0) : 0
                }

                SingleChannelHistogram {
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    channelName: "Green"
                    channelColor: "#50e150"
                    gridLineColor: histogramWindow.gridLineColor
                    dangerColor: histogramWindow.dangerColor
                    textColor: histogramWindow.primaryTextColor

                    histogramData: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["g"] || []) : []
                    clipCount: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["g_clip"] || 0) : 0
                    preClipCount: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["g_preclip"] || 0) : 0
                }

                SingleChannelHistogram {
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    channelName: "Blue"
                    channelColor: "#5050e1"
                    gridLineColor: histogramWindow.gridLineColor
                    dangerColor: histogramWindow.dangerColor
                    textColor: histogramWindow.primaryTextColor

                    histogramData: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["b"] || []) : []
                    clipCount: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["b_clip"] || 0) : 0
                    preClipCount: histogramWindow.uiStateRef && histogramWindow.uiStateRef.histogramData ? (histogramWindow.uiStateRef.histogramData["b_preclip"] || 0) : 0
                }
            }
        }
    }
}
