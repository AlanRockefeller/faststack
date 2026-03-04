import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Tile delegate for thumbnail grid view
Item {
    id: tile

    // Properties from model (prefixed to avoid shadowing model roles)
    property int tileIndex: 0
    property string tileFilePath: ""
    property string tileFileName: ""
    property bool tileIsFolder: false
    property bool tileIsStacked: false
    property bool tileIsUploaded: false
    property bool tileIsEdited: false
    property bool tileIsRestacked: false
    property bool tileIsFavorite: false
    property bool tileIsTodo: false
    property bool tileIsInBatch: false
    property bool tileIsCurrent: false
    property string tileThumbnailSource: ""
    property var tileFolderStats: null
    property bool tileIsSelected: false
    property bool tileIsParentFolder: false
    property bool tileHasCursor: false  // Keyboard cursor position
    property bool tileHasBackups: false
    property bool tileHasDeveloped: false

    // Theme property (bound by parent)
    property bool isDarkTheme: false

    // Configuration
    property int tileSize: 180
    property int thumbnailSize: 160
    property int textHeight: 24
    property color textColor: tile.isDarkTheme ? "white" : "black"
    property color selectedColor: "#4CAF50"
    property color currentColor: "#FFD700"  // Gold for current image
    property color hoverColor: tile.isDarkTheme ? "#404040" : "#e0e0e0"
    property color backgroundColor: tile.isDarkTheme ? "#2d2d2d" : "#fafafa"

    width: tileSize
    height: tileSize + textHeight

    // Flag colors for badges
    property color stackedColor: "#FF9800"   // Orange for stacked (S)
    property color uploadedColor: "#4CAF50"  // Green for uploaded (U)
    property color todoColor: "#2196F3"     // Blue for todo (D)
    property color editedColor: "#FFEB3B"    // Yellow for edited (E)
    property color restackedColor: "#FF9800" // Orange for restacked (R)
    property color favoriteColor: "#FFD700"  // Gold for favorite (F)
    property color batchColor: "#2196F3"     // Blue for batch (B)
    property color backupsColor: "#9C27B0"   // Purple for backups (Bk)
    property color developedColor: "#009688" // Teal for developed (D)
    property color cursorColor: "#00BFFF"    // Cyan for keyboard cursor
    property color loadingColor: tile.isDarkTheme ? "#3c3c3c" : "#e0e0e0"
    property color counterUploadedCol: "#7BBF7F"   // Muted green
    property color counterStackedCol: "#E8A64C"    // Muted orange
    property color counterEditedCol: "#E8D44C"     // Muted yellow
    property color emptyTextColor: tile.isDarkTheme ? "#888888" : "#666666"

    // Background
    Rectangle {
        anchors.fill: parent
        color: {
            if (tile.tileIsCurrent && !tile.tileIsFolder) {
                return Qt.rgba(currentColor.r, currentColor.g, currentColor.b, 0.25)
            } else if (tile.tileIsSelected) {
                return Qt.rgba(selectedColor.r, selectedColor.g, selectedColor.b, 0.3)
            } else if (tile.tileHasCursor) {
                return Qt.rgba(cursorColor.r, cursorColor.g, cursorColor.b, 0.15)
            } else if (tileMouseArea.containsMouse) {
                return hoverColor
            }
            return backgroundColor
        }
        radius: 4

        // Border - current gets gold, selected gets green, cursor gets cyan
        border.color: {
            if (tile.tileIsCurrent && !tile.tileIsFolder) {
                return currentColor
            } else if (tile.tileIsSelected) {
                return selectedColor
            } else if (tile.tileHasCursor) {
                return cursorColor
            }
            return "transparent"
        }
        border.width: (tile.tileIsCurrent || tile.tileIsSelected || tile.tileHasCursor) && !tile.tileIsFolder ? 3 : (tile.tileHasCursor && tile.tileIsFolder ? 2 : 0)
    }

    // Content column
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 4
        spacing: 2

        // Thumbnail container
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: thumbnailSize
            Layout.alignment: Qt.AlignHCenter

            // Thumbnail image
            Image {
                id: thumbnailImage
                anchors.centerIn: parent
                width: Math.min(thumbnailSize, parent.width)
                height: Math.min(thumbnailSize, parent.height)
                fillMode: Image.PreserveAspectFit
                source: tile.tileThumbnailSource
                asynchronous: true
                cache: false
                smooth: true

                // Loading placeholder
                Rectangle {
                    anchors.fill: parent
                    visible: thumbnailImage.status === Image.Loading
                    color: tile.loadingColor

                    BusyIndicator {
                        anchors.centerIn: parent
                        running: thumbnailImage.status === Image.Loading
                        width: 32
                        height: 32
                    }
                }
            }

            // Folder icon overlay (flat folder icon for dark mode)
            Text {
                anchors.centerIn: parent
                visible: tile.tileIsFolder && !tile.tileIsParentFolder
                text: "\uD83D\uDDC2"  // File cabinet / open folder emoji (cleaner look)
                font.pixelSize: 44
                opacity: 0.7
            }

            // Parent folder indicator
            Text {
                anchors.centerIn: parent
                visible: tile.tileIsParentFolder
                text: "\u2B06"  // Up arrow
                font.pixelSize: 48
                color: textColor
                opacity: 0.8
            }

            // Flag badges row (bottom-left corner of thumbnail)
            Row {
                anchors.left: parent.left
                anchors.bottom: parent.bottom
                anchors.margins: 4
                spacing: 2
                visible: !tile.tileIsFolder

                // Uploaded badge (U) - Green
                Rectangle {
                    visible: tile.tileIsUploaded
                    width: 18
                    height: 18
                    radius: 3
                    color: uploadedColor
                    Text {
                        anchors.centerIn: parent
                        text: "U"
                        font.pixelSize: 11
                        font.bold: true
                        color: "white"
                    }
                }

                // Edited badge (E) - Yellow
                Rectangle {
                    visible: tile.tileIsEdited
                    width: 18
                    height: 18
                    radius: 3
                    color: editedColor
                    Text {
                        anchors.centerIn: parent
                        text: "E"
                        font.pixelSize: 11
                        font.bold: true
                        color: "black"
                    }
                }

                // Restacked badge (R) - Orange
                Rectangle {
                    visible: tile.tileIsRestacked
                    width: 18
                    height: 18
                    radius: 3
                    color: restackedColor
                    Text {
                        anchors.centerIn: parent
                        text: "R"
                        font.pixelSize: 11
                        font.bold: true
                        color: "white"
                    }
                }

                // Todo badge (D) - Blue
                Rectangle {
                    visible: tile.tileIsTodo
                    width: 18
                    height: 18
                    radius: 3
                    color: todoColor
                    Text {
                        anchors.centerIn: parent
                        text: "D"
                        font.pixelSize: 11
                        font.bold: true
                        color: "white"
                    }
                }

                // Favorite badge (F) - Gold
                Rectangle {
                    visible: tile.tileIsFavorite
                    width: 18
                    height: 18
                    radius: 3
                    color: favoriteColor
                    Text {
                        anchors.centerIn: parent
                        text: "F"
                        font.pixelSize: 11
                        font.bold: true
                        color: "black"
                    }
                }

                // Batch badge (B) - Blue
                Rectangle {
                    visible: tile.tileIsInBatch
                    width: 18
                    height: 18
                    radius: 3
                    color: batchColor
                    Text {
                        anchors.centerIn: parent
                        text: "B"
                        font.pixelSize: 11
                        font.bold: true
                        color: "white"
                    }
                }

                // Stacked badge (S) - Orange (same as restacked but different meaning)
                Rectangle {
                    visible: tile.tileIsStacked && !tile.tileIsRestacked  // Don't show S if R is shown
                    width: 18
                    height: 18
                    radius: 3
                    color: stackedColor
                    Text {
                        anchors.centerIn: parent
                        text: "S"
                        font.pixelSize: 11
                        font.bold: true
                        color: "white"
                    }
                }
            }

            // Variant badges row (top-right corner of thumbnail)
            Row {
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 4
                spacing: 2
                visible: !tile.tileIsFolder
                layoutDirection: Qt.RightToLeft

                // Backups badge (Bk) - Purple
                Rectangle {
                    visible: tile.tileHasBackups
                    width: 18
                    height: 18
                    radius: 3
                    color: backupsColor
                    Text {
                        anchors.centerIn: parent
                        text: "Bk"
                        font.pixelSize: 9
                        font.bold: true
                        color: "white"
                    }
                }

                // Developed badge (D) - Teal
                Rectangle {
                    visible: tile.tileHasDeveloped
                    width: 18
                    height: 18
                    radius: 3
                    color: developedColor
                    Text {
                        anchors.centerIn: parent
                        text: "D"
                        font.pixelSize: 11
                        font.bold: true
                        color: "white"
                    }
                }
            }

            // ============================================================
            // TOP STATS OVERLAY: U (left), S (center), E (right)
            // Colored text: U=green, S=orange, E=yellow
            // Thin top scrim for readability
            // ============================================================
            Item {
                id: topStatsOverlay
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                height: 22
                visible: tile.tileIsFolder && tile.tileFolderStats && tile.tileFolderStats.total_images > 0

                // Thin top scrim gradient
                Rectangle {
                    anchors.fill: parent
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: Qt.rgba(0, 0, 0, 0.35) }
                        GradientStop { position: 1.0; color: Qt.rgba(0, 0, 0, 0.0) }
                    }
                }

                // Shared font for tabular numerals
                property string numFont: "Consolas, Monaco, monospace"
                property int numSize: 10
                // Muted colors for counters
                property color uploadedCol: tile.counterUploadedCol
                property color stackedCol: tile.counterStackedCol
                property color editedCol: tile.counterEditedCol
                // Letter slightly dimmer than number
                property real letterOpacity: 0.85
                property real numberOpacity: 1.0

                // U counter (top-left, always shown)
                Row {
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.leftMargin: 8
                    anchors.topMargin: 5
                    spacing: 3
                    Text {
                        text: "U"
                        font.pixelSize: topStatsOverlay.numSize
                        font.weight: Font.Medium
                        font.family: topStatsOverlay.numFont
                        color: topStatsOverlay.uploadedCol
                        opacity: topStatsOverlay.letterOpacity
                    }
                    Text {
                        text: tile.tileFolderStats ? tile.tileFolderStats.uploaded_count.toString() : "0"
                        font.pixelSize: topStatsOverlay.numSize
                        font.weight: Font.DemiBold
                        font.family: topStatsOverlay.numFont
                        color: topStatsOverlay.uploadedCol
                        opacity: topStatsOverlay.numberOpacity
                    }
                }

                // S counter (top-center, only if stacked_count > 0)
                Row {
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    anchors.topMargin: 5
                    spacing: 3
                    visible: tile.tileFolderStats && tile.tileFolderStats.stacked_count > 0
                    Text {
                        text: "S"
                        font.pixelSize: topStatsOverlay.numSize
                        font.weight: Font.Medium
                        font.family: topStatsOverlay.numFont
                        color: topStatsOverlay.stackedCol
                        opacity: topStatsOverlay.letterOpacity
                    }
                    Text {
                        text: tile.tileFolderStats ? tile.tileFolderStats.stacked_count.toString() : "0"
                        font.pixelSize: topStatsOverlay.numSize
                        font.weight: Font.DemiBold
                        font.family: topStatsOverlay.numFont
                        color: topStatsOverlay.stackedCol
                        opacity: topStatsOverlay.numberOpacity
                    }
                }

                // E counter (top-right, only if edited_count > 0)
                Row {
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.rightMargin: 8
                    anchors.topMargin: 5
                    spacing: 3
                    visible: tile.tileFolderStats && tile.tileFolderStats.edited_count > 0
                    Text {
                        text: "E"
                        font.pixelSize: topStatsOverlay.numSize
                        font.weight: Font.Medium
                        font.family: topStatsOverlay.numFont
                        color: topStatsOverlay.editedCol
                        opacity: topStatsOverlay.letterOpacity
                    }
                    Text {
                        text: tile.tileFolderStats ? tile.tileFolderStats.edited_count.toString() : "0"
                        font.pixelSize: topStatsOverlay.numSize
                        font.weight: Font.DemiBold
                        font.family: topStatsOverlay.numFont
                        color: topStatsOverlay.editedCol
                        opacity: topStatsOverlay.numberOpacity
                    }
                }
            }

            // ============================================================
            // BOTTOM OVERLAY: Sparkline + Centered file counts
            // ============================================================
            Item {
                id: bottomOverlay
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.right: parent.right
                height: 38
                visible: tile.tileIsFolder && tile.tileFolderStats && tile.tileFolderStats.total_images > 0

                // Subtle 3-stop gradient scrim (starts at ~80%)
                Rectangle {
                    anchors.fill: parent
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: Qt.rgba(0, 0, 0, 0.0) }
                        GradientStop { position: 0.4; color: Qt.rgba(0, 0, 0, 0.20) }
                        GradientStop { position: 1.0; color: Qt.rgba(0, 0, 0, 0.55) }
                    }
                }

                // Shared font for tabular numerals
                property string numFont: "Consolas, Monaco, monospace"
                property int numSize: 11

                // Coverage sparkline (triple-channel: upload green, stack orange, todo red)
                Row {
                    id: sparklineRow
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.bottom: countsRow.top
                    anchors.bottomMargin: 4
                    spacing: 1
                    visible: tile.tileFolderStats && tile.tileFolderStats.coverage_buckets && tile.tileFolderStats.coverage_buckets.length > 0

                    Repeater {
                        model: tile.tileFolderStats && tile.tileFolderStats.coverage_buckets ? tile.tileFolderStats.coverage_buckets : []

                        delegate: Column {
                            spacing: 1
                            // Upload bar (green) - top
                            Rectangle {
                                width: 3
                                height: 2
                                radius: 0.5
                                color: tile.counterUploadedCol
                                opacity: modelData[0] * 0.9 + 0.1  // 0.1 base opacity, up to 1.0
                            }
                            // Stack bar (orange) - middle
                            Rectangle {
                                width: 3
                                height: 2
                                radius: 0.5
                                color: tile.counterStackedCol
                                opacity: modelData[1] * 0.9 + 0.1  // 0.1 base opacity, up to 1.0
                            }
                            // Todo bar (red) - bottom
                            Rectangle {
                                width: 3
                                height: 2
                                radius: 0.5
                                color: "#F44336"
                                opacity: modelData[2] * 0.9 + 0.1  // 0.1 base opacity, up to 1.0
                            }
                        }
                    }
                }

                // File counts: "{jpg_count} JPG · {raw_count} RAW" (centered, always show both)
                Row {
                    id: countsRow
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 6
                    spacing: 0

                    Text {
                        text: tile.tileFolderStats ? tile.tileFolderStats.jpg_count.toString() : "0"
                        font.pixelSize: bottomOverlay.numSize
                        font.weight: Font.DemiBold
                        font.family: bottomOverlay.numFont
                        color: "#FFFFFF"
                    }
                    Text {
                        text: " IMG"
                        font.pixelSize: bottomOverlay.numSize
                        font.weight: Font.Medium
                        font.family: bottomOverlay.numFont
                        color: Qt.rgba(1, 1, 1, 0.85)
                    }
                    Text {
                        text: " · "
                        font.pixelSize: bottomOverlay.numSize
                        font.weight: Font.Medium
                        color: Qt.rgba(1, 1, 1, 0.5)
                    }
                    Text {
                        text: tile.tileFolderStats ? tile.tileFolderStats.raw_count.toString() : "0"
                        font.pixelSize: bottomOverlay.numSize
                        font.weight: Font.DemiBold
                        font.family: bottomOverlay.numFont
                        color: "#FFFFFF"
                    }
                    Text {
                        text: " RAW"
                        font.pixelSize: bottomOverlay.numSize
                        font.weight: Font.Medium
                        font.family: bottomOverlay.numFont
                        color: Qt.rgba(1, 1, 1, 0.85)
                    }
                }
            }
        }

        // Filename text
        Text {
            Layout.fillWidth: true
            Layout.preferredHeight: textHeight
            text: tile.tileIsParentFolder ? "(Parent Folder)" : tile.tileFileName
            color: textColor
            font.pixelSize: 11
            elide: Text.ElideMiddle
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }

    Component.onCompleted: {
        // Use robust check for uiState which might not be defined in all contexts
        var hasUiState = (typeof uiState !== 'undefined' && uiState !== null);
        if (tile.tileIndex === 0 && hasUiState && uiState.debugThumbTiming)
            console.log("[THUMB-TIMING] first delegate created (index 0) t=" + Date.now() + "ms")
    }

    // Mouse area for interactions
    MouseArea {
        id: tileMouseArea
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.LeftButton | Qt.RightButton

        onClicked: function(mouse) {
            if (tile.tileIsFolder) {
                // Navigate into folder (or parent)
                uiState.gridNavigateTo(tile.tileFilePath)
            } else {
                // Handle selection or opening
                var hasShift = (mouse.modifiers & Qt.ShiftModifier)
                var hasCtrl = (mouse.modifiers & Qt.ControlModifier)
                var isRightClick = (mouse.button === Qt.RightButton)

                if (isRightClick) {
                    // Right-click: toggle selection (as per help text)
                    uiState.gridSelectIndex(tile.tileIndex, false, true)
                } else if (hasShift || hasCtrl) {
                    // Shift: range select, Ctrl: add to selection
                    uiState.gridSelectIndex(tile.tileIndex, hasShift, hasCtrl)
                } else {
                    // Left-click without modifiers: open in loupe view
                    uiState.gridOpenIndex(tile.tileIndex)
                }
            }
        }
    }
}
