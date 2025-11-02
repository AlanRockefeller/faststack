import QtQuick
import QtQuick.Window
import QtQuick.Controls 2.15

ApplicationWindow {
    id: root
    width: Screen.width
    height: Screen.height
    visibility: Window.FullScreen
    title: "FastStack - " + (uiState && uiState.currentFilename ? uiState.currentFilename : "No folder loaded")

    property bool isDarkTheme: true
    property color currentBackgroundColor: isDarkTheme ? "#212121" : "white"
    property color currentTextColor: isDarkTheme ? "white" : "black"

    background: Rectangle { color: root.currentBackgroundColor }

    function toggleTheme() {
        isDarkTheme = !isDarkTheme
    }

    // Expose the Python UIState object to QML
    // This is set from Python via setContextProperty("uiState", ...)

    // Main view: either the loupe viewer or the grid
    Loader {
        id: mainViewLoader
        anchors.fill: parent
        source: "Components.qml"
    }

    // Keyboard focus and event handling

    // Status bar
    footer: Rectangle {
        id: footerRect
        implicitHeight: footerRow.implicitHeight + 10 // Add some padding
        anchors.left: parent.left
        anchors.right: parent.right
        color: "#80000000" // Semi-transparent black

        Row {
            id: footerRow
            spacing: 10
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: parent.left
            anchors.leftMargin: 10
            Label {
                text: `Image: ${uiState.currentIndex + 1} / ${uiState.imageCount}`
                color: root.currentTextColor
            }
            Label {
                text: ` | File: ${uiState.currentFilename || 'N/A'}`
                color: root.currentTextColor
            }
            Label {
                text: ` | Flag: ${uiState.isFlagged}`
                color: uiState.isFlagged ? "lightgreen" : root.currentTextColor
            }
            Label {
                text: ` | Rejected: ${uiState.isRejected}`
                color: uiState.isRejected ? "red" : root.currentTextColor
            }
            Label {
                text: ` | Stacked: ${uiState.stackedDate}`
                color: "lightgreen"
                visible: uiState.isStacked
            }
            Rectangle {
                color: uiState.stackInfoText ? "orange" : "transparent" // Brighter background
                radius: 3
                implicitWidth: stackInfoLabel.implicitWidth + 10
                implicitHeight: stackInfoLabel.implicitHeight + 5
                Label {
                    id: stackInfoLabel
                    anchors.centerIn: parent
                    text: `Stack: ${uiState.stackInfoText || 'N/A'}`
                    color: "black" // Black text for contrast on orange
                    font.bold: true
                    font.pixelSize: 16
                    onTextChanged: function() { console.log("Stack info text changed:", stackInfoLabel.text) }
                }
            }
        }
    }

    // Menu Bar
    menuBar: MenuBar {
        Menu {
            title: "&File"
            Action {
                text: "&Open Folder..."
                onTriggered: {
                    // This would trigger a file dialog in Python
                }
            }
            Action {
                text: "&Settings..."
            }

            Action {
                text: "&Exit"
                onTriggered: Qt.quit()
            }
        }
        Menu {
            title: "&View"
            Action {
                text: "Toggle Light/Dark Mode"
                onTriggered: root.toggleTheme()
            }
        }
        Menu {
            title: "&Actions"
            Action {
                text: "Run Stacks"
                onTriggered: uiState.launch_helicon()
            }
            Action {
                text: "Clear Stacks"
                onTriggered: uiState.clear_all_stacks()
            }
            Action {
                text: "Show Stacks"
                onTriggered: showStacksDialog.open()
            }
        }
        Menu {
            title: "&Help"
            Action {
                text: "&Key Bindings"
                onTriggered: aboutDialog.open()
            }
        }
    }

    Dialog {
        id: aboutDialog
        title: "Key Bindings"
        standardButtons: Dialog.Ok
        modal: true
        width: 400
        height: 400

        background: Rectangle {
            color: root.currentBackgroundColor
        }

        contentItem: Text {
            text: "<b>FastStack Keyboard and Mouse Commands</b><br><br>" +
                  "<b>Navigation:</b><br>" +
                  "&nbsp;&nbsp;J / Right Arrow: Next Image<br>" +
                  "&nbsp;&nbsp;K / Left Arrow: Previous Image<br><br>" +
                  "<b>Viewing:</b><br>" +
                  "&nbsp;&nbsp;Mouse Wheel: Zoom in/out<br>" +
                  "&nbsp;&nbsp;Left-click + Drag: Pan image<br>" +
                  "&nbsp;&nbsp;G: Toggle Grid View (not implemented)<br><br>" +
                  "<b>Rating & Stacking:</b><br>" +
                  "&nbsp;&nbsp;Space: Toggle Flag<br>" +
                  "&nbsp;&nbsp;X: Toggle Reject<br>" +
                  "&nbsp;&nbsp;S: Add to selection for Helicon<br>" +
                  "&nbsp;&nbsp;[: Begin new stack<br>" +
                  "&nbsp;&nbsp;]: End current stack<br>" +
                  "&nbsp;&nbsp;C: Clear all stacks<br><br>" +
                  "<b>Actions:</b><br>" +
                  "&nbsp;&nbsp;Enter: Launch Helicon Focus"
            padding: 10
            wrapMode: Text.WordWrap
            color: root.currentTextColor
        }
    }

    Dialog {
        id: showStacksDialog
        title: "Stack Information"
        standardButtons: Dialog.Ok
        modal: true
        width: 400
        height: 300

        background: Rectangle {
            color: root.currentBackgroundColor
        }

        contentItem: Text {
            text: uiState.get_stack_summary // Access property directly
            padding: 10
            wrapMode: Text.WordWrap
            color: root.currentTextColor
        }
    }
}
