pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Window

// This file is intended to hold QML components like the main image view.
// For simplicity, we'll start with just the main image view.

Item {
    id: loupeView
    anchors.fill: parent
    focus: true
    
    // Height of the status bar footer in Main.qml
    property var uiStateRef: null
    property var controllerRef: null
    property bool isDarkTheme: true
    property int footerHeight: 60

    // Expose zoom state to parent (Main.qml title bar)
    readonly property real currentZoomScale: imageRotator.zoomScale
    readonly property real currentFitScale: imageRotator.fitScale
    readonly property real currentPixelZoomScale: {
        if (!loupeView.uiStateRef || !mainImage || mainImage.sourceSize.width <= 0 || mainImage.sourceSize.height <= 0) return currentZoomScale

        var nativeW = loupeView.uiStateRef.currentNativeImageWidth
        var nativeH = loupeView.uiStateRef.currentNativeImageHeight
        if (nativeW <= 0 || nativeH <= 0) return currentZoomScale

        var scaleW = imageRotator.zoomScale * mainImage.sourceSize.width / nativeW
        var scaleH = imageRotator.zoomScale * mainImage.sourceSize.height / nativeH
        if (scaleW > 0 && scaleH > 0) return Math.min(scaleW, scaleH)
        return currentZoomScale
    }
    // Freeze the displayed source for the full crop session once crop mode
    // starts. Zoom-triggered high-res swaps stay blocked until crop mode exits,
    // because any async source swap during cropping can rescale the image and
    // invalidate the crop box's visual alignment.
    readonly property string requestedImageSource: loupeView.uiStateRef && loupeView.uiStateRef.imageCount > 0 ? loupeView.uiStateRef.currentImageSource : ""
    property string cropDragImageSource: ""
    readonly property bool isCropSourceFrozen: cropDragImageSource !== "" && ((mainMouseArea && mainMouseArea.isCropDragging) || (loupeView.uiStateRef && loupeView.uiStateRef.isCropping))
    readonly property string displayedImageSource: isCropSourceFrozen ? cropDragImageSource : requestedImageSource

    Component.onCompleted: {
        loupeView.uiStateRef = uiState
        loupeView.controllerRef = controller
        // mainImage may complete before uiStateRef is wired, so retry the
        // initial size report once from the parent if the child call no-op'd.
        if (mainImage && !mainImage.initialDisplaySizeReported) {
            mainImage.reportDisplaySize()
        }
    }

    function freezeCropImageSource() {
        if (cropDragImageSource === "") {
            cropDragImageSource = mainImage && mainImage.source ? mainImage.source : requestedImageSource
        }
    }

    function releaseCropImageSource() {
        cropDragImageSource = ""
    }

    function cancelActiveCropRotation() {
        if (!loupeView.uiStateRef || !loupeView.uiStateRef.isCropping || !mainMouseArea.isRotating) return false

        mainMouseArea.cropRotation = mainMouseArea.cropStartRotation
        mainMouseArea.clearPendingRotation(mainMouseArea.cropRotation)
        if (loupeView.controllerRef) loupeView.controllerRef.set_straighten_angle(mainMouseArea.cropRotation, -1)

        mainMouseArea.endCropInteraction()
        mainMouseArea.isRotating = false
        return true
    }

    function cancelCropMode() {
        if (!loupeView.uiStateRef || !loupeView.uiStateRef.isCropping || !loupeView.controllerRef) return false

        mainMouseArea.clearPendingRotation(0)
        mainMouseArea.endCropInteraction()
        mainMouseArea.resetCropRotation()
        mainMouseArea.isRotating = false
        loupeView.controllerRef.cancel_crop_mode()
        return true
    }

    Connections {
        target: loupeView.uiStateRef
        function onCurrentIndexChanged() {
            if (mainMouseArea && (
                mainMouseArea.cropReleasePending
                || mainMouseArea.isCropDragging
                || mainMouseArea.cropDragMode !== "none"
                || mainMouseArea.isRotating
            )) {
                mainMouseArea.endCropInteraction()
                mainMouseArea.isRotating = false
            }

            // Smart High-Res Logic:
            // Before the new image loads, decide if we should keep high-res mode.
            // Rule: Only keep high-res if we are currently "meaningfully zoomed" (> 1.1x fit).
            // This prevents "sticky" high-res where zooming in once keeps it forever.
            
            if (imageRotator.zoomScale > imageRotator.fitScale * 1.1) {
                // Keep high-res (setZoomed true if not already)
                if (!loupeView.uiStateRef.isZoomed) loupeView.uiStateRef.setZoomed(true)
            } else {
                // Drop to low-res for the next image
                if (loupeView.uiStateRef.isZoomed) loupeView.uiStateRef.setZoomed(false)
            }
        }
        function onIsCroppingChanged() {
            if (loupeView.uiStateRef && loupeView.uiStateRef.isCropping) {
                // Capture the session's visual source when crop mode turns on,
                // then keep it stable until crop mode exits.
                loupeView.freezeCropImageSource()
            } else {
                if (mainMouseArea) {
                    mainMouseArea.clearPendingRotation(0)
                    mainMouseArea.endCropInteraction()
                    mainMouseArea.isRotating = false
                    mainMouseArea.resetCropRotation()
                }
                if (loupeView.uiStateRef) loupeView.uiStateRef.isCropRotating = false
                loupeView.releaseCropImageSource()
            }
        }
    }

    Keys.onEscapePressed: (event) => {
        if (loupeView.uiStateRef && loupeView.uiStateRef.isCropping) {
            if (mainMouseArea.isRotating) {
                loupeView.cancelActiveCropRotation()
                event.accepted = true
            } else if (loupeView.cancelCropMode()) {
                event.accepted = true
            }
        }
    }

    Keys.onPressed: (event) => {
        // Zoom Shortcuts (Ctrl+1..4)
        if (event.modifiers & Qt.ControlModifier) {
             if (event.key === Qt.Key_1) {
                 loupeView.uiStateRef.request_absolute_zoom(1.0)
                 event.accepted = true
                 return
             } else if (event.key === Qt.Key_2) {
                 loupeView.uiStateRef.request_absolute_zoom(2.0)
                 event.accepted = true
                 return
             } else if (event.key === Qt.Key_3) {
                 loupeView.uiStateRef.request_absolute_zoom(3.0)
                 event.accepted = true
                 return
             } else if (event.key === Qt.Key_4) {
                 // 400% zoom
                 loupeView.uiStateRef.request_absolute_zoom(4.0)
                 event.accepted = true
                 return
             }
        }
        
        // Handle Enter for Crop Execution (formerly Keys.onEnterPressed)
        // We only accept the event if we actually act on it.
        if ((event.key === Qt.Key_Enter || event.key === Qt.Key_Return) && loupeView.uiStateRef && loupeView.uiStateRef.isCropping && loupeView.controllerRef) {
            // Force immediate rotation update before executing crop
            if (mainMouseArea.cropRotation !== 0) {
                loupeView.controllerRef.set_straighten_angle(mainMouseArea.cropRotation, -1)
            }

            loupeView.uiStateRef.setZoomed(false) // Force unzoom
            loupeView.controllerRef.execute_crop()
            event.accepted = true
            return
        }

        // IMPORTANT: Allow unhandled keys to propagate to Python eventFilter logic
        event.accepted = false
    }



    // Connection to handle zoom/pan reset signal from Python
    Connections {
        target: loupeView.uiStateRef
        function onResetZoomPanRequested() {
            imageRotator.zoomScale = imageRotator.fitScale
            panTransform.x = 0
            panTransform.y = 0
        }
        function onAbsoluteZoomRequested(scale) {
             imageRotator.zoomScale = scale
             
             // If we need to switch to high-res, flag this scale as the target 
             // for the incoming source change so recomputeFitScale doesn't clobber it.
             if (loupeView.uiStateRef && !loupeView.uiStateRef.isZoomed) {
                 imageRotator.targetAbsoluteZoom = scale
                 loupeView.uiStateRef.setZoomed(true)
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
        anchors.bottomMargin: loupeView.footerHeight
        clip: true

        // Container that handles Rotation (Straightening)
        // This item represents the "Canvas" that expands when rotated.
        Item {
            id: imageRotator
            anchors.centerIn: parent
            
            // Size matches the AABB of the rotated image
            // W' = W*|cos| + H*|sin|
            // Geometry is now updated atomically via updateRotatorGeometry()
            implicitWidth: 0
            implicitHeight: 0
            property bool isUpdatingGeometry: false
            
            // Fix A: Atomic Zoom Scale
            property real zoomScale: 1.0
            
            // Fix C: Persist requested absolute zoom across source changes
            property real targetAbsoluteZoom: -1.0
            
            // Zoom/pan lock: when >= 0, any change to zoomScale is reverted.
            // Active only during crop drag to keep the coordinate system stable.
            property real _lockedZoom: -1
            property real _lockedPanX: -1e9
            property real _lockedPanY: -1e9
            
            onZoomScaleChanged: {
                // During crop drag, revert any zoom changes to keep coordinates stable
                if (_lockedZoom >= 0 && Math.abs(zoomScale - _lockedZoom) > 0.0001) {
                    zoomScale = _lockedZoom
                    return
                }
                mainImage.updateZoomState()
            }

            // Fix B: Stable Logical Size
            property real baseW: 0
            property real baseH: 0

            function updateRotatorGeometry() {
               if (!mainImage || mainImage.sourceSize.width <= 0) return
               
               isUpdatingGeometry = true
               
               var rad = mainMouseArea.cropRotation * (Math.PI / 180.0)
               var cropModeActive = loupeView.uiStateRef && loupeView.uiStateRef.isCropping
               
               // Use base size if available (stable during zoom), otherwise sourceSize
               var w = (baseW > 0) ? baseW : mainImage.sourceSize.width
               var h = (baseH > 0) ? baseH : mainImage.sourceSize.height
               
               var newW = cropModeActive ? w : Math.abs(w * Math.cos(rad)) + Math.abs(h * Math.sin(rad))
               var newH = cropModeActive ? h : Math.abs(w * Math.sin(rad)) + Math.abs(h * Math.cos(rad))
               
               width = newW
               height = newH
               
               // Atomically update mainImage size to prevent aspect ratio distortion
               mainImage.width = w
               mainImage.height = h
               
               isUpdatingGeometry = false
               if (!cropModeActive) recomputeFitScale()
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
                        if (imageRotator._lockedPanX > -1e8 && Math.abs(x - imageRotator._lockedPanX) > 0.01) {
                            x = imageRotator._lockedPanX
                            return
                        }
                        mainImage.updateHistogramWithZoom()
                    }
                    onYChanged: {
                        if (imageRotator._lockedPanY > -1e8 && Math.abs(y - imageRotator._lockedPanY) > 0.01) {
                            y = imageRotator._lockedPanY
                            return
                        }
                        mainImage.updateHistogramWithZoom()
                    }
                }
            ]

            // The main image display
            Image {
                id: mainImage
                anchors.centerIn: parent
                visible: loupeView.uiStateRef && !loupeView.uiStateRef.isGridViewActive
                
                // Image size is now updated atomically in updateRotatorGeometry to prevent distortion
                // width: sourceSize.width
                // height: sourceSize.height
                
                function cropRotationOriginX() {
                    if (loupeView.uiStateRef && loupeView.uiStateRef.isCropping) return mainMouseArea.cropRotationPivotX * mainImage.width
                    return mainImage.width / 2
                }

                function cropRotationOriginY() {
                    if (loupeView.uiStateRef && loupeView.uiStateRef.isCropping) return mainMouseArea.cropRotationPivotY * mainImage.height
                    return mainImage.height / 2
                }

                transform: Rotation {
                    id: imageStraightenRotation
                    origin.x: mainImage.cropRotationOriginX()
                    origin.y: mainImage.cropRotationOriginY()
                    angle: mainMouseArea.cropRotation
                }
                
                // Darken mask overlay - anchored to mainImage, rotates/scales with it
                Image {
                    id: darkenOverlay
                    anchors.fill: parent
                    z: 90
                    visible: loupeView.uiStateRef && loupeView.uiStateRef.isDarkening && loupeView.uiStateRef.darkenOverlayVisible && !loupeView.uiStateRef.originalCompareActive
                    source: (loupeView.uiStateRef && loupeView.uiStateRef.isDarkening && loupeView.uiStateRef.darkenOverlayVisible && !loupeView.uiStateRef.originalCompareActive)
                            ? "image://provider/mask_overlay/" + loupeView.uiStateRef.darkenOverlayGeneration
                            : ""
                    fillMode: Image.Stretch
                    cache: false
                    opacity: 1.0  // Opacity is baked into the ARGB32 image
                }

                source: loupeView.displayedImageSource
                
                function _currentDpr() {
                    // Fall back to the current screen DPR; qmllint does not
                    // recognize a stable per-window DPR property here.
                    return Screen.devicePixelRatio
                }

                function handleSourceSizeChange() {
                    if (mainImage.sourceSize.width <= 0 || mainImage.sourceSize.height <= 0) return

                    if (mainMouseArea.isCropDragging) {
                        // Mark stale so onReleased will pick up the new geometry
                        _sourceSizeStale = true
                        return
                    }

                    const dpr = _currentDpr()

                    // Treat baseW/baseH as *device-independent pixels* that correspond to 1:1 physical pixels at zoomScale=1
                    imageRotator.baseW = mainImage.sourceSize.width / dpr
                    imageRotator.baseH = mainImage.sourceSize.height / dpr

                    // Rebuild rotator + mainImage geometry based on the NEW resolution
                    imageRotator.updateRotatorGeometry()

                    // Force fit recompute so fitScale / zoom logic stabilizes immediately
                    imageRotator.recomputeFitScale(true)
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
                    if (mainMouseArea.isCropDragging) {
                        // Source changed mid-drag (e.g. high-res edit buffer loading).
                        // Defer ALL visual/geometry resets so the coordinate system
                        // stays stable and the image doesn't flash black.
                        mainImage._sourceSizeStale = true
                        return
                    }

                    imageRotator.baseW = 0
                    imageRotator.baseH = 0
                    
                    // Smart Zoom Reset:
                    // If we intended to keep high-res (isZoomed is true), preserve capabilities.
                    // If not (isZoomed is false), reset to "fit" state for speed and consistency.
                    if (loupeView.uiStateRef && !loupeView.uiStateRef.isZoomed) {
                        mainMouseArea.resetCropRotation()
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
                
                property bool _sourceSizeStale: false
                property bool isZooming: false
                property bool initialDisplaySizeReported: false
        
                // IMPORTANT: tell Python the *viewport* size, not the sourceSize size
                function reportDisplaySize() {
                    if (loupeView.uiStateRef && imageViewport.width > 0 && imageViewport.height > 0) {
                        var dpr = Screen.devicePixelRatio
                        loupeView.uiStateRef.onDisplaySizeChanged(
                            Math.round(imageViewport.width * dpr),
                            Math.round(imageViewport.height * dpr)
                        )
                        initialDisplaySizeReported = true
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
                        if (loupeView.uiStateRef && loupeView.uiStateRef.isZoomed) {
                            loupeView.uiStateRef.setZoomed(false)
                        }
                    }
                }

                function updateZoomState() {
                    if (!loupeView.uiStateRef) return;
                    
                    // Thresholds for hysteresis
                    var highResThreshold = imageRotator.fitScale * 1.1
                    var lowResThreshold = imageRotator.fitScale * 1.02
                    
                    // Enable High-Res if zoomed in significantly
                    if (imageRotator.zoomScale > highResThreshold) {
                         lowResDebounceTimer.stop()
                         if (!loupeView.uiStateRef.isZoomed) {
                             loupeView.uiStateRef.setZoomed(true);
                         }
                    } 
                    // Disable High-Res (return to low-res) if zoomed out to near-fit
                    // formatting note: added hysteresis check AND debounce
                    else if (imageRotator.zoomScale <= lowResThreshold) {
                        if (loupeView.uiStateRef.isZoomed) {
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
                    if (loupeView.uiStateRef && loupeView.uiStateRef.isHistogramVisible && loupeView.controllerRef) {
                        var zoom = imageRotator.zoomScale
                        var panX = panTransform.x
                        var panY = panTransform.y
                        var imageScale = imageRotator.zoomScale
                        loupeView.controllerRef.update_histogram(zoom, panX, panY, imageScale)
                    }
                }


            }




        }

        // Crop overlay lives in viewport space so straighten rotates the image
        // inside a fixed output window, matching Photoshop's crop behavior.
        Item {
            id: cropOverlay
            anchors.fill: parent
            z: 100
            visible: loupeView.uiStateRef && loupeView.uiStateRef.isCropping

            property int _cropBoxRev: 0
            property bool isFullImageCrop: {
                var b = _liveCropBox()
                return b && b.length === 4 && b[0] === 0 && b[1] === 0 && b[2] === 1000 && b[3] === 1000
            }
            property bool hasPositiveCrop: {
                var b = _liveCropBox()
                return b && b.length === 4 && (b[2] - b[0]) > 0 && (b[3] - b[1]) > 0
            }
            property bool hasDrawableCrop: hasPositiveCrop && !isFullImageCrop
            property bool showCropContent: hasDrawableCrop || mainMouseArea.isRotating

            Connections {
                target: loupeView.uiStateRef
                function onCurrentCropBoxChanged() {
                    cropOverlay._cropBoxRev += 1
                }
            }

            function _liveCropBox() {
                var _ = cropOverlay._cropBoxRev
                return loupeView.uiStateRef ? loupeView.uiStateRef.currentCropBox : null
            }

            function _dimensionsAreSwapped() {
                // Symmetric about zero so +/-45 behave identically; mirrors the
                // backend's round(angle / 90) % 2 convention (editor.py).
                return Math.round(Math.abs(mainMouseArea.cropRotation) / 90) % 2 === 1
            }

            function cropViewRectForBox(box) {
                // Touch every input that affects the mainImage -> cropOverlay mapping
                // so this binding re-evaluates when any changes (QML can't track
                // dependencies through mapFromItem). The straighten Rotation pivot
                // (cropRotationPivotX/Y) is included so a pivot-only change still
                // invalidates the crop frame.
                var _ = cropOverlay._cropBoxRev + mainMouseArea.cropRotation + mainMouseArea.cropRotationPivotX + mainMouseArea.cropRotationPivotY + imageRotator.zoomScale + panTransform.x + panTransform.y + mainImage.width + mainImage.height + cropOverlay.width + cropOverlay.height
                if (!box || box.length !== 4 || !mainImage || mainImage.width <= 0 || mainImage.height <= 0) {
                    return {x: 0, y: 0, width: 0, height: 0}
                }

                var centerX = ((box[0] + box[2]) / 2000) * mainImage.width
                var centerY = ((box[1] + box[3]) / 2000) * mainImage.height
                var center = cropOverlay.mapFromItem(mainImage, centerX, centerY)
                var rectW = ((box[2] - box[0]) / 1000) * mainImage.width * imageRotator.zoomScale
                var rectH = ((box[3] - box[1]) / 1000) * mainImage.height * imageRotator.zoomScale

                if (_dimensionsAreSwapped()) {
                    var tmp = rectW
                    rectW = rectH
                    rectH = tmp
                }

                return {
                    x: center.x - rectW / 2,
                    y: center.y - rectH / 2,
                    width: rectW,
                    height: rectH
                }
            }

            function currentCropViewRect() {
                return cropViewRectForBox(_liveCropBox())
            }

            // Convert a viewport-space rect to a normalized 0-1000 crop box.
            //
            // preserveSize=true keeps the box's size and slides it to stay inside
            // the image (used for "move", which should translate without resizing).
            // preserveSize=false clips each edge independently at the image boundary
            // so the dragged edge stops at the edge while the anchored edge stays put
            // (used for resize and new-crop drawing).
            function cropBoxFromViewRect(left, top, right, bottom, preserveSize) {
                if (!mainImage || mainImage.width <= 0 || mainImage.height <= 0 || imageRotator.zoomScale <= 0) {
                    return [0, 0, 1000, 1000]
                }

                var rectLeft = Math.min(left, right)
                var rectRight = Math.max(left, right)
                var rectTop = Math.min(top, bottom)
                var rectBottom = Math.max(top, bottom)

                if (rectRight - rectLeft < 1) rectRight = rectLeft + 1
                if (rectBottom - rectTop < 1) rectBottom = rectTop + 1

                var center = mainImage.mapFromItem(cropOverlay, (rectLeft + rectRight) / 2, (rectTop + rectBottom) / 2)
                var sourceW = (rectRight - rectLeft) / imageRotator.zoomScale
                var sourceH = (rectBottom - rectTop) / imageRotator.zoomScale

                if (_dimensionsAreSwapped()) {
                    var tmp = sourceW
                    sourceW = sourceH
                    sourceH = tmp
                }

                var normW = sourceW * 1000 / mainImage.width
                var normH = sourceH * 1000 / mainImage.height
                var cx = center.x * 1000 / mainImage.width
                var cy = center.y * 1000 / mainImage.height

                var outLeft = cx - normW / 2
                var outRight = cx + normW / 2
                var outTop = cy - normH / 2
                var outBottom = cy + normH / 2

                if (preserveSize) {
                    // Slide the whole box back inside [0,1000] without resizing it.
                    if (outRight - outLeft >= 1000) {
                        outLeft = 0
                        outRight = 1000
                    } else {
                        if (outLeft < 0) {
                            outRight -= outLeft
                            outLeft = 0
                        }
                        if (outRight > 1000) {
                            outLeft -= outRight - 1000
                            outRight = 1000
                        }
                    }
                    if (outBottom - outTop >= 1000) {
                        outTop = 0
                        outBottom = 1000
                    } else {
                        if (outTop < 0) {
                            outBottom -= outTop
                            outTop = 0
                        }
                        if (outBottom > 1000) {
                            outTop -= outBottom - 1000
                            outBottom = 1000
                        }
                    }
                } else {
                    // Clip each edge at the image boundary so the dragged edge stops
                    // there while the anchored (opposite) edge stays fixed.
                    outLeft = Math.max(0, Math.min(1000, outLeft))
                    outRight = Math.max(0, Math.min(1000, outRight))
                    outTop = Math.max(0, Math.min(1000, outTop))
                    outBottom = Math.max(0, Math.min(1000, outBottom))
                }

                // Enforce a minimum crop extent in normalized units (~1% of the
                // image), independent of zoom so a high-zoom drag can't create a
                // degenerate crop.
                var minNorm = 10
                if (outRight - outLeft < minNorm) {
                    if (outLeft + minNorm <= 1000) outRight = outLeft + minNorm
                    else outLeft = outRight - minNorm
                }
                if (outBottom - outTop < minNorm) {
                    if (outTop + minNorm <= 1000) outBottom = outTop + minNorm
                    else outTop = outBottom - minNorm
                }

                outLeft = Math.max(0, Math.min(1000 - minNorm, outLeft))
                outTop = Math.max(0, Math.min(1000 - minNorm, outTop))
                outRight = Math.max(outLeft + minNorm, Math.min(1000, outRight))
                outBottom = Math.max(outTop + minNorm, Math.min(1000, outBottom))

                return [Math.round(outLeft), Math.round(outTop), Math.round(outRight), Math.round(outBottom)]
            }

            // Dimmer rectangles. The crop frame can extend past the viewport (for
            // example while straightened), so clamp every edge to the overlay
            // bounds to avoid negative/oversized dim regions.
            readonly property real _dimLeft: Math.max(0, Math.min(width, cropRect.x))
            readonly property real _dimRight: Math.max(0, Math.min(width, cropRect.x + cropRect.width))
            readonly property real _dimTop: Math.max(0, Math.min(height, cropRect.y))
            readonly property real _dimBottom: Math.max(0, Math.min(height, cropRect.y + cropRect.height))

            Rectangle { visible: cropOverlay.hasDrawableCrop; x: 0; y: 0; width: parent.width; height: cropOverlay._dimTop; color: "black"; opacity: 0.3 }
            Rectangle { visible: cropOverlay.hasDrawableCrop; x: 0; y: cropOverlay._dimBottom; width: parent.width; height: parent.height - cropOverlay._dimBottom; color: "black"; opacity: 0.3 }
            Rectangle { visible: cropOverlay.hasDrawableCrop; x: 0; y: cropOverlay._dimTop; width: cropOverlay._dimLeft; height: cropOverlay._dimBottom - cropOverlay._dimTop; color: "black"; opacity: 0.3 }
            Rectangle { visible: cropOverlay.hasDrawableCrop; x: cropOverlay._dimRight; y: cropOverlay._dimTop; width: parent.width - cropOverlay._dimRight; height: cropOverlay._dimBottom - cropOverlay._dimTop; color: "black"; opacity: 0.3 }

            Rectangle {
                id: cropRect
                property var viewRect: cropOverlay.currentCropViewRect()
                x: viewRect.x
                y: viewRect.y
                width: viewRect.width
                height: viewRect.height
                visible: cropOverlay.showCropContent
                color: "transparent"
                border.color: "white"
                border.width: 3

                Rectangle {
                    id: handleLine
                    visible: cropOverlay.hasDrawableCrop && mainMouseArea.isRotating
                    width: 2
                    height: 25
                    color: "white"
                    anchors.top: parent.bottom
                    anchors.horizontalCenter: parent.horizontalCenter
                }

                Rectangle {
                    id: rotateKnob
                    visible: cropOverlay.hasDrawableCrop && mainMouseArea.isRotating
                    width: 12
                    height: 12
                    radius: width / 2
                    color: "white"
                    border.color: "black"
                    border.width: 1
                    anchors.verticalCenter: handleLine.bottom
                    anchors.horizontalCenter: handleLine.horizontalCenter
                }
            }
        }

        // Alignment grid for rotate mode. Lives in the viewport (NOT inside
        // imageRotator/mainImage) so the lines stay screen-horizontal and
        // screen-vertical while the image rotates underneath them.
        Item {
            id: rotateAlignGrid
            anchors.fill: parent
            z: 10
            visible: loupeView.uiStateRef && loupeView.uiStateRef.isCropping && mainMouseArea.isRotating
            property real gridSpacing: 56
            property color lineColor: "#59ffffff"
            property color centerLineColor: "#b3ffffff"

            Repeater {
                id: rotateGridVLines
                model: rotateAlignGrid.visible ? 2 * Math.ceil(rotateAlignGrid.width / (2 * rotateAlignGrid.gridSpacing)) + 1 : 0
                Rectangle {
                    required property int index
                    property int centerIndex: (rotateGridVLines.count - 1) / 2
                    x: rotateAlignGrid.width / 2 + (index - centerIndex) * rotateAlignGrid.gridSpacing - width / 2
                    y: 0
                    width: 1
                    height: rotateAlignGrid.height
                    color: index === centerIndex ? rotateAlignGrid.centerLineColor : rotateAlignGrid.lineColor
                }
            }

            Repeater {
                id: rotateGridHLines
                model: rotateAlignGrid.visible ? 2 * Math.ceil(rotateAlignGrid.height / (2 * rotateAlignGrid.gridSpacing)) + 1 : 0
                Rectangle {
                    required property int index
                    property int centerIndex: (rotateGridHLines.count - 1) / 2
                    x: 0
                    y: rotateAlignGrid.height / 2 + (index - centerIndex) * rotateAlignGrid.gridSpacing - height / 2
                    width: rotateAlignGrid.width
                    height: 1
                    color: index === centerIndex ? rotateAlignGrid.centerLineColor : rotateAlignGrid.lineColor
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
                if (loupeView.uiStateRef && loupeView.uiStateRef.isDarkening) return Qt.CrossCursor
                if (!loupeView.uiStateRef || !loupeView.uiStateRef.isCropping) return Qt.ArrowCursor
                return Qt.CrossCursor
            }

        // Darken painting state
        property bool isDarkenPainting: false
        
        // Drag-to-pan with drag-and-drop when dragging outside window
        property real lastX: 0
        property real lastY: 0
        property real startX: 0
        property real startY: 0
        property bool isDraggingOutside: false
        property int dragThreshold: 10  // Minimum distance before checking for outside drag
        property bool isCropDragging: false
        // Flaky-trackpad tolerance: when a crop drag "ends", finalizing is
        // deferred briefly (cropReleaseGraceTimer). Cheap trackpads emit
        // spurious right-button release/press pairs while the user is still
        // dragging; without this grace window each spurious release aborts the
        // crop and the next press restarts a tiny box, so the user can never
        // draw the box they intend. While true, a real release is pending and a
        // fresh press will resume the in-progress drag instead of restarting.
        property bool cropReleasePending: false
        property real cropStartX: 0
        property real cropStartY: 0

        property string cropDragMode: "none" // "none", "new", "move", "left", "right", "top", "bottom", "topleft", "topright", "bottomleft", "bottomright"
        property real cropBoxStartLeft: 0
        property real cropBoxStartTop: 0
        property real cropBoxStartRight: 0
        property real cropBoxStartBottom: 0
        property real cropViewStartLeft: 0
        property real cropViewStartTop: 0
        property real cropViewStartRight: 0
        property real cropViewStartBottom: 0
        property real cropRotation: 0
        property real cropRotationPivotX: 0.5
        property real cropRotationPivotY: 0.5
        property bool isRotating: false
        property real cropStartAngle: 0
        property real cropStartRotation: 0
        property real cropStartAspect: -1
        
        // Reset rotation when image changes or updates (e.g. after crop save) to avoid persistence
        Connections {
            target: loupeView.uiStateRef
            function onCurrentIndexChanged() {
                mainMouseArea.resetCropRotation()
            }
        }


        onIsRotatingChanged: {
            if (loupeView.uiStateRef) {
                loupeView.uiStateRef.isCropRotating = isRotating && loupeView.uiStateRef.isCropping
                if (isRotating) {
                    loupeView.uiStateRef.statusMessage = "Press ESC to exit rotate mode"
                } else {
                    if (loupeView.uiStateRef.statusMessage === "Press ESC to exit rotate mode") {
                        loupeView.uiStateRef.statusMessage = ""
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
                if (loupeView.controllerRef && loupeView.uiStateRef && loupeView.uiStateRef.isCropping) {
                    loupeView.controllerRef.set_straighten_angle(mainMouseArea.pendingRotation, mainMouseArea.pendingAspect)
                }
            }
        }

        function clearPendingRotation(targetRotation) {
            rotationThrottleTimer.stop()
            pendingRotation = (targetRotation === undefined) ? 0 : targetRotation
            pendingAspect = -1
        }

        // Reset the straighten angle and its rotation pivot to the centered
        // defaults. Centralized so every crop-exit / image-change path stays in
        // sync (missing a pivot reset leaks a stale off-center rotation origin
        // into the next crop session).
        function resetCropRotation() {
            // Stop any in-flight throttle so a queued set_straighten_angle()
            // can't apply a stale angle to the next crop / image after reset.
            rotationThrottleTimer.stop()
            cropRotation = 0
            cropRotationPivotX = 0.5
            cropRotationPivotY = 0.5
        }

        function setCropBoxStart(left, top, right, bottom) {
            cropBoxStartLeft = left
            cropBoxStartTop = top
            cropBoxStartRight = right
            cropBoxStartBottom = bottom
        }

        function setCropViewStart(left, top, right, bottom) {
            cropViewStartLeft = left
            cropViewStartTop = top
            cropViewStartRight = right
            cropViewStartBottom = bottom
        }

        function setCropBoxStartFromBox(box) {
            if (!box || box.length !== 4) return
            setCropBoxStart(box[0], box[1], box[2], box[3])
            var r = cropOverlay.cropViewRectForBox(box)
            setCropViewStart(r.x, r.y, r.x + r.width, r.y + r.height)
        }

        function hasRightButton(mouse) {
            return mouse.button === Qt.RightButton || (mouse.buttons & Qt.RightButton)
        }

        function setInitialCropBoxAt(mx, my) {
            if (!loupeView.uiStateRef) return

            var left = Math.max(0, Math.min(999, mx))
            var top = Math.max(0, Math.min(999, my))
            var right = Math.min(1000, left + 10)
            var bottom = Math.min(1000, top + 10)

            if (right - left < 10) left = Math.max(0, right - 10)
            if (bottom - top < 10) top = Math.max(0, bottom - 10)

            loupeView.uiStateRef.currentCropBox = [Math.round(left), Math.round(top), Math.round(right), Math.round(bottom)]
        }

        function beginNewCrop(mouseX, mouseY, mx, my) {
            var clampedMx = Math.max(0, Math.min(1000, mx))
            var clampedMy = Math.max(0, Math.min(1000, my))
            var p = cropOverlay.mapFromItem(mainMouseArea, mouseX, mouseY)
            cropDragMode = "new"
            cropStartX = mouseX
            cropStartY = mouseY
            setCropBoxStart(clampedMx, clampedMy, clampedMx, clampedMy)
            setCropViewStart(p.x, p.y, p.x, p.y)
        }

        function beginCropInteraction() {
            loupeView.freezeCropImageSource()
            isCropDragging = true
            imageRotator._lockedZoom = imageRotator.zoomScale
            imageRotator._lockedPanX = panTransform.x
            imageRotator._lockedPanY = panTransform.y
        }

        function endCropInteraction() {
            // Any definitive end (commit, escape, navigation, grace timeout)
            // clears the pending-release guard so it can't leak across sessions.
            cropReleaseGraceTimer.stop()
            cropReleasePending = false

            isCropDragging = false
            cropDragMode = "none"

            imageRotator._lockedZoom = -1
            imageRotator._lockedPanX = -1e9
            imageRotator._lockedPanY = -1e9

            if (mainMouseArea.isRotating) imageRotator.recomputeFitScale(true)
            loupeView.forceActiveFocus()

            if (mainImage._sourceSizeStale) {
                mainImage._sourceSizeStale = false
                mainImage.handleSourceSizeChange()
            }
        }

        // Grace window after a crop drag release. A genuine release finalizes
        // the crop when this fires; a spurious trackpad release that is quickly
        // followed by another press cancels it (see onPressed) and the drag
        // resumes seamlessly.
        Timer {
            id: cropReleaseGraceTimer
            interval: 250
            repeat: false
            onTriggered: {
                if (mainMouseArea.cropReleasePending) {
                    mainMouseArea.cropReleasePending = false
                    mainMouseArea.endCropInteraction()
                }
            }
        }

        onPressed: function(mouse) {
            lastX = mouse.x
            lastY = mouse.y
            startX = mouse.x
            startY = mouse.y
            isDraggingOutside = false

            // Flaky-trackpad tolerance: if a crop release is still within its
            // grace window, finalize that deferred release before interpreting
            // the new press. The crop hit-test below handles both normal follow-up
            // drags and stuttered trackpad presses from the current visible box.
            if (cropReleasePending) {
                endCropInteraction()
            }

            // Darken painting mode
            if (loupeView.uiStateRef && loupeView.uiStateRef.isDarkening && !loupeView.uiStateRef.isCropping && loupeView.controllerRef) {
                var imgCoords = mapToImageCoordinates(Qt.point(mouse.x, mouse.y))
                var sx = Math.max(0, Math.min(1, imgCoords.x))
                var sy = Math.max(0, Math.min(1, imgCoords.y))
                if (imgCoords.x < 0 || imgCoords.x > 1 || imgCoords.y < 0 || imgCoords.y > 1) {
                    return  // click outside image bounds
                }
                var strokeType = (mouse.button === Qt.RightButton) ? "protect" : "add"
                loupeView.controllerRef.start_darken_stroke(sx, sy, strokeType)
                isDarkenPainting = true
                return
            }

            if (hasRightButton(mouse) && (!loupeView.uiStateRef || !loupeView.uiStateRef.isCropping)) {
                // Activate drag guard BEFORE toggle_crop_mode so that any
                // source/geometry changes it triggers are properly deferred.
                beginCropInteraction()

                if (loupeView.uiStateRef && !loupeView.uiStateRef.isCropping && loupeView.controllerRef) {
                    loupeView.controllerRef.toggle_crop_mode() // Ensure mode is ON
                }

                if (!loupeView.uiStateRef || !loupeView.uiStateRef.isCropping) {
                    endCropInteraction()
                    return
                }
                
                // Ensure loupeView has active focus so Escape key works
                loupeView.forceActiveFocus()
                
                // Start a NEW crop rectangle immediately from the clicked point
                var coords = mapToImageCoordinates(Qt.point(mouse.x, mouse.y))
                var mx = coords.x * 1000
                var my = coords.y * 1000

                beginNewCrop(mouse.x, mouse.y, mx, my)
                setInitialCropBoxAt(mx, my)
                return
            }
            
            if (loupeView.uiStateRef && loupeView.uiStateRef.isCropping) {
                // Check if clicking on existing crop box in viewport space.
                var box = loupeView.uiStateRef.currentCropBox
                if (box && box.length === 4) box = box.slice(0)
                
                var isFullImage = box && box.length === 4 && box[0] === 0 && box[1] === 0 && box[2] === 1000 && box[3] === 1000
                
                var coords = mapToImageCoordinates(Qt.point(mouse.x, mouse.y))
                var mx = coords.x * 1000
                var my = coords.y * 1000
                var viewPoint = cropOverlay.mapFromItem(mainMouseArea, mouse.x, mouse.y)
                var viewRect = cropOverlay.cropViewRectForBox(box)
                var viewLeft = viewRect.x
                var viewTop = viewRect.y
                var viewRight = viewRect.x + viewRect.width
                var viewBottom = viewRect.y + viewRect.height
                // Scale grab tolerance with DPI to match the rotate-knob hit test
                // (which uses Screen.devicePixelRatio) so edges and the knob feel
                // equally forgiving on HiDPI displays.
                var edgeThreshold = 10 * Screen.devicePixelRatio

                // Make it so the user doesn't have to click exactly on the crop box to modify it.
                var inside = viewPoint.x >= (viewLeft - edgeThreshold) && viewPoint.x <= (viewRight + edgeThreshold) && viewPoint.y >= (viewTop - edgeThreshold) && viewPoint.y <= (viewBottom + edgeThreshold)
                
                if (mainMouseArea.isRotating && cropOverlay.visible && rotateKnob.visible) {
                    var k = mainMouseArea.mapFromItem(rotateKnob, rotateKnob.width/2, rotateKnob.height/2)
                    var dxk = mouse.x - k.x
                    var dyk = mouse.y - k.y
                    var distk = Math.sqrt(dxk*dxk + dyk*dyk)

                    if (distk < 22 * Screen.devicePixelRatio) { // a little forgiving
                        cropDragMode = "rotate"

                        // Seed cropBoxStart variables before deriving the fixed crop pivot.
                        if (box && box.length === 4) {
                            setCropBoxStartFromBox(box)
                            cropRotationPivotX = ((box[0] + box[2]) / 2000)
                            cropRotationPivotY = ((box[1] + box[3]) / 2000)
                        }

                        var c = mainMouseArea.mapFromItem(cropOverlay, (cropViewStartLeft + cropViewStartRight) / 2, (cropViewStartTop + cropViewStartBottom) / 2)
                        cropStartAngle = Math.atan2(mouse.y - c.y, mouse.x - c.x) * 180 / Math.PI
                        cropStartRotation = cropRotation
                        
                        // Calculate start aspect ratio (in pixels)
                        if (mainImage.width > 0) {
                            if (box && box.length === 4) {
                                var boxW = (box[2] - box[0]) / 1000 * mainImage.width
                                var boxH = (box[3] - box[1]) / 1000 * mainImage.height
                                if (boxH > 0) cropStartAspect = boxW / boxH
                            }
                        }

                        beginCropInteraction()
                        return
                    }
                }
                
                // If crop box is full image, always start a new crop.
                // Independent of the rotate-knob check above so crop hit-testing
                // still runs when a knob click is missed in rotate mode.
                if (isFullImage) {
                    // Start a new crop rectangle from the clicked point
                    beginNewCrop(mouse.x, mouse.y, mx, my)
                } else if (inside) {
                    // Determine which edge/corner is being dragged in viewport space.
                    var nearLeft = Math.abs(viewPoint.x - viewLeft) < edgeThreshold
                    var nearRight = Math.abs(viewPoint.x - viewRight) < edgeThreshold
                    var nearTop = Math.abs(viewPoint.y - viewTop) < edgeThreshold
                    var nearBottom = Math.abs(viewPoint.y - viewBottom) < edgeThreshold
                    
                    if (nearLeft && nearTop) cropDragMode = "topleft"
                    else if (nearRight && nearTop) cropDragMode = "topright"
                    else if (nearLeft && nearBottom) cropDragMode = "bottomleft"
                    else if (nearRight && nearBottom) cropDragMode = "bottomright"
                    else if (nearLeft) cropDragMode = "left"
                    else if (nearRight) cropDragMode = "right"
                    else if (nearTop) cropDragMode = "top"
                    else if (nearBottom) cropDragMode = "bottom"
                    else cropDragMode = "move"

                    cropStartX = mouse.x
                    cropStartY = mouse.y
                    // Reuse the viewRect already computed above instead of
                    // recomputing cropViewRectForBox inside setCropBoxStartFromBox.
                    setCropBoxStart(box[0], box[1], box[2], box[3])
                    setCropViewStart(viewLeft, viewTop, viewRight, viewBottom)
                } else {
                    // Start new crop rectangle
                    beginNewCrop(mouse.x, mouse.y, mx, my)
                }
                beginCropInteraction()
            }
        }        
        // mapToImageCoordinates maps directly to mainImage for image-local tools.
        function mapToImageCoordinates(screenPoint) {
            if (!mainImage) return {x:0, y:0}
            var w = mainImage.width > 0 ? mainImage.width : mainImage.sourceSize.width
            var h = mainImage.height > 0 ? mainImage.height : mainImage.sourceSize.height
            if (w <= 0 || h <= 0) return {x:0, y:0}
            var p = mainImage.mapFromItem(mainMouseArea, screenPoint.x, screenPoint.y)
            return { x: p.x / w, y: p.y / h }
        }
        onPositionChanged: function(mouse) {
            // Darken painting drag — clamp to image bounds
            if (isDarkenPainting && loupeView.controllerRef) {
                var imgCoords = mapToImageCoordinates(Qt.point(mouse.x, mouse.y))
                var cx = Math.max(0, Math.min(1, imgCoords.x))
                var cy = Math.max(0, Math.min(1, imgCoords.y))
                loupeView.controllerRef.continue_darken_stroke(cx, cy)
                return
            }

            if (loupeView.uiStateRef && loupeView.uiStateRef.isCropping && isCropDragging) {
                if (cropDragMode === "new") {
                    // Update crop rectangle while dragging
                    updateCropBox(cropStartX, cropStartY, mouse.x, mouse.y, true)
                } else if (cropDragMode === "rotate") {
                    var c = mainMouseArea.mapFromItem(cropOverlay, (cropViewStartLeft + cropViewStartRight) / 2, (cropViewStartTop + cropViewStartBottom) / 2)
                    var currentAngle = Math.atan2(mouse.y - c.y, mouse.x - c.x) * 180 / Math.PI
                    var delta = currentAngle - cropStartAngle
                    // Handle wrap-around
                    if (delta > 180) delta -= 360
                    if (delta < -180) delta += 360
                    
                    var newRotation = cropStartRotation + delta

                    // Update rotation state
                    cropRotation = newRotation

                    // The crop box is intentionally left unchanged while straightening:
                    // the image rotates under a fixed crop frame. This matches the
                    // backend (_crop_box_canvas_rect), which preserves the box and lets
                    // rotation introduce black fill at the corners instead of silently
                    // shrinking the user's crop.

                    // Update rotation in backend live (throttled)
                    if (loupeView.controllerRef) {
                        pendingRotation = cropRotation
                        pendingAspect = -1
                        
                        if (!rotationThrottleTimer.running) {
                            rotationThrottleTimer.start()
                        }
                    }
                    // Return early to prevent overwriting crop box during rotation
                    return
                } else {
                    // Handle move/resize against the fixed viewport crop frame.
                    var startPoint = cropOverlay.mapFromItem(mainMouseArea, cropStartX, cropStartY)
                    var currentPoint = cropOverlay.mapFromItem(mainMouseArea, mouse.x, mouse.y)
                    var left = cropViewStartLeft
                    var top = cropViewStartTop
                    var right = cropViewStartRight
                    var bottom = cropViewStartBottom
                    
                    // Adjust based on drag mode
                    if (cropDragMode === "move") {
                        var dx = currentPoint.x - startPoint.x
                        var dy = currentPoint.y - startPoint.y

                        var width = cropViewStartRight - cropViewStartLeft
                        var height = cropViewStartBottom - cropViewStartTop

                        left = cropViewStartLeft + dx
                        top = cropViewStartTop + dy
                        right = left + width
                        bottom = top + height
                    } else {
                        if (cropDragMode.includes("left")) left = currentPoint.x;
                        if (cropDragMode.includes("right")) right = currentPoint.x;
                        if (cropDragMode.includes("top")) top = currentPoint.y;
                        if (cropDragMode.includes("bottom")) bottom = currentPoint.y;
                    }

                    var nextBox = cropOverlay.cropBoxFromViewRect(left, top, right, bottom, cropDragMode === "move")
                    if (cropDragMode !== "move") {
                        var constrainedBox = applyAspectRatioConstraint(nextBox[0], nextBox[1], nextBox[2], nextBox[3], cropDragMode)
                        nextBox = [constrainedBox[0], constrainedBox[1], constrainedBox[2], constrainedBox[3]]
                    }

                    loupeView.uiStateRef.currentCropBox = nextBox
                }
                return
            }
            
            if (pressed && (pressedButtons & Qt.LeftButton) && !(pressedButtons & Qt.RightButton) && !isDraggingOutside) {
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
                        if (loupeView.controllerRef) loupeView.controllerRef.start_drag_current_image()
                        return
                    }
                }
                
                // Normal pan behavior (only when not cropping)
                if (!loupeView.uiStateRef || !loupeView.uiStateRef.isCropping) {
                    panTransform.x += (mouse.x - lastX)
                    panTransform.y += (mouse.y - lastY)
                    lastX = mouse.x
                    lastY = mouse.y
                }
            }
        }
        
        onReleased: function(mouse) {
            // Darken painting release
            if (isDarkenPainting) {
                isDarkenPainting = false
                if (loupeView.controllerRef) loupeView.controllerRef.finish_darken_stroke()
                return
            }

            isDraggingOutside = false
            if (loupeView.uiStateRef && loupeView.uiStateRef.isCropping && isCropDragging) {
                // Defer finalizing the crop briefly. Cheap trackpads emit
                // spurious release/press pairs mid-drag; if another press
                // arrives within the grace window we resume the drag instead of
                // aborting it (see onPressed and cropReleaseGraceTimer).
                cropReleasePending = true
                isCropDragging = false
                cropReleaseGraceTimer.restart()
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
            if (!loupeView.uiStateRef || !mainImage.source) return

            var p1 = cropOverlay.mapFromItem(mainMouseArea, x1, y1)
            var p2 = cropOverlay.mapFromItem(mainMouseArea, x2, y2)
            var left = Math.min(p1.x, p2.x)
            var right = Math.max(p1.x, p2.x)
            var top = Math.min(p1.y, p2.y)
            var bottom = Math.max(p1.y, p2.y)
            
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
                
                var nextBox = cropOverlay.cropBoxFromViewRect(left, top, right, bottom)
                var constrainedBox = applyAspectRatioConstraint(nextBox[0], nextBox[1], nextBox[2], nextBox[3], mode)
                loupeView.uiStateRef.currentCropBox = [constrainedBox[0], constrainedBox[1], constrainedBox[2], constrainedBox[3]]
                return
            } else {
                // Just ensure minimum size
                if (right - left < 10) {
                    if (p2.x >= p1.x) right = left + 10
                    else left = right - 10
                }
                if (bottom - top < 10) {
                    if (p2.y >= p1.y) bottom = top + 10
                    else top = bottom - 10
                }
            }
            
            loupeView.uiStateRef.currentCropBox = cropOverlay.cropBoxFromViewRect(left, top, right, bottom)
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
            if (loupeView.uiStateRef.currentAspectRatioIndex <= 0 || !loupeView.uiStateRef.aspectRatioNames || loupeView.uiStateRef.aspectRatioNames.length <= loupeView.uiStateRef.currentAspectRatioIndex) {
                // No aspect ratio, just clamp to bounds
                return [
                    Math.max(0, Math.min(1000, left)),
                    Math.max(0, Math.min(1000, top)),
                    Math.max(0, Math.min(1000, right)),
                    Math.max(0, Math.min(1000, bottom))
                ];
            }

            var ratioName = loupeView.uiStateRef.aspectRatioNames[loupeView.uiStateRef.currentAspectRatioIndex];
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
            // At an odd 90-degree straighten the committed output swaps width
            // and height (editor.py _crop_box_canvas_rect), so a 16:9 lock must
            // constrain the source box to 9:16 to land 16:9 after the swap.
            // Mirror the swap cropViewRectForBox/cropBoxFromViewRect already do.
            if (cropOverlay._dimensionsAreSwapped()) pixelAspect = 1.0 / pixelAspect;
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
            if (!loupeView.uiStateRef || !loupeView.uiStateRef.currentCropBox || loupeView.uiStateRef.currentCropBox.length !== 4) return
            var box = loupeView.uiStateRef.currentCropBox
            
            // Start with center of current box
            var cx = (box[0] + box[2]) / 2
            var cy = (box[1] + box[3]) / 2
            
            // If current box is basically full image (default), use image center
            if (box[0] <= 10 && box[1] <= 10 && box[2] >= 990 && box[3] >= 990) {
                cx = 500
                cy = 500
            }
            
            var ratioName = loupeView.uiStateRef.aspectRatioNames[loupeView.uiStateRef.currentAspectRatioIndex];
            var ratioPair = getAspectRatio(ratioName);

            if (!ratioPair) { // Freeform selected
                loupeView.uiStateRef.currentCropBox = [0, 0, 1000, 1000] // Reset to full image
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
            
            loupeView.uiStateRef.currentCropBox = [Math.round(left), Math.round(top), Math.round(right), Math.round(bottom)]
        }
    }
    
    // Crop rectangle overlay (Moved to mainImage)
    
    // Aspect ratio selector window (upper left corner)
    Rectangle {
        id: aspectRatioWindow
        visible: loupeView.uiStateRef && loupeView.uiStateRef.isCropping
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
        
        property bool isDark: loupeView.isDarkTheme
        
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
                model: loupeView.uiStateRef && loupeView.uiStateRef.aspectRatioNames ? loupeView.uiStateRef.aspectRatioNames.length : 0
                
                Rectangle {
                    id: aspectRatioOption
                    required property int index
                    width: parent.width
                    height: 30
                    color: loupeView.uiStateRef && loupeView.uiStateRef.currentAspectRatioIndex === aspectRatioOption.index ? "#555555" : "transparent"
                    radius: 3
                    
                    Text {
                        anchors.left: parent.left
                        anchors.leftMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        text: loupeView.uiStateRef && loupeView.uiStateRef.aspectRatioNames ? loupeView.uiStateRef.aspectRatioNames[aspectRatioOption.index] : ""
                        color: aspectRatioWindow.isDark ? "white" : "black"
                        font.pixelSize: 11
                    }
                    
                    MouseArea {
                        anchors.fill: parent
                        onClicked: {
                            if (loupeView.uiStateRef) {
                                loupeView.uiStateRef.currentAspectRatioIndex = aspectRatioOption.index
                                // Re-apply aspect ratio to current crop box
                                if (loupeView.uiStateRef.currentCropBox && loupeView.uiStateRef.currentCropBox.length === 4) {
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
