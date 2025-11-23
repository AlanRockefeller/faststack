import QtQuick
import QtQuick.Window

// This file is intended to hold QML components like the main image view.
// For simplicity, we'll start with just the main image view.

Item {
    id: loupeView
    anchors.fill: parent


    // Connection to handle zoom/pan reset signal from Python
    Connections {
        target: uiState
        function onResetZoomPanRequested() {
            scaleTransform.xScale = 1.0
            scaleTransform.yScale = 1.0
            panTransform.x = 0
            panTransform.y = 0
        }
    }

    // The main image display
    Image {
        id: mainImage
        anchors.fill: parent
        source: uiState && uiState.imageCount > 0 ? uiState.currentImageSource : ""
        fillMode: Image.PreserveAspectFit
        cache: false // We do our own caching in Python
	smooth: uiState && !uiState.anySliderPressed
	mipmap: uiState && !uiState.anySliderPressed

        Component.onCompleted: {
            if (width > 0 && height > 0) {
                var dpr = Screen.devicePixelRatio
                uiState.onDisplaySizeChanged(Math.round(width * dpr), Math.round(height * dpr))
            }
        }

        onWidthChanged: {
            if (width > 0 && height > 0) {
                resizeDebounceTimer.restart()
            }
        }

        onHeightChanged: {
            if (width > 0 && height > 0) {
                resizeDebounceTimer.restart()
            }
        }

        function updateZoomState() {
            if (scaleTransform.xScale > 1.1 && !uiState.isZoomed) {
                uiState.setZoomed(true);
            } else if (scaleTransform.xScale <= 1.0 && uiState.isZoomed) {
                uiState.setZoomed(false);
            }
        }

        transform: [
            Scale {
                id: scaleTransform
                origin.x: mainImage.width / 2
                origin.y: mainImage.height / 2
                onXScaleChanged: mainImage.updateZoomState()
                onYScaleChanged: mainImage.updateZoomState()
            },
            Translate {
                id: panTransform
            }
        ]
    }

    // Zoom and Pan logic would go here
    // For example, using PinchArea or MouseArea
    Timer {
        id: resizeDebounceTimer
        interval: 100 // milliseconds
        running: false
        onTriggered: {
            if (mainImage.width > 0 && mainImage.height > 0) {
                var dpr = Screen.devicePixelRatio
                uiState.onDisplaySizeChanged(Math.round(mainImage.width * dpr), Math.round(mainImage.height * dpr))
            }
            running = false
        }
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton
        
        // Drag-to-pan with drag-and-drop when dragging outside window
        property real lastX: 0
        property real lastY: 0
        property real startX: 0
        property real startY: 0
        property bool isDraggingOutside: false
        property int dragThreshold: 10  // Minimum distance before checking for outside drag

        onPressed: function(mouse) {
            lastX = mouse.x
            lastY = mouse.y
            startX = mouse.x
            startY = mouse.y
            isDraggingOutside = false
        }

        onPositionChanged: function(mouse) {
            if (pressed && !isDraggingOutside) {
                // Check if we've moved beyond the threshold
                var dx = mouse.x - startX
                var dy = mouse.y - startY
                var distance = Math.sqrt(dx*dx + dy*dy)
                
                if (distance > dragThreshold) {
                    // Check if mouse is outside the window bounds
                    var globalPos = mapToItem(null, mouse.x, mouse.y)
                    
                    if (globalPos.x < 0 || globalPos.y < 0 || 
                        globalPos.x > loupeView.width || globalPos.y > loupeView.height) {
                        // Mouse is outside window - initiate drag-and-drop
                        isDraggingOutside = true
                        controller.start_drag_current_image()
                        return
                    }
                }
                
                // Normal pan behavior
                panTransform.x += (mouse.x - lastX)
                panTransform.y += (mouse.y - lastY)
                lastX = mouse.x
                lastY = mouse.y
            }
        }
        
        onReleased: function(mouse) {
            isDraggingOutside = false
        }

        // Wheel for zoom
        onWheel: function(wheel) {
            // A real implementation would be more complex, zooming
            // into the cursor position.
            var scaleFactor = wheel.angleDelta.y > 0 ? 1.2 : 1 / 1.2;
            scaleTransform.xScale *= scaleFactor;
            scaleTransform.yScale *= scaleFactor;
        }
    }


}
