import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Main grid view for thumbnail browser
Item {
    id: gridViewRoot
    anchors.fill: parent

    // Theme property (bound by parent)
    property bool isDarkTheme: false

    // Configuration
    property int cellWidth: 190
    property int cellHeight: 210

    // Selection count for keyboard handler (use gridSelectedCount for efficiency)
    property int selectedCount: uiState ? uiState.gridSelectedCount : 0

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

        model: thumbnailModel

        delegate: ThumbnailTile {
            width: thumbnailGrid.cellWidth - 10
            height: thumbnailGrid.cellHeight - 10

            // Theme binding from parent
            isDarkTheme: gridViewRoot.isDarkTheme

            // Model role bindings - use attached property 'index' directly
            // Model roles become context properties in delegate
            tileIndex: index
            tileFilePath: filePath || ""
            tileFileName: fileName || ""
            tileIsFolder: isFolder || false
            tileIsStacked: isStacked || false
            tileIsUploaded: isUploaded || false
            tileIsEdited: isEdited || false
            tileIsRestacked: isRestacked || false
            tileIsFavorite: isFavorite || false
            tileIsInBatch: isInBatch || false
            tileIsCurrent: isCurrent || false
            tileThumbnailSource: thumbnailSource || ""
            tileFolderStats: folderStats || null
            tileIsSelected: isSelected || false
            tileIsParentFolder: isParentFolder || false
            tileHasBackups: hasBackups || false
            tileHasDeveloped: hasDeveloped || false
            tileHasCursor: index === thumbnailGrid.currentIndex
        }

        // Scroll bar
        ScrollBar.vertical: ScrollBar {
            active: true
            policy: ScrollBar.AsNeeded
        }

        // Visible range prefetch
        property int prefetchMargin: 2  // rows

        onContentYChanged: {
            if (!prefetchTimer.running) prefetchTimer.start()  // Throttle
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
            if (!uiState || thumbnailGrid.count === 0) return

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
            if (uiState && uiState.debugMode) {
                console.log("Prefetch range:", topIndex, "-", bottomIndex, "maxCount=" + maxCount + " cols=" + cols)
            }

            // Actually trigger prefetch
            if (uiState) {
                uiState.gridPrefetchRange(topIndex, bottomIndex, maxCount)
            }
        }

        // Trigger prefetch when model count changes (initial load)
        onCountChanged: {
            if (count <= 0) {
                currentIndex = 0
                return
            }
            if (currentIndex >= count) {
                currentIndex = count - 1
            }
            prefetchTimer.restart()
        }

        // Empty state
        Text {
            anchors.centerIn: parent
            visible: thumbnailGrid.count === 0 && uiState && uiState.isFolderLoaded
            text: "No images in this folder"
            color: gridViewRoot.isDarkTheme ? "#888888" : "#666666"
            font.pixelSize: 16
        }

        // Keyboard shortcuts (inside GridView so it receives focus)
        Keys.onPressed: function(event) {
            if (!uiState) return

            // Calculate columns with epsilon to handle rounding issues during window resizing
            var cols = Math.max(1, Math.floor((thumbnailGrid.width + 1) / thumbnailGrid.cellWidth))

            if (event.key === Qt.Key_Escape) {
                // Clear selection or switch to loupe
                if (gridViewRoot.selectedCount > 0) {
                    uiState.gridClearSelection()
                } else {
                    uiState.toggleGridView()
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
                uiState.gridOpenIndex(thumbnailGrid.currentIndex)
                event.accepted = true
            } else if (event.key === Qt.Key_Space) {
                // Toggle selection on current item
                uiState.gridSelectIndex(thumbnailGrid.currentIndex, false, true)
                event.accepted = true
            } else if (event.key === Qt.Key_B) {
                // Add selected images to batch
                uiState.gridAddSelectionToBatch()
                event.accepted = true
            } else if (event.key === Qt.Key_Delete || event.key === Qt.Key_Backspace) {
                // Delete selected images or cursor image
                uiState.gridDeleteAtCursor(thumbnailGrid.currentIndex)
                event.accepted = true
            }
        }
    }

    // Focus and layout triggers
    onWidthChanged: prefetchTimer.restart()
    onHeightChanged: prefetchTimer.restart()

    Component.onCompleted: {
        if (uiState && uiState.debugThumbTiming)
            console.log("[THUMB-TIMING] GridView Component.onCompleted t=" + Date.now() + "ms")
        thumbnailGrid.forceActiveFocus()
        // Trigger initial prefetch after geometry stabilizes
        Qt.callLater(function() { prefetchTimer.restart() })
    }


    Connections {
        target: uiState
        function onIsGridViewActiveChanged() {
            if (uiState.isGridViewActive) {
                // Trigger prefetch when grid view becomes active
                thumbnailGrid.triggerPrefetch()
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
