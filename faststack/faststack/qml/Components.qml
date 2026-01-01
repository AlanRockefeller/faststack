import QtQuick
import QtQuick.Window

// This file is intended to hold QML components like the main image view.
// For simplicity, we'll start with just the main image view.

Item {
    id: loupeView
    anchors.fill: parent
    focus: true
    
    // Height of the status bar footer in Main.qml
    property int footerHeight: 60
    
    Connections {
        target: uiState
        function onCurrentIndexChanged() {
            // Smart High-Res Logic:
            // Before the new image loads, decide if we should keep high-res mode.
            // Rule: Only keep high-res if we are currently "meaningfully zoomed" (> 1.1x fit).
            // This prevents "sticky" high-res where zooming in once keeps it forever.
            
            if (imageRotator.zoomScale > imageRotator.fitScale * 1.1) {
                // Keep high-res (setZoomed true if not already)
                if (!uiState.isZoomed) uiState.setZoomed(true)
            } else {
                // Drop to low-res for the next image
                if (uiState.isZoomed) uiState.setZoomed(false)
            }
        }
    }
    
    Keys.onEscapePressed: (event) => {
        if (uiState && uiState.isCropping) {
            if (mainMouseArea.isRotating) {
                // Revert rotation
                mainMouseArea.cropRotation = mainMouseArea.cropStartRotation
                if (controller) controller.set_straighten_angle(mainMouseArea.cropRotation, -1)
                
                mainMouseArea.isRotating = false
                mainMouseArea.cropDragMode = "none"
                mainMouseArea.isCropDragging = false
                event.accepted = true
            } else if (controller) {
                controller.cancel_crop_mode()
                mainMouseArea.cropRotation = 0 // Reset local rotation
                event.accepted = true
            }
        }
    }




    Keys.onPressed: (event) => {
        // Zoom Shortcuts (Ctrl+1..4)
        if (event.modifiers & Qt.ControlModifier) {
             if (event.key === Qt.Key_1) {
                 uiState.request_absolute_zoom(1.0)
                 event.accepted = true
                 return
             } else if (event.key === Qt.Key_2) {
                 uiState.request_absolute_zoom(2.0)
                 event.accepted = true
                 return
             } else if (event.key === Qt.Key_3) {
                 uiState.request_absolute_zoom(3.0)
                 event.accepted = true
                 return
             } else if (event.key === Qt.Key_4) {
                 // 400% zoom
                 uiState.request_absolute_zoom(4.0)
                 event.accepted = true
                 return
             }
        }
        
        // Handle Enter for Crop Execution (formerly Keys.onEnterPressed)
        // We only accept the event if we actually act on it.
        if ((event.key === Qt.Key_Enter || event.key === Qt.Key_Return) && uiState && uiState.isCropping && controller) {
            // Force immediate rotation update before executing crop
            if (mainMouseArea.cropRotation !== 0) {
                controller.set_straighten_angle(mainMouseArea.cropRotation, -1)
            }

            uiState.setZoomed(false) // Force unzoom
            controller.execute_crop()
            event.accepted = true
            return
        }

        // IMPORTANT: Allow unhandled keys to propagate to Python eventFilter logic
        event.accepted = false
    }



    // Connection to handle zoom/pan reset signal from Python
    Connections {
        target: uiState
        function onResetZoomPanRequested() {
            imageRotator.zoomScale = imageRotator.fitScale
            panTransform.x = 0
            panTransform.y = 0
        }
        function onAbsoluteZoomRequested(scale) {
             if (uiState && uiState.debugMode) {
                 console.log("QML: Absolute zoom requested: " + scale)
             }
             
             imageRotator.zoomScale = scale
             
             // If we need to switch to high-res, flag this scale as the target 
             // for the incoming source change so recomputeFitScale doesn't clobber it.
             if (uiState && !uiState.isZoomed) {
                 imageRotator.targetAbsoluteZoom = scale
                 uiState.setZoomed(true)
             }
        }
    }

    // Container that handles Viewport Clipping and Sizing
    Item {
        id: imageViewport
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.bottomMargin: footerHeight
        clip: true

        // Container that handles Rotation (Straightening)
        // This item represents the "Canvas" that expands when rotated.
        Item {
            id: imageRotator
            anchors.centerIn: parent
            
            // Size matches the AABB of the rotated image
            // W' = W*|cos| + H*|sin|
            // Geometry is now updated atomically via updateRotatorGeometry()
            property real implicitWidth: 0
            property real implicitHeight: 0
            property bool isUpdatingGeometry: false
            
            // Fix A: Atomic Zoom Scale
            property real zoomScale: 1.0
            
            // Fix C: Persist requested absolute zoom across source changes
            property real targetAbsoluteZoom: -1.0
            
            onZoomScaleChanged: {
                mainImage.updateZoomState()
                if (cropOverlay.visible) cropOverlay.updateCropRect()
            }

            // Fix B: Stable Logical Size
            property real baseW: 0
            property real baseH: 0

            function updateRotatorGeometry() {
               if (!mainImage || mainImage.sourceSize.width <= 0) return
               
               isUpdatingGeometry = true
               
               var rad = mainMouseArea.cropRotation * (Math.PI / 180.0)
               
               // Use base size if available (stable during zoom), otherwise sourceSize
               var w = (baseW > 0) ? baseW : mainImage.sourceSize.width
               var h = (baseH > 0) ? baseH : mainImage.sourceSize.height
               
               var newW = Math.abs(w * Math.cos(rad)) + Math.abs(h * Math.sin(rad))
               var newH = Math.abs(w * Math.sin(rad)) + Math.abs(h * Math.cos(rad))
               
               width = newW
               height = newH
               
               // Atomically update mainImage size to prevent aspect ratio distortion
               mainImage.width = w
               mainImage.height = h
               
               isUpdatingGeometry = false
               recomputeFitScale()
            }

            Connections {
                target: mainMouseArea
                function onCropRotationChanged() { imageRotator.updateRotatorGeometry() }
            }
            // Trigger initial update (moved to end)

            // NEW: fit-to-window scale (minimum zoom)
            property real fitScale: 1.0

            function recomputeFitScale(force) {
                if (force === undefined) force = false;

                if (width <= 0 || height <= 0 || imageViewport.width <= 0 || imageViewport.height <= 0)
                    return;
                
                // Prevent jitter: Don't recompute fit scale while dragging (resize, move, or rotate)
                // Unless forced (e.g. on release)
                if (!force && mainMouseArea.isCropDragging) return;

                // Capture current relative zoom to preserve it during resize/reload
                var oldFit = fitScale
                var currentScale = imageRotator.zoomScale
                var ratio = 1.0
                if (oldFit > 0) {
                     ratio = currentScale / oldFit
                }

                // fit rotated canvas into viewport
                var s = Math.min(imageViewport.width / width, imageViewport.height / height);
                // Ensure fitScale is finite and positive
                // Allow upscaling to fit window (necessary for HiDPI logical sizing)
                if (!isFinite(s) || s <= 0) s = 1.0;
                // else if (s > 1.0) s = 1.0; // REMOVED: Cap prevented fitting small/logical images

                fitScale = s;

                // Restore zoom level
                if (targetAbsoluteZoom > 0) {
                     // Check if we have a pending absolute zoom request (e.g. from Ctrl+1)
                     // If so, use it directly (1.0 = 1:1 pixels) and consume the flag.
                     imageRotator.zoomScale = targetAbsoluteZoom;
                     targetAbsoluteZoom = -1.0;
                } else {
                     // Otherwise, preserve relative visual size (fit ratio)
                     imageRotator.zoomScale = fitScale * ratio;
                }
                // Preserve Pan (don't reset to 0) as pan is in screen pixels (mostly)
            }

            onWidthChanged: if (!isUpdatingGeometry) recomputeFitScale()
            onHeightChanged: if (!isUpdatingGeometry) recomputeFitScale()
            Component.onCompleted: {
                updateRotatorGeometry()
                recomputeFitScale()
            }
            
            Connections {
                target: imageViewport
                function onWidthChanged() { imageRotator.recomputeFitScale() }
                function onHeightChanged() { imageRotator.recomputeFitScale() }
            }

            transform: [
                Scale {
                    id: scaleTransform
                    origin.x: imageRotator.width / 2
                    origin.y: imageRotator.height / 2
                    xScale: imageRotator.zoomScale
                    yScale: imageRotator.zoomScale
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

            // The main image display
            Image {
                id: mainImage
                anchors.centerIn: parent
                
                // Image size is now updated atomically in updateRotatorGeometry to prevent distortion
                // width: sourceSize.width
                // height: sourceSize.height
                
                rotation: mainMouseArea.cropRotation
                
                // Crop overlay - moved back to mainImage for Visual Orbit (Rotate Together)
                // Coordinates are now Source Space, and backend handles conversion.
                Item {
                    id: cropOverlay
                    property var cropBox: uiState ? uiState.currentCropBox : [0, 0, 1000, 1000]
                    property bool hasActiveCrop: cropBox && cropBox.length === 4 && !(cropBox[0]===0 && cropBox[1]===0 && cropBox[2]===1000 && cropBox[3]===1000)
                    
                    visible: uiState && uiState.isCropping && (hasActiveCrop || mainMouseArea.isRotating)
                    anchors.fill: parent // Fills mainImage (Source Space)
                    z: 100
                    
                    onCropBoxChanged: { if (parent.source) updateCropRect() }
                    Component.onCompleted: { if (parent.source) updateCropRect() }
                    
                    Connections {
                        target: uiState
                        function onCurrentCropBoxChanged() { if (cropOverlay.visible && mainImage.source) cropOverlay.updateCropRect() }
                    }
                    
                    Connections {
                         target: mainImage
                         function onWidthChanged() { cropOverlay.updateCropRect() }
                         function onHeightChanged() { cropOverlay.updateCropRect() }
                    }
                    
                    function updateCropRect() {
                        if (!uiState || !uiState.currentCropBox || uiState.currentCropBox.length !== 4) return
                        var box = uiState.currentCropBox
                        
                        // Local coords in mainImage (Source Space)
                        var localLeft = (box[0] / 1000) * parent.width
                        var localTop = (box[1] / 1000) * parent.height
                        var localRight = (box[2] / 1000) * parent.width
                        var localBottom = (box[3] / 1000) * parent.height
                        
                        cropRect.x = localLeft
                        cropRect.y = localTop
                        cropRect.width = localRight - localLeft
                        cropRect.height = localBottom - localTop
                    }
                    
                    // Dimmer Rectangles
                    Rectangle { x: 0; y: 0; width: parent.width; height: cropRect.y; color: "black"; opacity: 0.3 }
                    Rectangle { x: 0; y: cropRect.y + cropRect.height; width: parent.width; height: parent.height - (cropRect.y + cropRect.height); color: "black"; opacity: 0.3 }
                    Rectangle { x: 0; y: cropRect.y; width: cropRect.x; height: cropRect.height; color: "black"; opacity: 0.3 }
                    Rectangle { x: cropRect.x + cropRect.width; y: cropRect.y; width: parent.width - (cropRect.x + cropRect.width); height: cropRect.height; color: "black"; opacity: 0.3 }
                    
                    Rectangle {
                        id: cropRect
                        color: "transparent"
                        border.color: "white"
                        border.width: 3 / ((scaleTransform && scaleTransform.xScale) ? scaleTransform.xScale : 1.0)
                        
                        // Rotation Handle Line
                        Rectangle {
                            id: handleLine
                            visible: mainMouseArea.isRotating
                            width: 2 / ((scaleTransform && scaleTransform.xScale) ? scaleTransform.xScale : 1.0)
                            height: 25 / ((scaleTransform && scaleTransform.xScale) ? scaleTransform.xScale : 1.0)
                            color: "white"
                            anchors.top: parent.bottom
                            anchors.horizontalCenter: parent.horizontalCenter
                        }
                        
                        // Rotation Knob
                        Rectangle {
                            id: rotateKnob
                            visible: mainMouseArea.isRotating
                            width: 12 / ((scaleTransform && scaleTransform.xScale) ? scaleTransform.xScale : 1.0)
                            height: 12 / ((scaleTransform && scaleTransform.xScale) ? scaleTransform.xScale : 1.0)
                            radius: width / 2
                            color: "white"
                            border.color: "black"
                            border.width: 1 / ((scaleTransform && scaleTransform.xScale) ? scaleTransform.xScale : 1.0)
                            anchors.verticalCenter: handleLine.bottom
                            anchors.horizontalCenter: handleLine.horizontalCenter
                        }
                    }
                }
                
                source: uiState && uiState.imageCount > 0 ? uiState.currentImageSource : ""
                
                function _currentDpr() {
                    // Per-window DPR is the safest (multi-monitor setups)
                    if (mainImage.window && mainImage.window.devicePixelRatio)
                        return mainImage.window.devicePixelRatio
                    return Screen.devicePixelRatio
                }

                function handleSourceSizeChange() {
                    if (mainImage.sourceSize.width <= 0 || mainImage.sourceSize.height <= 0) return

                    const dpr = _currentDpr()

                    // Treat baseW/baseH as *device-independent pixels* that correspond to 1:1 physical pixels at zoomScale=1
                    imageRotator.baseW = mainImage.sourceSize.width / dpr
                    imageRotator.baseH = mainImage.sourceSize.height / dpr

                    // Rebuild rotator + mainImage geometry based on the NEW resolution
                    imageRotator.updateRotatorGeometry()

                    // Force fit recompute so fitScale / zoom logic stabilizes immediately
                    imageRotator.recomputeFitScale(true)

                    if (uiState && uiState.debugMode) {
                        console.log("sourceSize changed:", mainImage.sourceSize.width, mainImage.sourceSize.height,
                                    "dpr:", dpr,
                                    "base:", imageRotator.baseW, imageRotator.baseH,
                                    "zoomScale:", imageRotator.zoomScale)
                    }
                }

                onSourceSizeChanged: { handleSourceSizeChange() }

                onStatusChanged: {
                   if (status === Image.Ready) {
                       // Some backends update sourceSize right as status flips
                       mainImage.handleSourceSizeChange()
                       imageRotator.updateRotatorGeometry()
                   }
                }

                // Force reset when source changes (existing logic)
                onSourceChanged: {
                    // Reset base size for new image so we pick up the new sourceSize
                    imageRotator.baseW = 0
                    imageRotator.baseH = 0
                    
                    // Smart Zoom Reset:
                    // If we intended to keep high-res (isZoomed is true), preserve capabilities.
                    // If not (isZoomed is false), reset to "fit" state for speed and consistency.
                    if (uiState && !uiState.isZoomed) {
                        mainMouseArea.cropRotation = 0
                        mainMouseArea.isRotating = false
                        mainMouseArea.cropDragMode = "none"
                        
                        imageRotator.zoomScale = imageRotator.fitScale
                        panTransform.x = 0
                        panTransform.y = 0
                    }
                }
                fillMode: Image.PreserveAspectFit
                cache: false // We do our own caching in Python
                smooth: false // Crisp rendering for technical accuracy
                mipmap: false // Crisp rendering
                
                property bool isZooming: false
        
                // IMPORTANT: tell Python the *viewport* size, not the sourceSize size
                function reportDisplaySize() {
                    if (imageViewport.width > 0 && imageViewport.height > 0) {
                        var dpr = Screen.devicePixelRatio
                        uiState.onDisplaySizeChanged(
                            Math.round(imageViewport.width * dpr),
                            Math.round(imageViewport.height * dpr)
                        )
                    }
                }

                Component.onCompleted: reportDisplaySize()
                Connections {
                    target: imageViewport
                    function onWidthChanged() { mainImage.reportDisplaySize() }
                    function onHeightChanged() { mainImage.reportDisplaySize() }
                }
        
                // Removed direct onWidth/HeightChanged handlers for resizeDebounceTimer 
                // because we now drive size reporting via viewport changes.

                Timer {
                    id: lowResDebounceTimer
                    interval: 200 // 200ms debounce to prevent thrashing
                    repeat: false
                    onTriggered: {
                        if (uiState && uiState.isZoomed) {
                            uiState.setZoomed(false)
                        }
                    }
                }

                function updateZoomState() {
                    if (!uiState) return;
                    
                    // Thresholds for hysteresis
                    var highResThreshold = imageRotator.fitScale * 1.1
                    var lowResThreshold = imageRotator.fitScale * 1.02
                    
                    // Enable High-Res if zoomed in significantly
                    if (imageRotator.zoomScale > highResThreshold) {
                         lowResDebounceTimer.stop()
                         if (!uiState.isZoomed) {
                             uiState.setZoomed(true);
                         }
                    } 
                    // Disable High-Res (return to low-res) if zoomed out to near-fit
                    // formatting note: added hysteresis check AND debounce
                    else if (imageRotator.zoomScale <= lowResThreshold) {
                        if (uiState.isZoomed) {
                            // Only drop to low-res after delay to handle wheel overshoot/jitter
                            if (!lowResDebounceTimer.running) lowResDebounceTimer.start()
                        }
                    } else {
                        // In hysteresis band: cancel any pending low-res switch
                        lowResDebounceTimer.stop()
                    }
                    
                    updateHistogramWithZoom()
                }
                
                function updateHistogramWithZoom() {
                    if (uiState && uiState.isHistogramVisible && controller) {
                        var zoom = imageRotator.zoomScale
                        var panX = panTransform.x
                        var panY = panTransform.y
                        var imageScale = imageRotator.zoomScale
                        controller.update_histogram(zoom, panX, panY, imageScale)
                    }
                }


            }


        }
    }

    // Zoom and Pan logic would go here
    // For example, using PinchArea or MouseArea


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
        property real cropStartAspect: -1
        
        // Reset rotation when image changes or updates (e.g. after crop save) to avoid persistence
        Connections {
            target: uiState
            function onCurrentIndexChanged() {
                mainMouseArea.cropRotation = 0
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
        
        property real pendingRotation: 0
        property real pendingAspect: -1
        
        Timer {
            id: rotationThrottleTimer
            interval: 32 // ~30 fps
            repeat: false
            onTriggered: {
                if (controller && uiState && uiState.isCropping) {
                    controller.set_straighten_angle(mainMouseArea.pendingRotation, mainMouseArea.pendingAspect)
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
                // Check if clicking on existing crop box - Using Image Space Hit Testing
                var box = uiState.currentCropBox
                var isFullImage = box && box.length === 4 && box[0] === 0 && box[1] === 0 && box[2] === 1000 && box[3] === 1000
                
                var coords = mapToImageCoordinates(Qt.point(mouse.x, mouse.y))
                var mx = coords.x * 1000
                var my = coords.y * 1000
                
                // Calculate threshold in normalized units (approx 10 screen pixels)
                var threshX = (10 / (scaleTransform.xScale * mainImage.width)) * 1000
                var threshY = (10 / (scaleTransform.yScale * mainImage.height)) * 1000
                
                // Clamp threshold: min 5 normalized units (prevent too small), max 40 (prevent too large)
                // This ensures handles remain usable at all zoom levels
                var edgeThreshold = Math.max(5, Math.min(40, Math.max(threshX, threshY)))

                var inside = mx >= box[0] && mx <= box[2] && my >= box[1] && my <= box[3]
                
                // --- Hit test for rotation handle (robust: uses actual knob transform) ---
                if (mainMouseArea.isRotating && cropOverlay.visible && rotateKnob.visible) {
                    // knob center in mainMouseArea coords (includes cropRect rotation)
                    // Note: rotateKnob is now inside mainImage -> cropOverlay -> cropRect
                    // But mapFromItem should still work if we target the object properly.
                    // We need to resolve `rotateKnob` which is inside cropOverlay.
                    // If cropOverlay moves, we need to ensure this binding works.
                    // IMPORTANT: cropOverlay is not moved yet in this call.
                    // Current logic relies on existing structure. I will defer logic update if structure changes.
                    // But hit testing via mapFromItem(rotateKnob) is robust to hierarchy changes as long as rotateKnob exists.
                    
                    var k = mainMouseArea.mapFromItem(rotateKnob, rotateKnob.width/2, rotateKnob.height/2)
                    var dxk = mouse.x - k.x
                    var dyk = mouse.y - k.y
                    var distk = Math.sqrt(dxk*dxk + dyk*dyk)

                    if (distk < 22 * Screen.devicePixelRatio) { // a little forgiving
                        cropDragMode = "rotate"

                        // crop center in mainMouseArea coords -> Changed to IMAGE center to avoid feedback loop
                        var c = mainMouseArea.mapFromItem(mainImage, mainImage.width/2, mainImage.height/2)
                        cropStartAngle = Math.atan2(mouse.y - c.y, mouse.x - c.x) * 180 / Math.PI
                        cropStartRotation = cropRotation
                        
                        // Calculate start aspect ratio (in pixels)
                        if (mainImage.width > 0) {
                            var box = uiState.currentCropBox
                            if (box && box.length === 4) {
                                var boxW = (box[2] - box[0]) / 1000 * mainImage.width
                                var boxH = (box[3] - box[1]) / 1000 * mainImage.height
                                cropStartAspect = boxW / boxH
                            }
                        }


                        // Seed cropBoxStart variables
                        if (box && box.length === 4) {
                            cropBoxStartLeft = box[0]
                            cropBoxStartTop = box[1]
                            cropBoxStartRight = box[2]
                            cropBoxStartBottom = box[3]
                        }

                        isCropDragging = true
                        return
                    }
                }
                
                // If crop box is full image, always start a new crop
                else if (isFullImage) {
                    cropDragMode = "new"
                    cropStartX = mouse.x
                    cropStartY = mouse.y
                } else if (inside) {
                    // Determine which edge/corner is being dragged (Image Space)
                    var nearLeft = Math.abs(mx - box[0]) < edgeThreshold
                    var nearRight = Math.abs(mx - box[2]) < edgeThreshold
                    var nearTop = Math.abs(my - box[1]) < edgeThreshold
                    var nearBottom = Math.abs(my - box[3]) < edgeThreshold
                    
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
                    cropBoxStartLeft = box[0]
                    cropBoxStartTop = box[1]
                    cropBoxStartRight = box[2]
                    cropBoxStartBottom = box[3]
                } else {
                    // Start new crop rectangle
                    cropDragMode = "new"
                    cropStartX = mouse.x
                    cropStartY = mouse.y
                    
                    // Initialize anchors
                    cropBoxStartLeft = mx
                    cropBoxStartRight = mx
                    cropBoxStartTop = my
                    cropBoxStartBottom = my
                }
                isCropDragging = true
            }
        }        
        // Legacy getCropRect removed - using Image Space hit testing instead.
        // mapToImageCoordinates now maps directly to mainImage
        function mapToImageCoordinates(screenPoint) {
            var p = mainMouseArea.mapToItem(mainImage, screenPoint.x, screenPoint.y)
            return {x: p.x / mainImage.width, y: p.y / mainImage.height}
        }
        onPositionChanged: function(mouse) {
            if (uiState && uiState.isCropping && isCropDragging) {
                if (cropDragMode === "new") {
                    // Update crop rectangle while dragging
                    updateCropBox(cropStartX, cropStartY, mouse.x, mouse.y, true)
                } else if (cropDragMode === "rotate") {
                    var c = mainMouseArea.mapFromItem(mainImage, mainImage.width/2, mainImage.height/2)
                    var currentAngle = Math.atan2(mouse.y - c.y, mouse.x - c.x) * 180 / Math.PI
                    var delta = currentAngle - cropStartAngle
                    // Handle wrap-around
                    if (delta > 180) delta -= 360
                    if (delta < -180) delta += 360
                    
                    var newRotation = cropStartRotation + delta

                    // Update rotation state
                    cropRotation = newRotation
                    
                    // Update rotation in backend live (throttled)
                    if (controller) {
                        pendingRotation = cropRotation
                        pendingAspect = cropStartAspect
                        
                        if (!rotationThrottleTimer.running) {
                            rotationThrottleTimer.start()
                        }
                    }

                    
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
                        if (controller) controller.start_drag_current_image()
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
                // Fix: Prevent accidental tiny crops with Right Click
                if (mouse.button === Qt.RightButton && cropDragMode === "new") {
                    var dx = Math.abs(mouse.x - cropStartX)
                    var dy = Math.abs(mouse.y - cropStartY)
                    var maxDim = Math.max(dx, dy)
                    var minDim = Math.min(dx, dy)
                    
                    // "at least 50 pixels in both dimensions"
                    if (maxDim < 50 || minDim < 50) {
                        if (controller) controller.cancel_crop_mode()
                        isCropDragging = false
                        cropDragMode = "none"
                        return
                    }
                }

                isCropDragging = false
                cropDragMode = "none"
                // Settle zoom/pan after rotation ends (Force recompute)
                if (mainMouseArea.isRotating) imageRotator.recomputeFitScale(true)
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
            var oldScale = imageRotator.zoomScale
            var newScale = oldScale * scaleFactor
            // Allow zooming out past "Fit" to 5%. Cap max at 20x.
            newScale = Math.max(0.05, Math.min(20.0, newScale))

            // Current state
            var currentPanX = panTransform.x
            var currentPanY = panTransform.y
            
            // Screen center (Viewport center)
            var centerX = imageViewport.width / 2
            var centerY = imageViewport.height / 2

            // Fix C: Use Viewport Coordinates (account for footer offset etc)
            var p = mainMouseArea.mapToItem(imageViewport, wheel.x, wheel.y)
            var mouseX = p.x
            var mouseY = p.y
            
            var mouseOffsetFromCenterX = mouseX - centerX
            var mouseOffsetFromCenterY = mouseY - centerY

            // Calculate the "image point" currently under the cursor (relative to image center, unscaled)
            // ScreenPos = Center + Pan + (ImagePoint * Scale)
            // ImagePoint = (ScreenPos - Center - Pan) / Scale
            // ImagePoint = (MouseOffsetFromCenter - Pan) / Scale
            var imagePointX = (mouseOffsetFromCenterX - currentPanX) / oldScale
            var imagePointY = (mouseOffsetFromCenterY - currentPanY) / oldScale

            // We want to keep this ImagePoint under the cursor after scaling:
            // MouseOffsetFromCenter = Pan_New + (ImagePoint * Scale_New)
            // Pan_New = MouseOffsetFromCenter - (ImagePoint * Scale_New)
            
            var newPanX = mouseOffsetFromCenterX - (imagePointX * newScale)
            var newPanY = mouseOffsetFromCenterY - (imagePointY * newScale)

            // Apply updates
            imageRotator.zoomScale = newScale
            panTransform.x = newPanX
            panTransform.y = newPanY

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
            if (!ratioPair || !mainImage || !imageRotator.width || !imageRotator.height) {
                return [left, top, right, bottom];
            }

            // Calculate effective aspect ratio in 0-1000 normalized space
            // targetAspect (pixels) = width_px / height_px
            // width_px = width_norm * imgW / 1000
            // height_px = height_norm * imgH / 1000
            // targetAspect = (width_norm * imgW) / (height_norm * imgH)
            // width_norm / height_norm = targetAspect * (imgH / imgW)
            
            var pixelAspect = ratioPair[0] / ratioPair[1];
            // Use mainImage (fixed canvas) for aspect ratio calculation
            var imageAspect = mainImage.width / mainImage.height;
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
    
    // Crop rectangle overlay (Moved to mainImage)
    
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
