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
            id: mainMouseArea
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton
            hoverEnabled: true
            cursorShape: {
                if (!uiState || !uiState.isCropping) return Qt.ArrowCursor
                // Use a simple cross cursor for crop mode - edge detection would require tracking mouse position
                // which is complex in QML. The edge dragging will still work based on click position.
                return Qt.CrossCursor
            }
        
        // Drag-to-pan with drag-and-drop when dragging outside window
        property real lastX: 0
        property real lastY: 0
        property real startX: 0
        property real startY: 0
        property bool isDraggingOutside: false
        property int dragThreshold: 10  // Minimum distance before checking for outside drag
        property bool isCropDragging: false
        property real cropStartX: 0
        property real cropStartY: 0

        property string cropDragMode: "none" // "none", "new", "move", "left", "right", "top", "bottom", "topleft", "topright", "bottomleft", "bottomright"
        property real cropBoxStartLeft: 0
        property real cropBoxStartTop: 0
        property real cropBoxStartRight: 0
        property real cropBoxStartBottom: 0
        
        onPressed: function(mouse) {
            lastX = mouse.x
            lastY = mouse.y
            startX = mouse.x
            startY = mouse.y
            isDraggingOutside = false
            
            if (uiState && uiState.isCropping) {
                // Check if clicking on existing crop box
                var cropRect = getCropRect()
                var edgeThreshold = 10 * Screen.devicePixelRatio
                var inside = mouse.x >= cropRect.x && mouse.x <= cropRect.x + cropRect.width &&
                             mouse.y >= cropRect.y && mouse.y <= cropRect.y + cropRect.height
                
                if (inside && cropRect.width > 0 && cropRect.height > 0) {
                    // Determine which edge/corner is being dragged
                    var nearLeft = Math.abs(mouse.x - cropRect.x) < edgeThreshold
                    var nearRight = Math.abs(mouse.x - (cropRect.x + cropRect.width)) < edgeThreshold
                    var nearTop = Math.abs(mouse.y - cropRect.y) < edgeThreshold
                    var nearBottom = Math.abs(mouse.y - (cropRect.y + cropRect.height)) < edgeThreshold
                    
                    if (nearLeft && nearTop) cropDragMode = "topleft"
                    else if (nearRight && nearTop) cropDragMode = "topright"
                    else if (nearLeft && nearBottom) cropDragMode = "bottomleft"
                    else if (nearRight && nearBottom) cropDragMode = "bottomright"
                    else if (nearLeft) cropDragMode = "left"
                    else if (nearRight) cropDragMode = "right"
                    else if (nearTop) cropDragMode = "top"
                    else if (nearBottom) cropDragMode = "bottom"
                    else cropDragMode = "move"
                    
                    // Store initial crop box
                    var box = uiState.currentCropBox
                    cropBoxStartLeft = box[0]
                    cropBoxStartTop = box[1]
                    cropBoxStartRight = box[2]
                    cropBoxStartBottom = box[3]
                } else {
                    // Start new crop rectangle
                    cropDragMode = "new"
                    cropStartX = mouse.x
                    cropStartY = mouse.y
                }
                isCropDragging = true
            }
        }
        
        function getCropRect() {
            if (!mainImage.source || !uiState || !uiState.currentCropBox) {
                return {x: 0, y: 0, width: 0, height: 0}
            }
            var imgWidth = mainImage.paintedWidth
            var imgHeight = mainImage.paintedHeight
            var imgX = (mainImage.width - imgWidth) / 2
            var imgY = (mainImage.height - imgHeight) / 2
            var box = uiState.currentCropBox
            return {
                x: imgX + (box[0] / 1000) * imgWidth,
                y: imgY + (box[1] / 1000) * imgHeight,
                width: (box[2] - box[0]) / 1000 * imgWidth,
                height: (box[3] - box[1]) / 1000 * imgHeight
            }
        }

        onPositionChanged: function(mouse) {
            if (uiState && uiState.isCropping && isCropDragging) {
                if (cropDragMode === "new") {
                    // Update crop rectangle while dragging
                    updateCropBox(cropStartX, cropStartY, mouse.x, mouse.y)
                } else if (cropDragMode !== "none") {
                    // Refine existing crop box
                    var cropRect = getCropRect()
                    var imgWidth = mainImage.paintedWidth
                    var imgHeight = mainImage.paintedHeight
                    var imgX = (mainImage.width - imgWidth) / 2
                    var imgY = (mainImage.height - imgHeight) / 2
                    
                    // Convert mouse position to normalized coordinates
                    var mouseX = (mouse.x - imgX) / imgWidth
                    var mouseY = (mouse.y - imgY) / imgHeight
                    mouseX = Math.max(0, Math.min(1, mouseX)) * 1000
                    mouseY = Math.max(0, Math.min(1, mouseY)) * 1000
                    
                    var left = cropBoxStartLeft
                    var top = cropBoxStartTop
                    var right = cropBoxStartRight
                    var bottom = cropBoxStartBottom
                    
                    // Adjust based on drag mode
                    if (cropDragMode === "move") {
                        var dx = mouseX - (cropBoxStartLeft + cropBoxStartRight) / 2
                        var dy = mouseY - (cropBoxStartTop + cropBoxStartBottom) / 2
                        var width = cropBoxStartRight - cropBoxStartLeft
                        var height = cropBoxStartBottom - cropBoxStartTop
                        left = Math.max(0, Math.min(1000 - width, cropBoxStartLeft + dx))
                        top = Math.max(0, Math.min(1000 - height, cropBoxStartTop + dy))
                        right = left + width
                        bottom = top + height
                    } else if (cropDragMode === "left") {
                        left = Math.max(0, Math.min(right - 10, mouseX))
                    } else if (cropDragMode === "right") {
                        right = Math.max(left + 10, Math.min(1000, mouseX))
                    } else if (cropDragMode === "top") {
                        top = Math.max(0, Math.min(bottom - 10, mouseY))
                    } else if (cropDragMode === "bottom") {
                        bottom = Math.max(top + 10, Math.min(1000, mouseY))
                    } else if (cropDragMode === "topleft") {
                        left = Math.max(0, Math.min(right - 10, mouseX))
                        top = Math.max(0, Math.min(bottom - 10, mouseY))
                    } else if (cropDragMode === "topright") {
                        right = Math.max(left + 10, Math.min(1000, mouseX))
                        top = Math.max(0, Math.min(bottom - 10, mouseY))
                    } else if (cropDragMode === "bottomleft") {
                        left = Math.max(0, Math.min(right - 10, mouseX))
                        bottom = Math.max(top + 10, Math.min(1000, mouseY))
                    } else if (cropDragMode === "bottomright") {
                        right = Math.max(left + 10, Math.min(1000, mouseX))
                        bottom = Math.max(top + 10, Math.min(1000, mouseY))
                    }
                    
                    // Apply aspect ratio if needed
                    if (uiState.currentAspectRatioIndex > 0 && uiState.aspectRatioNames && uiState.aspectRatioNames.length > uiState.currentAspectRatioIndex) {
                        var ratioName = uiState.aspectRatioNames[uiState.currentAspectRatioIndex]
                        var ratio = getAspectRatio(ratioName)
                        if (ratio) {
                            var targetAspect = ratio[0] / ratio[1]
                            var currentWidth = right - left
                            var currentHeight = bottom - top
                            var currentAspect = currentWidth / currentHeight
                            
                            if (currentAspect > targetAspect) {
                                var newHeight = currentWidth / targetAspect
                                var centerY = (top + bottom) / 2
                                top = Math.max(0, centerY - newHeight / 2)
                                bottom = Math.min(1000, top + newHeight)
                            } else {
                                var newWidth = currentHeight * targetAspect
                                var centerX = (left + right) / 2
                                left = Math.max(0, centerX - newWidth / 2)
                                right = Math.min(1000, left + newWidth)
                            }
                        }
                    }
                    
                    uiState.currentCropBox = [Math.round(left), Math.round(top), Math.round(right), Math.round(bottom)]
                }
                return
            }
            
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
                
                // Normal pan behavior (only when not cropping)
                if (!uiState || !uiState.isCropping) {
                    panTransform.x += (mouse.x - lastX)
                    panTransform.y += (mouse.y - lastY)
                    lastX = mouse.x
                    lastY = mouse.y
                }
            }
        }
        
        onReleased: function(mouse) {
            isDraggingOutside = false
            if (uiState && uiState.isCropping && isCropDragging) {
                isCropDragging = false
                cropDragMode = "none"
            }
        }

        // Wheel for zoom
        onWheel: function(wheel) {
            // A real implementation would be more complex, zooming
            // into the cursor position.
            var scaleFactor = wheel.angleDelta.y > 0 ? 1.2 : 1 / 1.2;
            scaleTransform.xScale *= scaleFactor;
            scaleTransform.yScale *= scaleFactor;
        }
        
        function updateCropBox(x1, y1, x2, y2) {
            if (!uiState || !mainImage.source) return
            
            // Get image display bounds (accounting for PreserveAspectFit)
            var imgWidth = mainImage.paintedWidth
            var imgHeight = mainImage.paintedHeight
            var imgX = (mainImage.width - imgWidth) / 2
            var imgY = (mainImage.height - imgHeight) / 2
            
            // Convert mouse coordinates to image coordinates
            var imgCoordX1 = (x1 - imgX) / imgWidth
            var imgCoordY1 = (y1 - imgY) / imgHeight
            var imgCoordX2 = (x2 - imgX) / imgWidth
            var imgCoordY2 = (y2 - imgY) / imgHeight
            
            // Clamp to image bounds
            imgCoordX1 = Math.max(0, Math.min(1, imgCoordX1))
            imgCoordY1 = Math.max(0, Math.min(1, imgCoordY1))
            imgCoordX2 = Math.max(0, Math.min(1, imgCoordX2))
            imgCoordY2 = Math.max(0, Math.min(1, imgCoordY2))
            
            // Ensure left < right and top < bottom
            var left = Math.min(imgCoordX1, imgCoordX2) * 1000
            var right = Math.max(imgCoordX1, imgCoordX2) * 1000
            var top = Math.min(imgCoordY1, imgCoordY2) * 1000
            var bottom = Math.max(imgCoordY1, imgCoordY2) * 1000
            
            // Apply aspect ratio constraint if selected (index 0 is Freeform, so skip it)
            if (uiState.currentAspectRatioIndex > 0 && uiState.aspectRatioNames && uiState.aspectRatioNames.length > uiState.currentAspectRatioIndex) {
                var ratioName = uiState.aspectRatioNames[uiState.currentAspectRatioIndex]
                var ratio = getAspectRatio(ratioName)
                if (ratio) {
                    var currentWidth = right - left
                    var currentHeight = bottom - top
                    var targetAspect = ratio[0] / ratio[1]
                    var currentAspect = currentWidth / currentHeight
                    
                    if (currentAspect > targetAspect) {
                        // Too wide, adjust height
                        var newHeight = currentWidth / targetAspect
                        var centerY = (top + bottom) / 2
                        top = centerY - newHeight / 2
                        bottom = centerY + newHeight / 2
                        // Clamp to image bounds
                        if (top < 0) {
                            bottom += -top
                            top = 0
                        }
                        if (bottom > 1000) {
                            top -= (bottom - 1000)
                            bottom = 1000
                        }
                    } else {
                        // Too tall, adjust width
                        var newWidth = currentHeight * targetAspect
                        var centerX = (left + right) / 2
                        left = centerX - newWidth / 2
                        right = centerX + newWidth / 2
                        // Clamp to image bounds
                        if (left < 0) {
                            right += -left
                            left = 0
                        }
                        if (right > 1000) {
                            left -= (right - 1000)
                            right = 1000
                        }
                    }
                }
            }
            
            uiState.currentCropBox = [Math.round(left), Math.round(top), Math.round(right), Math.round(bottom)]
        }
        
        function getAspectRatio(name) {
            // Map aspect ratio names to ratios
            if (name === "1:1 (Square)") return [1, 1]
            if (name === "4:5 (Portrait)") return [4, 5]
            if (name === "1.91:1 (Landscape)") return [191, 100]
            if (name === "9:16 (Story)") return [9, 16]
            return null
        }
        
        function updateCropBoxFromAspectRatio() {
            if (!uiState || !uiState.currentCropBox || uiState.currentCropBox.length !== 4) return
            var box = uiState.currentCropBox
            updateCropBox(
                box[0] / 1000 * mainImage.paintedWidth + (mainImage.width - mainImage.paintedWidth) / 2,
                box[1] / 1000 * mainImage.paintedHeight + (mainImage.height - mainImage.paintedHeight) / 2,
                box[2] / 1000 * mainImage.paintedWidth + (mainImage.width - mainImage.paintedWidth) / 2,
                box[3] / 1000 * mainImage.paintedHeight + (mainImage.height - mainImage.paintedHeight) / 2
            )
        }
    }
    
    // Crop rectangle overlay
    Item {
        id: cropOverlay
        visible: uiState && uiState.isCropping && uiState.currentCropBox
        anchors.fill: parent
        z: 100
        
        property var cropBox: uiState ? uiState.currentCropBox : [0, 0, 1000, 1000]
        
        onCropBoxChanged: {
            if (!mainImage.source) return
            updateCropRect()
        }
        
        Component.onCompleted: {
            if (mainImage.source) updateCropRect()
        }
        
        Connections {
            target: mainImage
            function onPaintedWidthChanged() { if (cropOverlay.visible) cropOverlay.updateCropRect() }
            function onPaintedHeightChanged() { if (cropOverlay.visible) cropOverlay.updateCropRect() }
        }
        
        function updateCropRect() {
            if (!mainImage.source) return
            
            var imgWidth = mainImage.paintedWidth
            var imgHeight = mainImage.paintedHeight
            var imgX = (mainImage.width - imgWidth) / 2
            var imgY = (mainImage.height - imgHeight) / 2
            
            var left = imgX + (cropBox[0] / 1000) * imgWidth
            var top = imgY + (cropBox[1] / 1000) * imgHeight
            var right = imgX + (cropBox[2] / 1000) * imgWidth
            var bottom = imgY + (cropBox[3] / 1000) * imgHeight
            
            cropRect.x = left
            cropRect.y = top
            cropRect.width = right - left
            cropRect.height = bottom - top
        }
        
        // Semi-transparent overlay - draw 4 rectangles around the crop area
        Rectangle {
            // Top
            x: 0
            y: 0
            width: parent.width
            height: cropRect.y
            color: "black"
            opacity: 0.3
        }
        Rectangle {
            // Bottom
            x: 0
            y: cropRect.y + cropRect.height
            width: parent.width
            height: parent.height - (cropRect.y + cropRect.height)
            color: "black"
            opacity: 0.3
        }
        Rectangle {
            // Left
            x: 0
            y: cropRect.y
            width: cropRect.x
            height: cropRect.height
            color: "black"
            opacity: 0.3
        }
        Rectangle {
            // Right
            x: cropRect.x + cropRect.width
            y: cropRect.y
            width: parent.width - (cropRect.x + cropRect.width)
            height: cropRect.height
            color: "black"
            opacity: 0.3
        }
        
        // Crop rectangle with thick white border
        Rectangle {
            id: cropRect
            color: "transparent"
            border.color: "white"
            border.width: 3
        }
    }
    
    // Aspect ratio selector window (upper left corner)
    Rectangle {
        id: aspectRatioWindow
        visible: uiState && uiState.isCropping
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.margins: 10
        width: 200
        height: Math.max(150, aspectRatioColumn.implicitHeight + 20)
        color: "#333333"
        border.color: "#666666"
        border.width: 1
        radius: 4
        z: 1000
        
        // Try to get root from parent hierarchy
        property bool isDark: root.isDarkTheme
        
        Component.onCompleted: {
            // Update colors based on theme
            color = isDark ? "#333333" : "#f0f0f0"
            border.color = isDark ? "#666666" : "#cccccc"
        }
        
        Column {
            id: aspectRatioColumn
            anchors.fill: parent
            anchors.margins: 10
            spacing: 5
            
            Text {
                text: "Aspect Ratio"
                font.bold: true
                color: "white"
                font.pixelSize: 12
            }
            
            Repeater {
                model: uiState && uiState.aspectRatioNames ? uiState.aspectRatioNames.length : 0
                
                Rectangle {
                    width: parent.width
                    height: 30
                    color: uiState && uiState.currentAspectRatioIndex === index ? "#555555" : "transparent"
                    radius: 3
                    
                    Text {
                        anchors.left: parent.left
                        anchors.leftMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        text: uiState && uiState.aspectRatioNames ? uiState.aspectRatioNames[index] : ""
                        color: "white"
                        font.pixelSize: 11
                    }
                    
                    MouseArea {
                        anchors.fill: parent
                        onClicked: {
                            if (uiState) {
                                uiState.currentAspectRatioIndex = index
                                // Re-apply aspect ratio to current crop box
                                if (uiState.currentCropBox && uiState.currentCropBox.length === 4) {
                                    mainMouseArea.updateCropBoxFromAspectRatio()
                                }
                            }
                        }
                    }
                }
            }
        }
    }


}
