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
        anchors.margins: 8

        cellWidth: gridViewRoot.cellWidth
        cellHeight: gridViewRoot.cellHeight
        clip: true

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

            // Add margin
            var cols = Math.floor(thumbnailGrid.width / thumbnailGrid.cellWidth)
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
            if (count > 0) {
                // Small delay to let the view layout
                prefetchTimer.restart()
            }
        }

        // Empty state
        Text {
            anchors.centerIn: parent
            visible: thumbnailGrid.count === 0
            text: "No images in this folder"
            color: gridViewRoot.isDarkTheme ? "#888888" : "#666666"
            font.pixelSize: 16
        }
    }

    // Keyboard shortcuts
    Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Escape) {
            // Clear selection or switch to loupe
            if (!uiState) return
            if (gridViewRoot.selectedCount > 0) {
                uiState.gridClearSelection()
            } else {
                uiState.toggleGridView()
            }
            event.accepted = true
        } else if (event.key === Qt.Key_Backspace) {
            // Navigate to parent
            if (!uiState) return
            var model = thumbnailModel
            if (model && model.rowCount() > 0) {
                // Check if first item is parent folder
                var firstEntry = model.data(model.index(0, 0), 259)  // IsFolderRole
                var isParent = model.data(model.index(0, 0), 269)    // IsParentFolderRole
                if (firstEntry && isParent) {
                    var parentPath = model.data(model.index(0, 0), 257)  // FilePathRole
                    if (parentPath) {
                        uiState.gridNavigateTo(parentPath)
                    }
                }
            }
            event.accepted = true
        }
    }

    // Focus handling
    Component.onCompleted: {
        gridViewRoot.forceActiveFocus()
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
                gridViewRoot.forceActiveFocus()
            }
        }
    }
}
