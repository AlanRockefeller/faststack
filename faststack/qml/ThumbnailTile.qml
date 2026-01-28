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
    property bool tileIsInBatch: false
    property bool tileIsCurrent: false
    property string tileThumbnailSource: ""
    property var tileFolderStats: null
    property bool tileIsSelected: false
    property bool tileIsParentFolder: false

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
    property color editedColor: "#FFEB3B"    // Yellow for edited (E)
    property color restackedColor: "#FF9800" // Orange for restacked (R)
    property color batchColor: "#2196F3"     // Blue for batch (B)

    // Background
    Rectangle {
        anchors.fill: parent
        color: {
            if (tile.tileIsCurrent && !tile.tileIsFolder) {
                return Qt.rgba(currentColor.r, currentColor.g, currentColor.b, 0.25)
            } else if (tile.tileIsSelected) {
                return Qt.rgba(selectedColor.r, selectedColor.g, selectedColor.b, 0.3)
            } else if (tileMouseArea.containsMouse) {
                return hoverColor
            }
            return backgroundColor
        }
        radius: 4

        // Border - current gets gold, selected gets green
        border.color: {
            if (tile.tileIsCurrent && !tile.tileIsFolder) {
                return currentColor
            } else if (tile.tileIsSelected) {
                return selectedColor
            }
            return "transparent"
        }
        border.width: (tile.tileIsCurrent || tile.tileIsSelected) && !tile.tileIsFolder ? 3 : 0
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
                    color: tile.isDarkTheme ? "#3c3c3c" : "#e0e0e0"

                    BusyIndicator {
                        anchors.centerIn: parent
                        running: thumbnailImage.status === Image.Loading
                        width: 32
                        height: 32
                    }
                }
            }

            // Folder icon overlay (for folders without faststack.json)
            Text {
                anchors.centerIn: parent
                visible: tile.tileIsFolder && !tile.tileIsParentFolder
                text: "\uD83D\uDCC1"  // Folder emoji
                font.pixelSize: 48
                opacity: 0.8
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

            // Folder stats overlay (for folders with faststack.json)
            Rectangle {
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.right: parent.right
                height: tile.tileFolderStats && tile.tileFolderStats.total_images > 0 ? 36 : 0
                color: Qt.rgba(0, 0, 0, 0.7)
                visible: tile.tileIsFolder && tile.tileFolderStats && tile.tileFolderStats.total_images > 0

                Column {
                    anchors.centerIn: parent
                    spacing: 2

                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: tile.tileFolderStats ? tile.tileFolderStats.total_images + " images" : ""
                        font.pixelSize: 10
                        font.bold: true
                        color: "white"
                    }

                    Row {
                        anchors.horizontalCenter: parent.horizontalCenter
                        spacing: 6
                        visible: tile.tileFolderStats && (tile.tileFolderStats.stacked_count > 0 || tile.tileFolderStats.uploaded_count > 0 || tile.tileFolderStats.edited_count > 0)

                        Text {
                            visible: tile.tileFolderStats && tile.tileFolderStats.stacked_count > 0
                            text: "S:" + (tile.tileFolderStats ? tile.tileFolderStats.stacked_count : 0)
                            font.pixelSize: 9
                            color: "#FF9800"
                        }
                        Text {
                            visible: tile.tileFolderStats && tile.tileFolderStats.uploaded_count > 0
                            text: "U:" + (tile.tileFolderStats ? tile.tileFolderStats.uploaded_count : 0)
                            font.pixelSize: 9
                            color: "#4CAF50"
                        }
                        Text {
                            visible: tile.tileFolderStats && tile.tileFolderStats.edited_count > 0
                            text: "E:" + (tile.tileFolderStats ? tile.tileFolderStats.edited_count : 0)
                            font.pixelSize: 9
                            color: "#FFEB3B"
                        }
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

    // Mouse area for interactions
    MouseArea {
        id: tileMouseArea
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.LeftButton

        onClicked: function(mouse) {
            if (tile.tileIsFolder) {
                // Navigate into folder (or parent)
                uiState.gridNavigateTo(tile.tileFilePath)
            } else {
                // Handle selection or opening
                var hasShift = (mouse.modifiers & Qt.ShiftModifier)
                var hasCtrl = (mouse.modifiers & Qt.ControlModifier)

                if (hasShift || hasCtrl) {
                    // Batch selection
                    uiState.gridSelectIndex(tile.tileIndex, hasShift, hasCtrl)
                } else {
                    // Open in loupe view
                    uiState.gridOpenIndex(tile.tileIndex)
                }
            }
        }
    }
}
