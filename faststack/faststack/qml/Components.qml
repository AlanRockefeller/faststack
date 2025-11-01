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
        source: uiState && uiState.currentImageSource ? uiState.currentImageSource : ""
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

    // Overlay for metadata
    Rectangle {
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: 40
        color: "#80000000" // Semi-transparent black

        Row {
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: parent.left
            anchors.leftMargin: 10
            spacing: 15

            Text {
                text: uiState && uiState.currentFilename ? uiState.currentFilename : ""
                color: "white"
                font.pixelSize: 14
            }
            Text {
                text: uiState && uiState.isFlagged ? `[${uiState.isFlagged ? 'F' : ''}]` : ""
                color: "lightgreen"
                font.bold: true
            }
             Text {
                text: uiState && uiState.isRejected ? `[${uiState.isRejected ? 'X' : ''}]` : ""
                color: "red"
                font.bold: true
            }
        }
    }
}
