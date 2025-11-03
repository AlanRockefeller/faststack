import QtQuick
import QtQuick.Window
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import "."

ApplicationWindow {
    id: root
    width: Screen.width
    height: Screen.height
    x: 0
    y: 0
    visibility: Window.Windowed
    flags: Qt.FramelessWindowHint
    title: "FastStack"

    Material.theme: isDarkTheme ? Material.Dark : Material.Light

    property bool isDarkTheme: uiState.get_theme() === 0
    property color currentBackgroundColor: isDarkTheme ? "#000000" : "white"
    property color currentTextColor: isDarkTheme ? "white" : "black"

    background: Rectangle { color: root.currentBackgroundColor }

    function toggleTheme() {
        uiState.set_theme(isDarkTheme ? 1 : 0) // 0 for dark, 1 for light
    }

    Connections {
        target: uiState
        function onThemeChanged() {
            root.isDarkTheme = uiState.get_theme() === 0
        }
    }

    // Expose the Python UIState object to QML
    // This is set from Python via setContextProperty("uiState", ...)

    // Main view: either the loupe viewer or the grid
    Loader {
        id: mainViewLoader
        anchors.fill: parent
        anchors.topMargin: titleBar.height
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

        RowLayout {
            id: footerRow
            spacing: 10
            Label {
                Layout.leftMargin: 10
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
                visible: uiState.isPreloading
                Layout.preferredWidth: 200
                height: 10 // give it some height
                color: "gray"
                border.color: "red"
                border.width: 1

                Rectangle {
                    color: "lightblue"
                    width: parent.width * (uiState.preloadProgress / 100)
                    height: parent.height
                }
            }
            Rectangle {
                Layout.fillWidth: true
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
                }
            }
        }
    }

    header: Rectangle {
        id: titleBar
        height: 30
        color: root.currentBackgroundColor

        MouseArea {
            anchors.fill: parent
            property point lastMousePos: Qt.point(0, 0)
            onPressed: function(mouse) {
                lastMousePos = Qt.point(mouse.x, mouse.y)
            }
            onPositionChanged: function(mouse) {
                var delta = Qt.point(mouse.x - lastMousePos.x, mouse.y - lastMousePos.y)
                root.x += delta.x
                root.y += delta.y
            }
        }

                RowLayout {

                    id: menuAndControls

                    anchors.fill: parent

        

                    MenuBar {
                        id: menuBar
                        Layout.preferredWidth: 300 // Give it some width
                        background: Rectangle {
                            color: root.currentBackgroundColor
                        }
                        palette.buttonText: root.currentTextColor
                        palette.button: root.currentBackgroundColor
                        palette.window: root.currentBackgroundColor
                        palette.text: root.currentTextColor

                        Menu {
                            title: "&File"
                            Action { text: "&Open Folder..." }
                            Action {
                                text: "&Settings..."
                                onTriggered: {
                                    settingsDialog.heliconPath = uiState.get_helicon_path()
                                    settingsDialog.cacheSize = uiState.get_cache_size()
                                    settingsDialog.prefetchRadius = uiState.get_prefetch_radius()
                                    settingsDialog.theme = uiState.get_theme()
                                    settingsDialog.defaultDirectory = uiState.get_default_directory()
                                    settingsDialog.open()
                                }
                            }
                            Action { text: "&Exit"; onTriggered: Qt.quit() }
                        }
                        Menu {
                            title: "&View"
                            Action { text: "Toggle Light/Dark Mode"; onTriggered: root.toggleTheme() }
                        }
                        Menu {
                            title: "&Actions"
                            Action { text: "Run Stacks"; onTriggered: uiState.launch_helicon() }
                            Action { text: "Clear Stacks"; onTriggered: uiState.clear_all_stacks() }
                            Action { text: "Show Stacks"; onTriggered: showStacksDialog.open() }
                            Action { text: "Preload All Images"; onTriggered: uiState.preloadAllImages() }
                        }
                        Menu {
                            title: "&Help"
                            Action { text: "&Key Bindings"; onTriggered: aboutDialog.open() }
                        }
                    }

        

                    Item { Layout.fillWidth: true } // Spacer

        

                    Row {

                        // Removed anchors

                        spacing: 10

        

                        Button {

                            text: "-"

                            onClicked: root.showMinimized()

                        }

                        Button {

                            text: "[]"

                            onClicked: root.visibility === Window.Maximized ? root.showNormal() : root.showMaximized()

                        }

                        Button {

                            text: "X"

                            onClicked: Qt.quit()

                        }

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

    SettingsDialog {
        id: settingsDialog
    }
}
