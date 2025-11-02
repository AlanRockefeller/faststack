import QtQuick

// This file is intended to hold QML components like the main image view.
// For simplicity, we'll start with just the main image view.

Item {
    id: loupeView
    anchors.fill: parent

    // The main image display
    Image {
        id: mainImage
        anchors.fill: parent
        source: uiState && uiState.imageCount > 0 ? uiState.currentImageSource : ""
        fillMode: Image.PreserveAspectFit
        cache: false // We do our own caching in Python

        // Zoom and Pan logic would go here
        // For example, using PinchArea or MouseArea
        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton
            
            // Simple drag-to-pan placeholder
            property real lastX: 0
            property real lastY: 0

            onPressed: {
                lastX = mouseX
                lastY = mouseY
            }

            onPositionChanged: {
                if (pressed) {
                    mainImage.x += (mouseX - lastX)
                    mainImage.y += (mouseY - lastY)
                    lastX = mouseX
                    lastY = mouseY
                }
            }

            // Wheel for zoom
            onWheel: {
                // A real implementation would be more complex, zooming
                // into the cursor position.
                var scaleFactor = wheel.angleDelta.y > 0 ? 1.2 : 1 / 1.2;
                mainImage.scale *= scaleFactor;
            }
        }
    }


}
