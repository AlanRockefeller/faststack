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
            tileIsInBatch: isInBatch || false
            tileIsCurrent: isCurrent || false
            tileThumbnailSource: thumbnailSource || ""
            tileFolderStats: folderStats || null
            tileIsSelected: isSelected || false
            tileIsParentFolder: isParentFolder || false
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
            prefetchTimer.restart()
        }

        Timer {
            id: prefetchTimer
            interval: 100
            repeat: false
            onTriggered: {
                thumbnailGrid.triggerPrefetch()
            }
        }

        function triggerPrefetch() {
            if (thumbnailGrid.count === 0) return

            // Calculate visible range
            var topIndex = thumbnailGrid.indexAt(thumbnailGrid.contentX, thumbnailGrid.contentY)
            var bottomIndex = thumbnailGrid.indexAt(
                thumbnailGrid.contentX + thumbnailGrid.width,
                thumbnailGrid.contentY + thumbnailGrid.height
            )

            if (topIndex < 0) topIndex = 0
            if (bottomIndex < 0) bottomIndex = thumbnailGrid.count - 1

            // Add margin (with epsilon to handle sub-pixel rounding during resize)
            var cols = Math.floor((thumbnailGrid.width + 1) / thumbnailGrid.cellWidth)
            if (cols < 1) cols = 1
            var marginItems = cols * thumbnailGrid.prefetchMargin
            topIndex = Math.max(0, topIndex - marginItems)
            bottomIndex = Math.min(thumbnailGrid.count - 1, bottomIndex + marginItems)

            // Log for debugging
            if (uiState && uiState.debugMode) {
                console.log("Prefetch range:", topIndex, "-", bottomIndex)
            }

            // Actually trigger prefetch
            if (uiState) {
                uiState.gridPrefetchRange(topIndex, bottomIndex)
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
            visible: thumbnailGrid.count === 0
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

    // Focus handling
    Component.onCompleted: {
        thumbnailGrid.forceActiveFocus()
        // Trigger initial prefetch after a short delay
        initialPrefetchTimer.start()
    }

    Timer {
        id: initialPrefetchTimer
        interval: 200
        repeat: false
        onTriggered: {
            if (thumbnailGrid.count > 0) {
                thumbnailGrid.triggerPrefetch()
            }
        }
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
