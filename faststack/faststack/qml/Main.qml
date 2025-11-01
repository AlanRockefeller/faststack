import QtQuick
import QtQuick.Window
import QtQuick.Controls 2.15

ApplicationWindow {
    id: root
    width: 1280
    height: 720
    visible: true
    title: "FastStack - " + (uiState && uiState.currentFilename ? uiState.currentFilename : "No folder loaded")

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
        Row {
            spacing: 10
            Label {
                text: `Image: ${uiState && uiState.currentIndex !== null ? uiState.currentIndex + 1 : 'N/A'} / ${uiState && uiState.imageCount !== null ? uiState.imageCount : 'N/A'}`
            }
            Label {
                text: ` | File: ${uiState && uiState.currentFilename ? uiState.currentFilename : 'N/A'}`
            }
            Label {
                text: ` | Flag: ${uiState && uiState.isFlagged ? uiState.isFlagged : 'N/A'}`
                color: uiState && uiState.isFlagged ? "lightgreen" : "white"
            }
            Label {
                text: ` | Rejected: ${uiState && uiState.isRejected ? uiState.isRejected : 'N/A'}`
                color: uiState && uiState.isRejected ? "red" : "white"
            }
            Rectangle {
                color: uiState && uiState.stackInfoText ? "#404000" : "transparent" // Dark yellow background
                radius: 3
                implicitWidth: stackInfoLabel.implicitWidth + 10
                implicitHeight: stackInfoLabel.implicitHeight + 5
                Label {
                    id: stackInfoLabel
                    anchors.centerIn: parent
                    text: `Stack: ${uiState && uiState.stackInfoText ? uiState.stackInfoText : 'N/A'}`
                    color: uiState && uiState.stackInfoText ? "yellow" : "white"
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
            title: "&Help"
            Action {
                text: "&About"
                onTriggered: aboutDialog.open()
            }
        }
    }

    Dialog {
        id: aboutDialog
        title: "About FastStack"
        standardButtons: Dialog.Ok
        modal: true
        width: 400
        height: 300

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
        }
    }
}
