import QtQuick
import QtQuick.Window

// This file is intended to hold QML components like the main image view.
// For simplicity, we'll start with just the main image view.

Item {
    id: loupeView
    anchors.fill: parent
    focus: true
    
    Keys.onEscapePressed: {
        if (uiState && uiState.isCropping && mainMouseArea.isRotating) {
            mainMouseArea.isRotating = false
            mainMouseArea.cropDragMode = "none"
            mainMouseArea.isCropDragging = false
            event.accepted = true
        }
    }

    Keys.onReturnPressed: {
        if (uiState && uiState.isCropping && controller) {
            controller.execute_crop()
            event.accepted = true
        }
    }
    Keys.onEnterPressed: {
        if (uiState && uiState.isCropping && controller) {
            controller.execute_crop()
            event.accepted = true
        }
    }


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
	smooth: uiState && !uiState.anySliderPressed && !isZooming
	mipmap: uiState && !uiState.anySliderPressed && !isZooming
        
        property bool isZooming: false

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
            
            // Update histogram with zoom/pan info if histogram is visible
            if (uiState && uiState.isHistogramVisible && controller) {
                var zoom = scaleTransform.xScale
                var panX = panTransform.x
                var panY = panTransform.y
                // Calculate image scale (painted size vs actual size)
                var imageScale = mainImage.paintedWidth > 0 ? (mainImage.paintedWidth / mainImage.sourceSize.width) : 1.0
                controller.update_histogram(zoom, panX, panY, imageScale)
            }
        }
        
        function updateHistogramWithZoom() {
            if (uiState && uiState.isHistogramVisible && controller) {
                var zoom = scaleTransform.xScale
                var panX = panTransform.x
                var panY = panTransform.y
                var imageScale = mainImage.paintedWidth > 0 ? (mainImage.paintedWidth / mainImage.sourceSize.width) : 1.0
                controller.update_histogram(zoom, panX, panY, imageScale)
            }
        }

        property alias scaleTransform: scaleTransform
        property alias panTransform: panTransform
        
        transform: [
            Scale {
                id: scaleTransform
                origin.x: mainImage.width / 2
                origin.y: mainImage.height / 2
                onXScaleChanged: {
                    mainImage.updateZoomState()
                    mainImage.updateHistogramWithZoom()
                    if (cropOverlay.visible) cropOverlay.updateCropRect()
                }
                onYScaleChanged: {
                    mainImage.updateZoomState()
                    mainImage.updateHistogramWithZoom()
                    if (cropOverlay.visible) cropOverlay.updateCropRect()
                }
            },
            Translate {
                id: panTransform
                onXChanged: {
                    mainImage.updateHistogramWithZoom()
                    if (cropOverlay.visible) cropOverlay.updateCropRect()
                }
                onYChanged: {
                    mainImage.updateHistogramWithZoom()
                    if (cropOverlay.visible) cropOverlay.updateCropRect()
                }
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
            acceptedButtons: Qt.LeftButton | Qt.RightButton
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
        property real cropRotation: 0
        property bool isRotating: false
        property real cropStartAngle: 0
        property real cropStartRotation: 0
        onCropRotationChanged: uiState.cropRotation = cropRotation

        Connections {
            target: uiState
            function onCropRotationChanged() {
                if (mainMouseArea.cropRotation !== uiState.cropRotation) {
                    mainMouseArea.cropRotation = uiState.cropRotation
                }
            }
        }

        onIsRotatingChanged: {
            if (uiState) {
                if (isRotating) {
                    uiState.statusMessage = "Press ESC to exit rotate mode"
                } else {
                    if (uiState.statusMessage === "Press ESC to exit rotate mode") {
                        uiState.statusMessage = ""
                    }
                }
            }
        }
        
        onPressed: function(mouse) {
            lastX = mouse.x
            lastY = mouse.y
            startX = mouse.x
            startY = mouse.y
            isDraggingOutside = false
            
            if (mouse.button === Qt.RightButton) {
                if (uiState && uiState.isCropping) {
                    // Cancel crop mode if already active
                    if (controller) controller.cancel_crop_mode()
                } else if (uiState) {
                    // Enter crop mode and start new crop
                    uiState.isCropping = true
                    
                    // Set up new crop state
                    cropDragMode = "new"
                    cropStartX = mouse.x
                    cropStartY = mouse.y
                    
                    // Initialize anchors
                    var startCoords = mapToImageCoordinates(Qt.point(mouse.x, mouse.y))
                    // Clamp to [0, 1] and convert to [0, 1000]
                    var startNormX = Math.max(0, Math.min(1, startCoords.x)) * 1000
                    var startNormY = Math.max(0, Math.min(1, startCoords.y)) * 1000
                    
                    cropBoxStartLeft = startNormX
                    cropBoxStartRight = startNormX
                    cropBoxStartTop = startNormY
                    cropBoxStartBottom = startNormY
                    
                    isCropDragging = true
                }
                return
            }
            
            if (uiState && uiState.isCropping) {
                // Check if clicking on existing crop box
                var cropGeo = getCropRect()
                var box = uiState.currentCropBox
                var isFullImage = box && box.length === 4 && box[0] === 0 && box[1] === 0 && box[2] === 1000 && box[3] === 1000
                
                var edgeThreshold = 10 * Screen.devicePixelRatio
                var inside = mouse.x >= cropGeo.x && mouse.x <= cropGeo.x + cropGeo.width &&
                             mouse.y >= cropGeo.y && mouse.y <= cropGeo.y + cropGeo.height
                
                // --- Hit test for rotation handle (robust: uses actual knob transform) ---
                if (mainMouseArea.isRotating && cropOverlay.visible && rotateKnob.visible) {
                    // knob center in mainMouseArea coords (includes cropRect rotation)
                    var k = mainMouseArea.mapFromItem(rotateKnob, rotateKnob.width/2, rotateKnob.height/2)
                    var dxk = mouse.x - k.x
                    var dyk = mouse.y - k.y
                    var distk = Math.sqrt(dxk*dxk + dyk*dyk)

                    if (distk < 22) { // a little forgiving
                        cropDragMode = "rotate"

                        // crop center in mainMouseArea coords (includes rotation)
                        var c = mainMouseArea.mapFromItem(cropRect, cropRect.width/2, cropRect.height/2)
                        cropStartAngle = Math.atan2(mouse.y - c.y, mouse.x - c.x) * 180 / Math.PI
                        cropStartRotation = cropRotation

                        isCropDragging = true
                        return
                    }
                }
                // If crop box is full image, always start a new crop
                else if (isFullImage) {
                    cropDragMode = "new"
                    cropStartX = mouse.x
                    cropStartY = mouse.y
                } else if (inside && cropGeo.width > 0 && cropGeo.height > 0) {
                    // Determine which edge/corner is being dragged
                    var nearLeft = Math.abs(mouse.x - cropGeo.x) < edgeThreshold
                    var nearRight = Math.abs(mouse.x - (cropGeo.x + cropGeo.width)) < edgeThreshold
                    var nearTop = Math.abs(mouse.y - cropGeo.y) < edgeThreshold
                    var nearBottom = Math.abs(mouse.y - (cropGeo.y + cropGeo.height)) < edgeThreshold
                    
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
                    if (!box || box.length !== 4) return
                    cropBoxStartLeft = box[0]
                    cropBoxStartTop = box[1]
                    cropBoxStartRight = box[2]
                    cropBoxStartBottom = box[3]
                } else {
                    // Start new crop rectangle
                    cropDragMode = "new"
                    cropStartX = mouse.x
                    cropStartY = mouse.y
                    
                    // Initialize anchors for aspect ratio constraint using normalized coordinates
                    var startCoords = mapToImageCoordinates(Qt.point(mouse.x, mouse.y))
                    // Clamp to [0, 1] and convert to [0, 1000]
                    var startNormX = Math.max(0, Math.min(1, startCoords.x)) * 1000
                    var startNormY = Math.max(0, Math.min(1, startCoords.y)) * 1000
                    
                    cropBoxStartLeft = startNormX
                    cropBoxStartRight = startNormX
                    cropBoxStartTop = startNormY
                    cropBoxStartBottom = startNormY
                }
                isCropDragging = true
            }
        }        
        function getCropRect() {
            if (!mainImage.source || !uiState || !uiState.currentCropBox || uiState.currentCropBox.length !== 4) {
                return {x: 0, y: 0, width: 0, height: 0}
            }
            var imgWidth = mainImage.paintedWidth
            var imgHeight = mainImage.paintedHeight
            var imgX = (mainImage.width - imgWidth) / 2
            var imgY = (mainImage.height - imgHeight) / 2
            var box = uiState.currentCropBox
            
            // Account for zoom and pan transforms when displaying crop box
            var scale = scaleTransform.xScale
            var panX = panTransform.x
            var panY = panTransform.y
            
            // Convert normalized crop box (0-1000) to image-local coordinates
            var localX = (box[0] / 1000) * imgWidth
            var localY = (box[1] / 1000) * imgHeight
            var localWidth = (box[2] - box[0]) / 1000 * imgWidth
            var localHeight = (box[3] - box[1]) / 1000 * imgHeight
            
            // Apply zoom and pan transforms to get screen coordinates
            return {
                x: imgX + (localX * scale) + panX,
                y: imgY + (localY * scale) + panY,
                width: localWidth * scale,
                height: localHeight * scale
            }
        }
        function mapToImageCoordinates(screenPoint) {
            var imgWidth = mainImage.paintedWidth
            var imgHeight = mainImage.paintedHeight
            var imgX = (mainImage.width - imgWidth) / 2
            var imgY = (mainImage.height - imgHeight) / 2
            
            var scale = scaleTransform.xScale
            var panX = panTransform.x
            var panY = panTransform.y

            // Inverse of getCropRect transform:
            // Screen = imgX + (Local * Scale) + Pan
            // Local = (Screen - Pan - imgX) / Scale
            
            var localX = (screenPoint.x - panX - imgX) / scale
            var localY = (screenPoint.y - panY - imgY) / scale

            return {x: localX / imgWidth, y: localY / imgHeight}
        }
        onPositionChanged: function(mouse) {
            if (uiState && uiState.isCropping && isCropDragging) {
                if (cropDragMode === "new") {
                    // Update crop rectangle while dragging
                    updateCropBox(cropStartX, cropStartY, mouse.x, mouse.y, true)
                } else if (cropDragMode === "rotate") {
                    var c = mainMouseArea.mapFromItem(cropRect, cropRect.width/2, cropRect.height/2)
                    var currentAngle = Math.atan2(mouse.y - c.y, mouse.x - c.x) * 180 / Math.PI
                    cropRotation = cropStartRotation + (currentAngle - cropStartAngle)
                    
                    // Update rotation in backend live
                    if (controller) {
                        console.log("Rotating: " + cropRotation)
                        controller.set_straighten_angle(cropRotation)
                    }
                } else if (cropDragMode !== "none") {
                    
                    var coords = mapToImageCoordinates(Qt.point(mouse.x, mouse.y))

                    // Clamp to image bounds and convert to 0-1000 range
                    var mouseX = Math.max(0, Math.min(1, coords.x)) * 1000
                    var mouseY = Math.max(0, Math.min(1, coords.y)) * 1000
                    
                    var left = cropBoxStartLeft
                    var top = cropBoxStartTop
                    var right = cropBoxStartRight
                    var bottom = cropBoxStartBottom
                    
                    // Adjust based on drag mode
                    if (cropDragMode === "move") {
                        var startCenterX = (cropBoxStartLeft + cropBoxStartRight) / 2
                        var startCenterY = (cropBoxStartTop + cropBoxStartBottom) / 2
                        
                        var dx = mouseX - startCenterX
                        var dy = mouseY - startCenterY

                        var width = cropBoxStartRight - cropBoxStartLeft
                        var height = cropBoxStartBottom - cropBoxStartTop

                        left = Math.max(0, Math.min(1000 - width, cropBoxStartLeft + dx))
                        top = Math.max(0, Math.min(1000 - height, cropBoxStartTop + dy))
                        right = left + width
                        bottom = top + height
                    } else {
                        if (cropDragMode.includes("left")) left = mouseX;
                        if (cropDragMode.includes("right")) right = mouseX;
                        if (cropDragMode.includes("top")) top = mouseY;
                        if (cropDragMode.includes("bottom")) bottom = mouseY;

                        var constrainedBox = applyAspectRatioConstraint(left, top, right, bottom, cropDragMode)
                        left = constrainedBox[0]
                        top = constrainedBox[1]
                        right = constrainedBox[2]
                        bottom = constrainedBox[3]
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

        // Wheel for zoom - zooms in towards cursor, zooms out towards center
        onWheel: function(wheel) {
            // Disable smooth rendering during zoom for better performance
            mainImage.isZooming = true
            
            // Use a smaller scale factor for smoother, more responsive zoom
            var isZoomingIn = wheel.angleDelta.y > 0
            var scaleFactor = isZoomingIn ? 1.1 : 1 / 1.1;
            
            // Calculate old and new scale
            var oldScale = scaleTransform.xScale
            var newScale = oldScale * scaleFactor
            newScale = Math.max(0.1, Math.min(20.0, newScale))
            
            // Get the image's painted (displayed) bounds
            var imgWidth = mainImage.paintedWidth
            var imgHeight = mainImage.paintedHeight
            var centerX = mainImage.width / 2
            var centerY = mainImage.height / 2
            
            if (isZoomingIn) {
                // Zoom in: zoom towards cursor position
                var mouseX = wheel.x
                var mouseY = wheel.y
                var imgX = (mainImage.width - imgWidth) / 2
                var imgY = (mainImage.height - imgHeight) / 2
                
                // Calculate the point in the image that's under the cursor
                var pointInImageX = mouseX - imgX
                var pointInImageY = mouseY - imgY
                
                // Only zoom towards cursor if cursor is over the image
                if (pointInImageX >= 0 && pointInImageX <= imgWidth && 
                    pointInImageY >= 0 && pointInImageY <= imgHeight) {
                    
                    // Calculate offset from image center in screen coordinates
                    var centerOffsetX = pointInImageX - imgWidth / 2
                    var centerOffsetY = pointInImageY - imgHeight / 2
                    
                    // The current screen position of a point is: (imgPoint * oldScale) + oldPan + center
                    // We want to find what's currently under the cursor and keep it there
                    // Instead of dividing by oldScale (which loses precision), work with scaled values
                    
                    // Calculate what the scaled image point currently is (before zoom)
                    // This is: (centerOffset - pan) which represents (imgPoint * oldScale)
                    var scaledImagePointX = centerOffsetX - panTransform.x
                    var scaledImagePointY = centerOffsetY - panTransform.y
                    
                    // Adjust the scale origin to the cursor position
                    scaleTransform.origin.x = mouseX
                    scaleTransform.origin.y = mouseY
                    
                    // Apply the new scale first
                    scaleTransform.xScale = newScale
                    scaleTransform.yScale = newScale
                    
                    // After zoom, the scaled image point becomes: scaledImagePoint * (newScale / oldScale)
                    // We want it to stay at the same screen position, so:
                    // newPan = centerOffset - (scaledImagePoint * newScale / oldScale)
                    // Use scaleRatio to avoid precision loss from repeated division
                    var scaleRatio = newScale / oldScale
                    var newPanX = centerOffsetX - (scaledImagePointX * scaleRatio)
                    var newPanY = centerOffsetY - (scaledImagePointY * scaleRatio)
                    
                    // Apply the adjusted pan
                    panTransform.x = newPanX
                    panTransform.y = newPanY
                } else {
                    // If cursor is outside image, zoom from center
                    scaleTransform.origin.x = centerX
                    scaleTransform.origin.y = centerY
                    scaleTransform.xScale = newScale
                    scaleTransform.yScale = newScale
                }
            } else {
                // Zoom out: always zoom towards center of screen, but keep current origin logic
                // The issue is switching origin abruptly causes jumps.
                // If we are zoomed in, we should zoom out relative to the current view center or cursor.
                
                // If we simply zoom out without changing origin, it zooms out from wherever the origin currently is.
                // If the origin was set to a specific point during zoom in, keeping it there is fine.
                // Resetting origin to center (centerX, centerY) causes the jump because the image shifts to align its center with the new origin.
                
                // Let's keep the current origin unless we are fully zoomed out.
                // Or better: zoom out relative to the cursor just like zooming in, which feels most natural.
                
                var mouseX = wheel.x
                var mouseY = wheel.y
                
                // Use cursor as origin for zoom out too
                scaleTransform.origin.x = mouseX
                scaleTransform.origin.y = mouseY
                
                // We need similar pan compensation to keep the point under cursor stable
                var imgWidth = mainImage.paintedWidth
                var imgHeight = mainImage.paintedHeight
                var imgX = (mainImage.width - imgWidth) / 2
                var imgY = (mainImage.height - imgHeight) / 2
                var pointInImageX = mouseX - imgX
                var pointInImageY = mouseY - imgY
                
                var centerOffsetX = pointInImageX - imgWidth / 2
                var centerOffsetY = pointInImageY - imgHeight / 2
                var scaledImagePointX = centerOffsetX - panTransform.x
                var scaledImagePointY = centerOffsetY - panTransform.y
                
                scaleTransform.xScale = newScale
                scaleTransform.yScale = newScale
                
                var scaleRatio = newScale / oldScale
                var newPanX = centerOffsetX - (scaledImagePointX * scaleRatio)
                var newPanY = centerOffsetY - (scaledImagePointY * scaleRatio)
                
                panTransform.x = newPanX
                panTransform.y = newPanY
            }
            
            // Re-enable smooth rendering after a short delay
            zoomSmoothTimer.restart()
        }
        
        Timer {
            id: zoomSmoothTimer
            interval: 150  // Re-enable smooth rendering 150ms after last zoom
            onTriggered: {
                mainImage.isZooming = false
            }
        }
        
        function updateCropBox(x1, y1, x2, y2, applyAspectRatio = false) {
            if (!uiState || !mainImage.source) return

            var imgCoord1 = mapToImageCoordinates(Qt.point(x1, y1))
            var imgCoord2 = mapToImageCoordinates(Qt.point(x2, y2))
            
            // Clamp to image bounds (normalized 0-1)
            var imgCoordX1 = Math.max(0, Math.min(1, imgCoord1.x))
            var imgCoordY1 = Math.max(0, Math.min(1, imgCoord1.y))
            var imgCoordX2 = Math.max(0, Math.min(1, imgCoord2.x))
            var imgCoordY2 = Math.max(0, Math.min(1, imgCoord2.y))
            
            // Calculate raw box in 0-1000 space
            var left = Math.min(imgCoordX1, imgCoordX2) * 1000
            var right = Math.max(imgCoordX1, imgCoordX2) * 1000
            var top = Math.min(imgCoordY1, imgCoordY2) * 1000
            var bottom = Math.max(imgCoordY1, imgCoordY2) * 1000
            
            // Determine primary drag direction for "new" mode (from anchor x1,y1 to mouse x2,y2)
            // We need to know which corner is the anchor to apply aspect ratio correctly
            // x1,y1 is anchor. x2,y2 is mouse.
            
            if (applyAspectRatio && mainImage.sourceSize) {
                // We need to pass the specific corner being dragged to applyAspectRatioConstraint
                // Since "new" creates a box from x1,y1 to x2,y2, we can infer the mode.
                var mode = "new"
                if (x2 >= x1 && y2 >= y1) mode = "bottomright"
                else if (x2 < x1 && y2 >= y1) mode = "bottomleft"
                else if (x2 >= x1 && y2 < y1) mode = "topright"
                else if (x2 < x1 && y2 < y1) mode = "topleft"
                
                // Pass the raw coordinates of the "mouse" corner (x2, y2) and the "anchor" corner (x1, y1)
                // But applyAspectRatioConstraint expects left, top, right, bottom.
                // It assumes one corner is fixed based on mode.
                // So we pass the current box, and it will adjust the moving corner.
                
                var constrainedBox = applyAspectRatioConstraint(left, top, right, bottom, mode)
                left = constrainedBox[0]
                top = constrainedBox[1]
                right = constrainedBox[2]
                bottom = constrainedBox[3]
            } else {
                // Just ensure minimum size
                if (right - left < 10) {
                    if (right < 1000) right = Math.min(1000, left + 10)
                    else left = Math.max(0, right - 10)
                }
                if (bottom - top < 10) {
                    if (bottom < 1000) bottom = Math.min(1000, top + 10)
                    else top = Math.max(0, bottom - 10)
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
            if (name === "16:9 (Wide)") return [16, 9]
            return null
        }
        
        function applyAspectRatioConstraint(left, top, right, bottom, dragMode) {
            if (uiState.currentAspectRatioIndex <= 0 || !uiState.aspectRatioNames || uiState.aspectRatioNames.length <= uiState.currentAspectRatioIndex) {
                // No aspect ratio, just clamp to bounds
                return [
                    Math.max(0, Math.min(1000, left)),
                    Math.max(0, Math.min(1000, top)),
                    Math.max(0, Math.min(1000, right)),
                    Math.max(0, Math.min(1000, bottom))
                ];
            }

            var ratioName = uiState.aspectRatioNames[uiState.currentAspectRatioIndex];
            var ratioPair = getAspectRatio(ratioName);
            if (!ratioPair || !mainImage.sourceSize || mainImage.sourceSize.width === 0 || mainImage.sourceSize.height === 0) {
                return [left, top, right, bottom];
            }

            // Calculate effective aspect ratio in 0-1000 normalized space
            // targetAspect (pixels) = width_px / height_px
            // width_px = width_norm * imgW / 1000
            // height_px = height_norm * imgH / 1000
            // targetAspect = (width_norm * imgW) / (height_norm * imgH)
            // width_norm / height_norm = targetAspect * (imgH / imgW)
            
            var pixelAspect = ratioPair[0] / ratioPair[1];
            var imageAspect = mainImage.sourceSize.width / mainImage.sourceSize.height;
            var targetAspect = pixelAspect * (1.0 / imageAspect); // Normalized aspect ratio

            var currentWidth = right - left;
            var currentHeight = bottom - top;

            // For "new" drag (which we mapped to specific corners in updateCropBox) or corner drags
            
            if (dragMode.includes("left") || dragMode.includes("right")) {
                // Edge drag (Left/Right) or Corner drag (where Width drives Height)
                // Standard behavior: Corner drags are driven by the dominant axis or strictly one axis?
                // Let's use the explicit corner logic below.
                // This block handles pure Edge drags.
                
                if (!dragMode.includes("top") && !dragMode.includes("bottom")) {
                     // Pure Left/Right drag: Adjust height symmetrically
                    var newWidth = right - left;
                    var newHeight = newWidth / targetAspect;
                    var vCenter = (cropBoxStartTop + cropBoxStartBottom) / 2;
                    
                    top = vCenter - newHeight / 2;
                    bottom = vCenter + newHeight / 2;
                    
                    // Clamp vertical
                    var clamped = false;
                    if (top < 0) {
                        top = 0;
                        bottom = newHeight;
                        if (bottom > 1000) { bottom = 1000; clamped = true; }
                    }
                    if (bottom > 1000) {
                        bottom = 1000;
                        top = 1000 - newHeight;
                        if (top < 0) { top = 0; clamped = true; }
                    }
                    
                    // If height was clamped, recalculate width
                    if (clamped) {
                        var finalHeight = bottom - top;
                        var finalWidth = finalHeight * targetAspect;
                        // Adjust left/right to match final width (anchor opposite side)
                        if (dragMode.includes("left")) {
                            left = right - finalWidth;
                        } else {
                            right = left + finalWidth;
                        }
                    }
                }
            } 
            
            if ((dragMode.includes("top") || dragMode.includes("bottom")) && !dragMode.includes("left") && !dragMode.includes("right")) {
                // Pure Top/Bottom drag: Adjust width symmetrically
                var newHeight = bottom - top;
                var newWidth = newHeight * targetAspect;
                var hCenter = (cropBoxStartLeft + cropBoxStartRight) / 2;
                
                left = hCenter - newWidth / 2;
                right = hCenter + newWidth / 2;
                
                // Clamp horizontal
                var clamped = false;
                if (left < 0) {
                    left = 0;
                    right = newWidth;
                    if (right > 1000) { right = 1000; clamped = true; }
                }
                if (right > 1000) {
                    right = 1000;
                    left = 1000 - newWidth;
                    if (left < 0) { left = 0; clamped = true; }
                }
                
                if (clamped) {
                    var finalWidth = right - left;
                    var finalHeight = finalWidth / targetAspect;
                    if (dragMode.includes("top")) {
                        top = bottom - finalHeight;
                    } else {
                        bottom = top + finalHeight;
                    }
                }
            }
            
            // Corner Drags
            if (dragMode.includes("topleft")) { // Corner: Top-Left (Anchor: Bottom-Right)
                var newW = right - left;
                var newH = newW / targetAspect;
                
                // Check bounds
                if (bottom - newH < 0) { // Top < 0
                    newH = bottom;
                    newW = newH * targetAspect;
                }
                if (right - newW < 0) { // Left < 0 (shouldn't happen if we started inside, but good to check)
                     // If we are here, it means even with max height, width is too big?
                     // Just clamp to 0
                }
                
                left = right - newW;
                top = bottom - newH;
                
            } else if (dragMode.includes("topright")) { // Corner: Top-Right (Anchor: Bottom-Left)
                var newW = right - left;
                var newH = newW / targetAspect;
                
                // Check bounds: top >= 0
                if (bottom - newH < 0) {
                    newH = bottom;
                    newW = newH * targetAspect;
                }
                // Check bounds: right <= 1000
                if (left + newW > 1000) {
                    newW = 1000 - left;
                    newH = newW / targetAspect;
                }
                
                right = left + newW;
                top = bottom - newH;
                
            } else if (dragMode.includes("bottomleft")) { // Corner: Bottom-Left (Anchor: Top-Right)
                var newW = right - left;
                var newH = newW / targetAspect;
                
                // Check bounds: bottom <= 1000
                if (top + newH > 1000) {
                    newH = 1000 - top;
                    newW = newH * targetAspect;
                }
                // Check bounds: left >= 0
                if (right - newW < 0) {
                    newW = right;
                    newH = newW / targetAspect;
                }
                
                left = right - newW;
                bottom = top + newH;
                
            } else if (dragMode.includes("bottomright")) { // Corner: Bottom-Right (Anchor: Top-Left)
                var newW = right - left;
                var newH = newW / targetAspect;
                
                // Check bounds: bottom <= 1000
                if (top + newH > 1000) {
                    newH = 1000 - top;
                    newW = newH * targetAspect;
                }
                // Check bounds: right <= 1000
                if (left + newW > 1000) {
                    newW = 1000 - left;
                    newH = newW / targetAspect;
                }
                
                right = left + newW;
                bottom = top + newH;
            }

            return [Math.round(left), Math.round(top), Math.round(right), Math.round(bottom)];
        }
        
        function updateCropBoxFromAspectRatio() {
            if (!uiState || !uiState.currentCropBox || uiState.currentCropBox.length !== 4) return
            var box = uiState.currentCropBox
            
            // Start with center of current box
            var cx = (box[0] + box[2]) / 2
            var cy = (box[1] + box[3]) / 2
            
            // If current box is basically full image (default), use image center
            if (box[0] <= 10 && box[1] <= 10 && box[2] >= 990 && box[3] >= 990) {
                cx = 500
                cy = 500
            }
            
            var ratioName = uiState.aspectRatioNames[uiState.currentAspectRatioIndex];
            var ratioPair = getAspectRatio(ratioName);

            if (!ratioPair) { // Freeform selected
                uiState.currentCropBox = [0, 0, 1000, 1000] // Reset to full image
                mainMouseArea.cropRotation = 0 // Also reset visual rotation
                mainMouseArea.isRotating = false
                mainMouseArea.cropDragMode = "none"
                return;
            }
            var targetAspect = ratioPair[0] / ratioPair[1];
            
            // Maximize width/height within 0-1000 centered at cx, cy
            // Distance to edges
            var maxW_half = Math.min(cx, 1000 - cx)
            var maxH_half = Math.min(cy, 1000 - cy)
            
            // Try fitting to width limits first
            var width = maxW_half * 2
            var height = width / targetAspect
            
            // If height exceeds limits, scale down
            if (height > maxH_half * 2) {
                height = maxH_half * 2
                width = height * targetAspect
            }
            
            // Also ensure we don't make a tiny box if cx,cy is near edge.
            // If box is too small (<100), re-center to image center (500,500)
            if (width < 100 || height < 100) {
                cx = 500; cy = 500;
                maxW_half = 500; maxH_half = 500;
                width = 1000;
                height = width / targetAspect;
                if (height > 1000) {
                    height = 1000;
                    width = height * targetAspect;
                }
            }
            
            var left = cx - width / 2
            var right = cx + width / 2
            var top = cy - height / 2
            var bottom = cy + height / 2
            
            uiState.currentCropBox = [Math.round(left), Math.round(top), Math.round(right), Math.round(bottom)]
        }
    }
    
    // Crop rectangle overlay
    Item {
        id: cropOverlay
        property var cropBox: uiState ? uiState.currentCropBox : [0, 0, 1000, 1000]
        property bool hasActiveCrop: cropBox
                                     && cropBox.length === 4
                                     && !(cropBox[0] === 0
                                          && cropBox[1] === 0
                                          && cropBox[2] === 1000
                                          && cropBox[3] === 1000)
        visible: uiState && uiState.isCropping && (hasActiveCrop || mainMouseArea.isRotating)
        anchors.fill: parent
        z: 100
        
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
        
        Connections {
            target: uiState
            function onCurrentCropBoxChanged() {
                cropOverlay.cropBox = uiState.currentCropBox
                if (cropOverlay.visible && mainImage.source) {
                    cropOverlay.updateCropRect()
                }
            }
        }
        
        function updateCropRect() {
            if (!mainImage.source) return
            
            var imgWidth = mainImage.paintedWidth
            var imgHeight = mainImage.paintedHeight
            var imgX = (mainImage.width - imgWidth) / 2
            var imgY = (mainImage.height - imgHeight) / 2
            
            // Account for zoom and pan transforms when displaying crop box
            var scale = mainImage.scaleTransform ? mainImage.scaleTransform.xScale : 1.0
            var panX = mainImage.panTransform ? mainImage.panTransform.x : 0
            var panY = mainImage.panTransform ? mainImage.panTransform.y : 0
            
            // Convert normalized crop box (0-1000) to image-local coordinates
            var localLeft = (cropBox[0] / 1000) * imgWidth
            var localTop = (cropBox[1] / 1000) * imgHeight
            var localRight = (cropBox[2] / 1000) * imgWidth
            var localBottom = (cropBox[3] / 1000) * imgHeight
            
            // Apply zoom and pan transforms to get screen coordinates
            var left = imgX + (localLeft * scale) + panX
            var top = imgY + (localTop * scale) + panY
            var right = imgX + (localRight * scale) + panX
            var bottom = imgY + (localBottom * scale) + panY
            
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
            rotation: mainMouseArea.cropRotation
            transformOrigin: Item.Center

            // Rotation Handle Line
            Rectangle {
                id: handleLine
                visible: mainMouseArea.isRotating
                width: 2
                height: 25
                color: "white"
                anchors.top: parent.bottom
                anchors.horizontalCenter: parent.horizontalCenter
            }

            // Rotation Handle Knob
            Rectangle {
                id: rotateKnob
                visible: mainMouseArea.isRotating
                width: 12
                height: 12
                radius: 6
                color: "white"
                border.color: "black"
                border.width: 1
                anchors.verticalCenter: handleLine.bottom
                anchors.horizontalCenter: handleLine.horizontalCenter
            }
        }
    }
    
    // Aspect ratio selector window (upper left corner)
    Rectangle {
        id: aspectRatioWindow
        visible: uiState && uiState.isCropping
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.margins: 10
        width: 120
        height: Math.max(150, aspectRatioColumn.implicitHeight + 20)
        color: "#333333"
        border.color: "#666666"
        border.width: 1
        radius: 4
        z: 1000
        
        // Try to get root from parent hierarchy
        property bool isDark: typeof root !== "undefined" && root ? root.isDarkTheme : true
        
        Component.onCompleted: {
            // Update colors based on theme
            color = isDark ? "#333333" : "#f0f0f0"
            border.color = isDark ? "#666666" : "#cccccc"
        }        Column {
            id: aspectRatioColumn
            anchors.fill: parent
            anchors.margins: 10
            spacing: 5
            
            Text {
                text: "Aspect Ratio"
                font.bold: true
                color: aspectRatioWindow.isDark ? "white" : "black"
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
                        color: aspectRatioWindow.isDark ? "white" : "black"
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
            
                Rectangle {
                    width: parent.width
                    height: 30
                    color: mainMouseArea.isRotating ? "#555555" : "transparent"
                    radius: 3
                    
                    Text {
                        anchors.left: parent.left
                        anchors.leftMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        text: "Rotate"
                        color: aspectRatioWindow.isDark ? "white" : "black"
                        font.pixelSize: 11
                        font.bold: mainMouseArea.isRotating
                    }
                    
                    MouseArea {
                        anchors.fill: parent
                        onClicked: {
                            mainMouseArea.isRotating = !mainMouseArea.isRotating
                            mainMouseArea.cropDragMode = "none"
                        }
                    }
                }
        }
    }


}
