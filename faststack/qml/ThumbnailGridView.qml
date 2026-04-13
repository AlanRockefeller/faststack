pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

// Main grid view for thumbnail browser
Item {
    id: gridViewRoot
    anchors.fill: parent

    // Theme property (bound by parent)
    property bool isDarkTheme: false
    property var uiStateRef: typeof uiState !== "undefined" ? uiState : null
    property var thumbnailModelRef: typeof thumbnailModel !== "undefined" ? thumbnailModel : null

    // Configuration
    property int cellWidth: 190
    property int cellHeight: 210

    // Selection count for keyboard handler (use gridSelectedCount for efficiency)
    property int selectedCount: gridViewRoot.uiStateRef ? gridViewRoot.uiStateRef.gridSelectedCount : 0
    // Preserve per-directory view state so returning from a child folder restores the scroll position.
    property var directoryViewState: ({})
    property string trackedDirectory: gridViewRoot.uiStateRef ? gridViewRoot.uiStateRef.gridDirectory : ""
    property bool pendingDirectoryRestore: false

    function clampIndex(index) {
        // For an empty model, keep 0 as a safe default index instead of using -1.
        if (thumbnailGrid.count <= 0) return 0
        return Math.max(0, Math.min(index, thumbnailGrid.count - 1))
    }

    function clampContentY(value) {
        var maxY = Math.max(0, thumbnailGrid.contentHeight - thumbnailGrid.height)
        return Math.max(0, Math.min(value, maxY))
    }

    function resetViewToTop() {
        thumbnailGrid.currentIndex = 0
        thumbnailGrid.contentY = 0
    }

    function isDirectoryLoadComplete() {
        return !gridViewRoot.uiStateRef || gridViewRoot.uiStateRef.isFolderLoaded
    }

    function saveDirectoryViewState(directory) {
        // `!directory` intentionally covers null/undefined and the empty-string default
        // used when no uiStateRef/gridDirectory is available yet.
        if (!directory || gridViewRoot.pendingDirectoryRestore) return
        // During a directory switch, GridView can briefly emit top-of-list updates before
        // trackedDirectory catches up. Ignore those transition events so we don't overwrite
        // the previous directory's saved position with a transient reset-to-top state.
        if (gridViewRoot.uiStateRef && gridViewRoot.uiStateRef.gridDirectory !== directory) return

        gridViewRoot.directoryViewState[directory] = {
            contentY: gridViewRoot.clampContentY(thumbnailGrid.contentY),
            currentIndex: gridViewRoot.clampIndex(thumbnailGrid.currentIndex)
        }
    }

    function applyDirectoryViewState(directory) {
        if (thumbnailGrid.count <= 0) {
            if (!gridViewRoot.isDirectoryLoadComplete()) return false

            gridViewRoot.pendingDirectoryRestore = false
            gridViewRoot.resetViewToTop()
            return true
        }

        var state = gridViewRoot.directoryViewState[directory]
        gridViewRoot.pendingDirectoryRestore = false
        if (!state) {
            gridViewRoot.resetViewToTop()
            return true
        }

        thumbnailGrid.currentIndex = gridViewRoot.clampIndex(state.currentIndex)
        thumbnailGrid.contentY = gridViewRoot.clampContentY(state.contentY)
        return true
    }

    function retryPendingDirectoryRestore(directory) {
        Qt.callLater(function() {
            if (!gridViewRoot.pendingDirectoryRestore || gridViewRoot.trackedDirectory !== directory) return

            // Later retries are driven by onCountChanged and onIsFolderLoadedChanged.
            if (!gridViewRoot.applyDirectoryViewState(directory)) return

            if (thumbnailGrid.prefetchEnabled) prefetchTimer.restart()
        })
    }

    function queueDirectoryRestore(directory) {
        gridViewRoot.trackedDirectory = directory
        gridViewRoot.pendingDirectoryRestore = true

        gridViewRoot.retryPendingDirectoryRestore(directory)
    }

    // Wrapper to expose function to Loader
    function setPrefetchEnabled(enabled) {
        thumbnailGrid.setPrefetchEnabled(enabled)
    }

    // Grid view
    GridView {
        id: thumbnailGrid
        anchors.fill: parent
        anchors.leftMargin: 8
        anchors.rightMargin: 8
        anchors.topMargin: 8
        anchors.bottomMargin: 40  // Extra space for status bar

        cellWidth: gridViewRoot.cellWidth
        cellHeight: gridViewRoot.cellHeight
        clip: true
        focus: true
        keyNavigationEnabled: false  // We handle all navigation in Keys.onPressed
        highlightFollowsCurrentItem: true
        currentIndex: 0  // Track cursor position

        model: gridViewRoot.thumbnailModelRef

        delegate: ThumbnailTile {
            width: thumbnailGrid.cellWidth - 10
            height: thumbnailGrid.cellHeight - 10

            // Theme binding from parent
            isDarkTheme: gridViewRoot.isDarkTheme

            tileHasCursor: index === thumbnailGrid.currentIndex
        }

        // Scroll bar
        ScrollBar.vertical: ScrollBar {
            active: true
            policy: ScrollBar.AsNeeded
        }

        // Visible range prefetch
        property int prefetchMargin: 2  // rows
        property bool prefetchEnabled: false  // Gate for prefetch requests (default off for startup safety)

        function setPrefetchEnabled(enabled) {
            prefetchEnabled = enabled
            if (enabled) {
                // Restore position to ensure we don't prefetch top-of-list by accident
                if (thumbnailGrid.currentIndex >= 0) {
                    thumbnailGrid.positionViewAtIndex(thumbnailGrid.currentIndex, GridView.Contain)
                }
                // Schedule a fresh prefetch with a slight delay to allow layout to settle
                // This prevents "coalesced_from=prefetch" delays for visible items
                Qt.callLater(function() {
                    if (prefetchEnabled) prefetchTimer.restart()
                })
            } else {
                prefetchTimer.stop()
                // Cancel any queued work immediately to clear the backlog
                if (gridViewRoot.uiStateRef) gridViewRoot.uiStateRef.cancelThumbnailPrefetch()
            }
        }

        onContentYChanged: {
            gridViewRoot.saveDirectoryViewState(gridViewRoot.trackedDirectory)
            if (prefetchEnabled && !prefetchTimer.running) prefetchTimer.start()  // Throttle
        }

        onCurrentIndexChanged: {
            gridViewRoot.saveDirectoryViewState(gridViewRoot.trackedDirectory)
        }

        Timer {
            id: prefetchTimer
            interval: 50
            repeat: false
            onTriggered: {
                thumbnailGrid.triggerPrefetch()
            }
        }

        function triggerPrefetch() {
            if (!prefetchEnabled) return
            if (!gridViewRoot.uiStateRef || thumbnailGrid.count === 0) return

            var cellW = thumbnailGrid.cellWidth
            var cellH = thumbnailGrid.cellHeight
            if (cellW <= 0 || cellH <= 0) return

            // Calculate columns and visible rows
            var cols = Math.max(1, Math.floor(thumbnailGrid.width / cellW))
            var firstRow = Math.max(0, Math.floor(thumbnailGrid.contentY / cellH))
            var rowsVisible = Math.max(1, Math.ceil(thumbnailGrid.height / cellH))

            // Padding rows for smoother scrolling
            var padRows = thumbnailGrid.prefetchMargin || 4
            var startRow = Math.max(0, firstRow - padRows)
            var endRow = firstRow + rowsVisible + padRows

            // Calculate item indices
            var topIndex = startRow * cols
            var bottomIndex = (endRow * cols) - 1

            // Clamp to model boundaries
            topIndex = Math.max(0, Math.min(topIndex, thumbnailGrid.count - 1))
            bottomIndex = Math.max(0, Math.min(bottomIndex, thumbnailGrid.count - 1))

            // Determine budget (intended items to prefetch)
            var maxCount = (rowsVisible + 2 * padRows) * cols
            maxCount = Math.max(200, Math.min(maxCount, 800))

            // Log for debugging
            if (gridViewRoot.uiStateRef && gridViewRoot.uiStateRef.debugMode) {
                console.log("Prefetch range:", topIndex, "-", bottomIndex, "maxCount=" + maxCount + " cols=" + cols)
            }

            // Actually trigger prefetch
            if (gridViewRoot.uiStateRef) {
                gridViewRoot.uiStateRef.gridPrefetchRange(topIndex, bottomIndex, maxCount)
            }
        }

        // Trigger prefetch when model count changes (initial load)
        onCountChanged: {
            if (count <= 0) {
                gridViewRoot.resetViewToTop()
                return
            }
            if (currentIndex >= count) {
                currentIndex = count - 1
            }
            if (gridViewRoot.pendingDirectoryRestore) {
                // Defensive fallback: if model population ever becomes async and races
                // with gridDirectoryChanged, restore again after the new count lands.
                // Keep retrying while the folder is still loading so a transient
                // zero-count state doesn't get treated as a definitive empty folder.
                gridViewRoot.retryPendingDirectoryRestore(gridViewRoot.trackedDirectory)
            }
            if (prefetchEnabled) prefetchTimer.restart()
        }

        // Empty state
        Text {
            anchors.centerIn: parent
            visible: thumbnailGrid.count === 0 && gridViewRoot.uiStateRef && gridViewRoot.uiStateRef.isFolderLoaded
            text: "No images in this folder"
            color: gridViewRoot.isDarkTheme ? "#888888" : "#666666"
            font.pixelSize: 16
        }

        // Keyboard shortcuts (inside GridView so it receives focus)
        Keys.onPressed: function(event) {
            if (!gridViewRoot.uiStateRef) return

            // Calculate columns with epsilon to handle rounding issues during window resizing
            var cols = Math.max(1, Math.floor((thumbnailGrid.width + 1) / thumbnailGrid.cellWidth))

            if (event.key === Qt.Key_Escape) {
                // Clear selection or switch to loupe
                if (gridViewRoot.selectedCount > 0) {
                    gridViewRoot.uiStateRef.gridClearSelection()
                } else {
                    gridViewRoot.uiStateRef.toggleGridView()
                }
                event.accepted = true
            } else if (event.key === Qt.Key_Left) {
                // Move cursor left
                if (thumbnailGrid.currentIndex > 0) {
                    thumbnailGrid.currentIndex--
                    thumbnailGrid.positionViewAtIndex(thumbnailGrid.currentIndex, GridView.Contain)
                }
                event.accepted = true
            } else if (event.key === Qt.Key_Right) {
                // Move cursor right
                if (thumbnailGrid.currentIndex < thumbnailGrid.count - 1) {
                    thumbnailGrid.currentIndex++
                    thumbnailGrid.positionViewAtIndex(thumbnailGrid.currentIndex, GridView.Contain)
                }
                event.accepted = true
            } else if (event.key === Qt.Key_Up) {
                // Move cursor up one row
                var newIndex = thumbnailGrid.currentIndex - cols
                if (newIndex >= 0) {
                    thumbnailGrid.currentIndex = newIndex
                    thumbnailGrid.positionViewAtIndex(thumbnailGrid.currentIndex, GridView.Contain)
                }
                event.accepted = true
            } else if (event.key === Qt.Key_Down) {
                // Move cursor down one row
                var newIndex = thumbnailGrid.currentIndex + cols
                if (newIndex < thumbnailGrid.count) {
                    thumbnailGrid.currentIndex = newIndex
                    thumbnailGrid.positionViewAtIndex(thumbnailGrid.currentIndex, GridView.Contain)
                }
                event.accepted = true
            } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                // Open current item in loupe view (or navigate into folder)
                gridViewRoot.uiStateRef.gridOpenIndex(thumbnailGrid.currentIndex)
                event.accepted = true
            } else if (event.key === Qt.Key_Space) {
                // Toggle selection on current item
                gridViewRoot.uiStateRef.gridSelectIndex(thumbnailGrid.currentIndex, false, true)
                event.accepted = true
            } else if (event.key === Qt.Key_B) {
                // Add selected images to batch
                gridViewRoot.uiStateRef.gridAddSelectionToBatch()
                event.accepted = true
            } else if (event.key === Qt.Key_Delete || event.key === Qt.Key_Backspace) {
                // Delete selected images or cursor image
                gridViewRoot.uiStateRef.gridDeleteAtCursor(thumbnailGrid.currentIndex)
                event.accepted = true
            }
        }
    }

    // Focus and layout triggers
    onWidthChanged: { if (thumbnailGrid.prefetchEnabled) prefetchTimer.restart() }
    onHeightChanged: { if (thumbnailGrid.prefetchEnabled) prefetchTimer.restart() }

    Component.onCompleted: {
        if (gridViewRoot.uiStateRef && gridViewRoot.uiStateRef.debugThumbTiming)
            console.log("[THUMB-TIMING] GridView Component.onCompleted t=" + Date.now() + "ms")
        thumbnailGrid.forceActiveFocus()
        
        // Sync initial cursor position from state to prevent top-of-list prefetch
        if (gridViewRoot.uiStateRef && gridViewRoot.uiStateRef.currentIndex >= 0 && gridViewRoot.uiStateRef.currentIndex < thumbnailGrid.count) {
            thumbnailGrid.currentIndex = gridViewRoot.uiStateRef.currentIndex
            thumbnailGrid.positionViewAtIndex(thumbnailGrid.currentIndex, GridView.Center)
        }

        gridViewRoot.trackedDirectory = gridViewRoot.uiStateRef ? gridViewRoot.uiStateRef.gridDirectory : ""
        gridViewRoot.saveDirectoryViewState(gridViewRoot.trackedDirectory)
    }


    Connections {
        target: gridViewRoot.uiStateRef
        function onGridDirectoryChanged(directory) {
            gridViewRoot.queueDirectoryRestore(directory)
        }
        function onIsFolderLoadedChanged() {
            if (gridViewRoot.pendingDirectoryRestore && gridViewRoot.uiStateRef && gridViewRoot.uiStateRef.isFolderLoaded) {
                gridViewRoot.retryPendingDirectoryRestore(gridViewRoot.trackedDirectory)
            }
        }
        function onIsGridViewActiveChanged() {
            if (gridViewRoot.uiStateRef.isGridViewActive) {
                // Prefetch triggering is now handled by Main.qml via setPrefetchEnabled
                // to avoid transient state issues.
                thumbnailGrid.forceActiveFocus()
            }
        }
        function onGridScrollToIndex(index) {
            // Scroll to show the current loupe image when entering grid view
            if (index >= 0 && index < thumbnailGrid.count) {
                // Move cursor to match the loupe image
                thumbnailGrid.currentIndex = index
                // Scroll to center it in the view
                thumbnailGrid.positionViewAtIndex(index, GridView.Center)
            }
        }
    }
}
