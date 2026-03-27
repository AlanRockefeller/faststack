import QtQuick
import QtQuick.Window
import QtQuick.Dialogs
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import "."

ApplicationWindow {
    id: root
    visible: true
    width: 1200
    height: 800
    minimumWidth: 800
    minimumHeight: 500
    flags: Qt.FramelessWindowHint | Qt.Window | Qt.WindowMinMaxButtonsHint
    title: "FastStack"

    property bool allowCloseWithRecycleBins: false
    property bool fullScreenLoupe: false
    property var savedWindowGeometry: ({})

    function enterFullScreenLoupe() {
        if (!uiState || uiState.isGridViewActive) return

        savedWindowGeometry = {
            x: root.x,
            y: root.y,
            width: root.width,
            height: root.height,
            visibility: root.visibility
        }

        fullScreenLoupe = true
        root.showFullScreen()
    }

    function exitFullScreenLoupe() {
        if (!fullScreenLoupe) return

        fullScreenLoupe = false
        
        if (savedWindowGeometry.visibility === Window.Maximized) {
            root.showMaximized()
        } else {
            root.showNormal()
            if (savedWindowGeometry.visibility === Window.Windowed) {
                root.x = savedWindowGeometry.x
                root.y = savedWindowGeometry.y
                root.width = savedWindowGeometry.width
                root.height = savedWindowGeometry.height
            }
        }
        root.requestActivate()
    }

    function toggleFullScreenLoupe() {
        if (fullScreenLoupe) {
            exitFullScreenLoupe()
        } else {
            enterFullScreenLoupe()
        }
    }

    onClosing: function(close) {
        if (allowCloseWithRecycleBins) {
            close.accepted = true
            return
        }
        if (uiState && uiState.hasRecycleBinItems) {
            close.accepted = false
            uiState.refreshRecycleBinStats()
            recycleBinCleanupDialog.open()
        } else {
            close.accepted = true
        }
    }

    Component.onCompleted: {
        // Initialization complete
    }

    Material.theme: (uiState && uiState.theme === 0) ? Material.Dark : Material.Light
    Material.accent: "#4fb360"

    // Frameless windows on Windows report FullScreen instead of Maximized
    // after showMaximized(). Treat both as "maximized" unless we are in the
    // app's own fullscreen loupe mode (which is the real FullScreen).
    property bool isMaximized: root.visibility === Window.Maximized
                               || (root.visibility === Window.FullScreen
                                   && !root.fullScreenLoupe)
    property bool isDarkTheme: uiState ? uiState.theme === 0 : true
    property color currentBackgroundColor: isDarkTheme ? "#000000" : "#ffffff"
    property color currentTextColor: isDarkTheme ? "white" : "black"
    property color hoverColor: isDarkTheme ? Qt.lighter(currentBackgroundColor, 1.5) : Qt.darker(currentBackgroundColor, 1.1)


    background: Rectangle { color: root.currentBackgroundColor }

    function toggleTheme() {
        if (uiState) {
            uiState.theme = (uiState.theme === 0 ? 1 : 0)
        }
    }

    function openExifDialog(data) {
        exifDialog.summaryData = data.summary
        exifDialog.fullData = data.full
        exifDialog.open()
    }


    // -------- CUSTOM TITLE BAR --------
    property int titleBarHeight: 36

    Rectangle {
        id: customTitleBar
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: root.titleBarHeight
        color: root.isDarkTheme ? "#1a1a1a" : "#f5f5f5"
        z: 200
        visible: !root.fullScreenLoupe

        // Subtle bottom separator
        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            height: 1
            color: root.isDarkTheme ? "#333333" : "#dddddd"
        }

        // Menu-active flag: hovered over title bar or any menu is open
        property bool titleBarHovered: titleBarHoverArea.containsMouse
        property bool anyMenuOpen: fileMenu.visible || viewMenu.visible
                                   || actionsMenu.visible || helpMenu.visible
        property bool menuActive: titleBarHovered
                                  || fileMouseArea.containsMouse
                                  || viewMouseArea.containsMouse
                                  || actionsMouseArea.containsMouse
                                  || helpMouseArea.containsMouse
                                  || anyMenuOpen

        // Hover detection for the entire title bar
        MouseArea {
            id: titleBarHoverArea
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.NoButton
        }

        // -- Left: App title --
        Text {
            id: appTitleLabel
            anchors.left: parent.left
            anchors.leftMargin: 12
            anchors.verticalCenter: parent.verticalCenter
            text: "FastStack"
            color: root.currentTextColor
            font.pixelSize: 13
            font.weight: Font.DemiBold
            font.family: "Segoe UI Variable"
        }

        // -- Left-center: hover-revealed menu buttons --
        Row {
            id: menuButtonRow
            anchors.left: appTitleLabel.right
            anchors.leftMargin: 16
            anchors.verticalCenter: parent.verticalCenter
            spacing: 2
            opacity: customTitleBar.menuActive ? 1.0 : 0.0
            visible: opacity > 0

            Behavior on opacity {
                NumberAnimation { duration: 150 }
            }

            // FILE MENU BUTTON
            Rectangle {
                id: fileBtn
                width: fileLabel.width + 16
                height: 26
                color: fileMouseArea.containsMouse ? root.hoverColor : "transparent"
                radius: 4

                Text {
                    id: fileLabel
                    anchors.centerIn: parent
                    text: "File"
                    color: root.currentTextColor
                    font.pixelSize: 12
                    font.family: "Segoe UI Variable"
                }

                MouseArea {
                    id: fileMouseArea
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: {
                        var pos = fileBtn.mapToItem(null, 0, fileBtn.height)
                        fileMenu.popup(pos.x, pos.y)
                    }
                }
            }

            // VIEW MENU BUTTON
            Rectangle {
                id: viewBtn
                width: viewLabel.width + 16
                height: 26
                color: viewMouseArea.containsMouse ? root.hoverColor : "transparent"
                radius: 4

                Text {
                    id: viewLabel
                    anchors.centerIn: parent
                    text: "View"
                    color: root.currentTextColor
                    font.pixelSize: 12
                    font.family: "Segoe UI Variable"
                }

                MouseArea {
                    id: viewMouseArea
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: {
                        var pos = viewBtn.mapToItem(null, 0, viewBtn.height)
                        viewMenu.popup(pos.x, pos.y)
                    }
                }
            }

            // ACTIONS MENU BUTTON
            Rectangle {
                id: actionsBtn
                width: actionsLabel.width + 16
                height: 26
                color: actionsMouseArea.containsMouse ? root.hoverColor : "transparent"
                radius: 4

                Text {
                    id: actionsLabel
                    anchors.centerIn: parent
                    text: "Actions"
                    color: root.currentTextColor
                    font.pixelSize: 12
                    font.family: "Segoe UI Variable"
                }

                MouseArea {
                    id: actionsMouseArea
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: {
                        var pos = actionsBtn.mapToItem(null, 0, actionsBtn.height)
                        actionsMenu.popup(pos.x, pos.y)
                    }
                }
            }

            // HELP MENU BUTTON
            Rectangle {
                id: helpBtn
                width: helpLabel.width + 16
                height: 26
                color: helpMouseArea.containsMouse ? root.hoverColor : "transparent"
                radius: 4

                Text {
                    id: helpLabel
                    anchors.centerIn: parent
                    text: "Help"
                    color: root.currentTextColor
                    font.pixelSize: 12
                    font.family: "Segoe UI Variable"
                }

                MouseArea {
                    id: helpMouseArea
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: {
                        var pos = helpBtn.mapToItem(null, 0, helpBtn.height)
                        helpMenu.popup(pos.x, pos.y)
                    }
                }
            }
        }

        // -- Center: draggable space --
        // Uses separate TapHandler (double-click) and DragHandler (window move)
        // so that startSystemMove() never interferes with double-click recognition.
        Item {
            id: titleBarDragArea
            anchors.left: menuButtonRow.right
            anchors.leftMargin: 8
            anchors.right: zoomLabel.left
            anchors.rightMargin: 8
            anchors.top: parent.top
            anchors.bottom: parent.bottom

            TapHandler {
                onDoubleTapped: {
                    if (uiState && uiState.debugMode)
                        console.log("[TitleBar] double-tap: visibility =", root.visibility,
                                    "isMaximized =", root.isMaximized,
                                    "fullScreenLoupe =", root.fullScreenLoupe)
                    if (root.isMaximized) {
                        root.showNormal()
                    } else {
                        root.showMaximized()
                    }
                    if (uiState && uiState.debugMode)
                        console.log("[TitleBar] double-tap: after visibility =", root.visibility,
                                    "isMaximized =", root.isMaximized)
                }
            }

            DragHandler {
                id: titleBarDragHandler
                target: null  // we move the window, not this item
                onActiveChanged: {
                    if (active) {
                        if (uiState && uiState.debugMode)
                            console.log("[TitleBar] drag-start: starting system move")
                        root.startSystemMove()
                    }
                }
            }
        }

        // -- Right-center: subtle zoom label --
        Text {
            id: zoomLabel
            anchors.right: windowControls.left
            anchors.rightMargin: 16
            anchors.verticalCenter: parent.verticalCenter
            color: root.isDarkTheme ? "#777777" : "#999999"
            font.pixelSize: 11
            font.family: "Segoe UI Variable"
            visible: text !== ""

            property var loupe: mainViewLoader.item
            property real zs: loupe ? loupe.currentZoomScale : 0
            property real fs: loupe ? loupe.currentFitScale : 0

            text: {
                if (!loupe || fs <= 0 || zs <= 0) return ""
                if (uiState && uiState.isGridViewActive) return ""
                var ratio = zs / fs
                if (Math.abs(ratio - 1.0) < 0.03) return "Zoom: Fit to window (" + Math.round(zs * 100) + "%)"
                return "Zoom: " + Math.round(ratio * 100) + "%"
            }
        }

        // -- Far right: window controls --
        Row {
            id: windowControls
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom

            // Minimize
            Rectangle {
                width: 46
                height: parent.height
                color: minimizeArea.containsMouse
                       ? (root.isDarkTheme ? "#333333" : "#e0e0e0") : "transparent"

                Text {
                    anchors.centerIn: parent
                    text: "\u2013"  // en-dash as minimize icon
                    color: root.currentTextColor
                    font.pixelSize: 14
                    font.family: "Segoe UI Variable"
                }

                MouseArea {
                    id: minimizeArea
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: root.showMinimized()
                }
            }

            // Maximize / Restore
            Rectangle {
                width: 46
                height: parent.height
                color: maximizeArea.containsMouse
                       ? (root.isDarkTheme ? "#333333" : "#e0e0e0") : "transparent"

                Text {
                    anchors.centerIn: parent
                    text: root.isMaximized ? "\u2752" : "\u25A1"
                    color: root.currentTextColor
                    font.pixelSize: root.isMaximized ? 12 : 14
                    font.family: "Segoe UI Variable"
                }

                MouseArea {
                    id: maximizeArea
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: {
                        if (root.isMaximized)
                            root.showNormal()
                        else
                            root.showMaximized()
                    }
                }
            }

            // Close
            Rectangle {
                width: 46
                height: parent.height
                color: closeArea.containsMouse ? "#c42b1c" : "transparent"

                Text {
                    anchors.centerIn: parent
                    text: "\u2715"
                    color: closeArea.containsMouse ? "white" : root.currentTextColor
                    font.pixelSize: 13
                    font.family: "Segoe UI Variable"
                }

                MouseArea {
                    id: closeArea
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: root.close()
                }
            }
        }
    }

    // -------- RESIZE HANDLES (frameless window) --------
    // Only active when not maximized/fullscreen
    property int resizeMargin: 5
    property bool resizeHandlesEnabled: root.visibility === Window.Windowed

    // Top edge
    MouseArea {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: root.resizeMargin
        cursorShape: Qt.SizeVerCursor
        visible: root.resizeHandlesEnabled
        z: 300
        onPressed: root.startSystemResize(Qt.TopEdge)
    }
    // Bottom edge
    MouseArea {
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: root.resizeMargin
        cursorShape: Qt.SizeVerCursor
        visible: root.resizeHandlesEnabled
        z: 300
        onPressed: root.startSystemResize(Qt.BottomEdge)
    }
    // Left edge
    MouseArea {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: root.resizeMargin
        cursorShape: Qt.SizeHorCursor
        visible: root.resizeHandlesEnabled
        z: 300
        onPressed: root.startSystemResize(Qt.LeftEdge)
    }
    // Right edge
    MouseArea {
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: root.resizeMargin
        cursorShape: Qt.SizeHorCursor
        visible: root.resizeHandlesEnabled
        z: 300
        onPressed: root.startSystemResize(Qt.RightEdge)
    }
    // Top-left corner
    MouseArea {
        anchors.top: parent.top
        anchors.left: parent.left
        width: root.resizeMargin * 2
        height: root.resizeMargin * 2
        cursorShape: Qt.SizeFDiagCursor
        visible: root.resizeHandlesEnabled
        z: 301
        onPressed: root.startSystemResize(Qt.TopEdge | Qt.LeftEdge)
    }
    // Top-right corner
    MouseArea {
        anchors.top: parent.top
        anchors.right: parent.right
        width: root.resizeMargin * 2
        height: root.resizeMargin * 2
        cursorShape: Qt.SizeBDiagCursor
        visible: root.resizeHandlesEnabled
        z: 301
        onPressed: root.startSystemResize(Qt.TopEdge | Qt.RightEdge)
    }
    // Bottom-left corner
    MouseArea {
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        width: root.resizeMargin * 2
        height: root.resizeMargin * 2
        cursorShape: Qt.SizeBDiagCursor
        visible: root.resizeHandlesEnabled
        z: 301
        onPressed: root.startSystemResize(Qt.BottomEdge | Qt.LeftEdge)
    }
    // Bottom-right corner
    MouseArea {
        anchors.bottom: parent.bottom
        anchors.right: parent.right
        width: root.resizeMargin * 2
        height: root.resizeMargin * 2
        cursorShape: Qt.SizeFDiagCursor
        visible: root.resizeHandlesEnabled
        z: 301
        onPressed: root.startSystemResize(Qt.BottomEdge | Qt.RightEdge)
    }

    // -------- MENU POPUPS --------
    Menu {
        id: fileMenu
        parent: Overlay.overlay
        implicitWidth: 200

        background: Rectangle {
            implicitWidth: 200
            implicitHeight: fileMenuColumn.implicitHeight
            color: root.currentBackgroundColor
            border.color: root.isDarkTheme ? "#666666" : "#cccccc"
            radius: 4
        }

        contentItem: Column {
            id: fileMenuColumn

            ItemDelegate {
                width: 200
                height: 36
                text: "Open Folder..."
                onClicked: {
                    if (uiState) {
                        uiState.open_folder()
                    }
                    fileMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? hoverColor : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 200
                height: 36
                text: "Settings..."
                onClicked: {
                    settingsDialog.open()
                    fileMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            Rectangle {
                width: 200
                height: 1
                color: root.isDarkTheme ? "#666666" : "#cccccc"
            }
            ItemDelegate {
                width: 200
                height: 36
                text: "Exit"
                onClicked: Qt.quit()
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
        }
    }

    Menu {
        id: viewMenu
        parent: Overlay.overlay
        implicitWidth: 220

        background: Rectangle {
            implicitWidth: 220
            implicitHeight: viewMenuColumn.implicitHeight
            color: root.currentBackgroundColor
            border.color: root.isDarkTheme ? "#666666" : "#cccccc"
            radius: 4
        }

        contentItem: Column {
            id: viewMenuColumn

            // Toggle theme
            ItemDelegate {
                width: 220
                height: 36
                text: "Toggle Light/Dark Mode"
                onClicked: {
                    root.toggleTheme()
                    viewMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }

            // Separator
            Rectangle {
                width: 220
                height: 1
                color: root.isDarkTheme ? "#666666" : "#cccccc"
            }

            // Color: None (Original)
            ItemDelegate {
                width: 220
                height: 36
                text: "Color: None (Original)"
                onClicked: {
                    if (controller) controller.set_color_mode("none")
                    viewMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0")
                                          : ((uiState && uiState.colorMode === "none")
                                             ? (root.isDarkTheme ? "#505050" : "#d0ffd0")
                                             : "transparent")
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    font.bold: uiState && uiState.colorMode === "none"
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }

            // Color: Saturation Compensation
            ItemDelegate {
                width: 220
                height: 36
                text: "Color: Saturation Compensation"
                onClicked: {
                    if (controller) controller.set_color_mode("saturation")
                    viewMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0")
                                          : ((uiState && uiState.colorMode === "saturation")
                                             ? (root.isDarkTheme ? "#505050" : "#d0ffd0")
                                             : "transparent")
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    font.bold: uiState && uiState.colorMode === "saturation"
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }

            // Color: Full ICC Profile
            ItemDelegate {
                width: 220
                height: 36
                text: "Color: Full ICC Profile"
                onClicked: {
                    if (controller) controller.set_color_mode("icc")
                    viewMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0")
                                          : ((uiState && uiState.colorMode === "icc")
                                             ? (root.isDarkTheme ? "#505050" : "#d0ffd0")
                                             : "transparent")
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    font.bold: uiState && uiState.colorMode === "icc"
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
        }
    }

    Menu {
        id: actionsMenu
        parent: Overlay.overlay
        implicitWidth: 220

        background: Rectangle {
            implicitWidth: 220
            implicitHeight: actionsMenuColumn.implicitHeight
            color: root.currentBackgroundColor
            border.color: root.isDarkTheme ? "#666666" : "#cccccc"
            radius: 4
        }

        contentItem: Column {
            id: actionsMenuColumn

            // Develop RAW (True Headroom)
            ItemDelegate {
                width: 220
                height: 36
                text: (uiState && uiState.hasWorkingTif) ? "Re-develop RAW" : "Develop RAW"
                enabled: uiState ? uiState.hasRaw : false
                onClicked: {
                    if (uiState) uiState.developRaw()
                    actionsMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: enabled ? root.currentTextColor : (root.isDarkTheme ? "#666666" : "#999999")
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }

            // Edit Image (from old Main.qml)
            ItemDelegate {
                width: 220
                height: 36
                text: "Edit Image"
                onClicked: {
                    if (uiState) {
                        uiState.isEditorOpen = !uiState.isEditorOpen
                        if (uiState.isEditorOpen && controller) {
                            controller.load_image_for_editing()
                        }
                    }
                    actionsMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 220
                height: 36
                text: "Crop Image"
                onClicked: {
                    if (controller) {
                        controller.toggle_crop_mode()
                    }
                    actionsMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }

            ItemDelegate {
                width: 220
                height: 36
                text: "Run Stacks (raw)"
                onClicked: { if (uiState) uiState.launch_helicon(true); actionsMenu.close() }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 220
                height: 36
                text: "Run Stacks (jpg)"
                onClicked: { if (uiState) uiState.launch_helicon(false); actionsMenu.close() }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 220
                height: 36
                text: "Clear Stacks"
                onClicked: { if (uiState) uiState.clear_all_stacks(); actionsMenu.close() }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 220
                height: 36
                text: "Show Stacks"
                onClicked: { showStacksDialog.open(); actionsMenu.close() }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 220
                height: 36
                text: "Preload All Images"
                onClicked: { if (uiState) uiState.preloadAllImages(); actionsMenu.close() }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 220
                height: 36
                text: "Filter Images..."
                onClicked: { filterDialog.open(); actionsMenu.close() }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }

            // Clear Filename Filter (from old Main.qml)
            ItemDelegate {
                width: 220
                height: 36
                text: "Clear Filename Filter"
                onClicked: {
                    if (controller) controller.clear_filter()
                    actionsMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 220
                height: 36
                text: "Add Favorites to Batch"
                onClicked: {
                    if (uiState) uiState.addFavoritesToBatch()
                    actionsMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 220
                height: 36
                text: "Add Uploaded to Batch"
                onClicked: {
                    if (uiState) uiState.addUploadedToBatch()
                    actionsMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 220
                height: 36
                text: "Jump to Last Uploaded"
                onClicked: {
                    if (uiState) uiState.jumpToLastUploaded()
                    actionsMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 220
                height: 36
                text: "Auto-Level Batch"
                onClicked: {
                    if (uiState) uiState.batchAutoLevels()
                    actionsMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
            ItemDelegate {
                width: 220
                height: 36
                text: "Stack Source RAWs"
                enabled: uiState ? uiState.isStackedJpg : false
                onClicked: {
                    if (uiState) uiState.stack_source_raws();
                    actionsMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }

            // Separator before grid view toggle
            Rectangle {
                width: 220
                height: 1
                color: root.isDarkTheme ? "#666666" : "#cccccc"
            }

            // Toggle Grid/Loupe View
            ItemDelegate {
                width: 220
                height: 36
                text: uiState && uiState.isGridViewActive ? "Single Image View" : "Thumbnail View"
                onClicked: {
                    if (uiState) uiState.toggleGridView();
                    actionsMenu.close()
                }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
        }
    }

    Menu {
        id: helpMenu
        parent: Overlay.overlay
        implicitWidth: 200

        background: Rectangle {
            implicitWidth: 200
            implicitHeight: helpMenuColumn.implicitHeight
            color: root.currentBackgroundColor
            border.color: root.isDarkTheme ? "#666666" : "#cccccc"
            radius: 4
        }

        contentItem: Column {
            id: helpMenuColumn

            ItemDelegate {
                width: 200
                height: 36
                text: "Key Bindings"
                onClicked: { aboutDialog.open(); helpMenu.close() }
                background: Rectangle {
                    color: parent.hovered ? (root.isDarkTheme ? "#555555" : "#e0e0e0") : "transparent"
                }
                contentItem: Text {
                    text: parent.text
                    color: root.currentTextColor
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 10
                }
            }
        }
    }

    property int footerHeight: 60
    property int effectiveFooterHeight: fullScreenLoupe ? 0 : footerHeight

    Shortcut {
        sequence: "F11"
        context: Qt.ApplicationShortcut
        enabled: uiState ? !uiState.isGridViewActive && !uiState.isDialogOpen : false
        onActivated: root.toggleFullScreenLoupe()
    }

    Shortcut {
        sequence: "Escape"
        context: Qt.ApplicationShortcut
        enabled: root.fullScreenLoupe
        onActivated: root.exitFullScreenLoupe()
    }

    Shortcut {
        sequence: "E"
        context: Qt.ApplicationShortcut
        enabled: uiState ? !uiState.isDialogOpen : true
        onActivated: {
            if (!uiState) return

            if (uiState.isEditorOpen) {
                uiState.isEditorOpen = false
            } else {
                uiState.isEditorOpen = true
                if (controller) {
                    controller.load_image_for_editing()
                }
            }
        }
    }

    // Grid View Toggle (T for Thumbnails)
    Shortcut {
        sequence: "T"
        context: Qt.ApplicationShortcut
        enabled: uiState ? !uiState.isDialogOpen : true
        onActivated: {
            if (uiState) uiState.toggleGridView()
        }
    }

    // Handle View Switching and Prefetch Gating
    Connections {
        target: uiState
        function onIsGridViewActiveChanged() {
            if (uiState.isGridViewActive && root.fullScreenLoupe) {
                root.exitFullScreenLoupe()
            }

            var gridItem = gridViewLoader.item
            if (!gridItem) return

            if (uiState.isGridViewActive) {
                // Switching TO grid:
                // 1. Immediately disable prefetch to block transient top-of-list requests
                //    that happen before the view layout/scroll is restored.
                if (typeof gridItem.setPrefetchEnabled === "function") {
                    gridItem.setPrefetchEnabled(false)
                }

                // 2. Re-enable on next event loop tick.
                //    This allows the GridView to restore its currentIndex/contentY position.
                Qt.callLater(function() {
                    var it = gridViewLoader.item
                    if (uiState.isGridViewActive && it && typeof it.setPrefetchEnabled === "function") {
                        it.setPrefetchEnabled(true)
                    }
                })
            } else {
                // Switching AWAY from grid:
                // Disable immediately to stop background work.
                if (typeof gridItem.setPrefetchEnabled === "function") {
                    gridItem.setPrefetchEnabled(false)
                }
            }
        }
    }

    // -------- MAIN VIEW --------
    // StackLayout to switch between loupe and grid view
    StackLayout {
        id: contentArea
        anchors.top: customTitleBar.visible ? customTitleBar.bottom : parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        currentIndex: uiState && uiState.isGridViewActive ? 1 : 0

        // Index 0: Loupe View (single image)
        Item {
            id: loupeViewContainer
            Layout.fillWidth: true
            Layout.fillHeight: true

            Loader {
                id: mainViewLoader
                anchors.fill: parent
                source: "Components.qml"
                focus: !uiState || !uiState.isGridViewActive
                onLoaded: item.footerHeight = Qt.binding(function() { return root.effectiveFooterHeight })

                // Key bindings implemented in old Main.qml
                Keys.onPressed: function(event) {
                    if (!uiState || !controller) {
                        return
                    }

                    // Global Key for saving edited image (Ctrl+S) when editor is open
                    if (event.key === Qt.Key_S && (event.modifiers & Qt.ControlModifier)) {
                        if (uiState.isEditorOpen) {
                            controller.save_edited_image()
                            event.accepted = true
                        }
                    }
                }
            }
        }

        // Index 1: Grid View (thumbnail browser)
        Item {
            id: gridViewContainer
            Layout.fillWidth: true
            Layout.fillHeight: true

            Loader {
                id: gridViewLoader
                anchors.fill: parent
                source: "ThumbnailGridView.qml"
                active: true  // Keep loaded to preserve state during view toggle
                visible: uiState && uiState.isGridViewActive
                focus: uiState && uiState.isGridViewActive

                onLoaded: {
                    // Enable prefetch on startup if grid is active (single owner)
                    var loadedItem = item
                    if (uiState && uiState.isGridViewActive && loadedItem && typeof loadedItem.setPrefetchEnabled === "function") {
                        // Delay to match the toggle behavior (allow layout to settle)
                        Qt.callLater(function() {
                            if (gridViewLoader.item === loadedItem && uiState.isGridViewActive) {
                                loadedItem.setPrefetchEnabled(true)
                            }
                        })
                    }
                }

                // Bind theme property to loaded item
                Binding {
                    target: gridViewLoader.item
                    property: "isDarkTheme"
                    value: root.isDarkTheme
                    when: gridViewLoader.item
                }
            }
        }
    }

    // -------- STATUS BAR OVERLAY --------
    Rectangle {
        z: 100
        anchors.bottom: parent.bottom
        id: footerRect
        // Keep footer height fixed so the main image area doesn't change size when
        // stack/batch labels appear or disappear (prevents cache invalidations).
        height: root.effectiveFooterHeight
        implicitHeight: root.effectiveFooterHeight
        anchors.left: parent.left
        anchors.right: parent.right
        color: Qt.rgba(root.currentBackgroundColor.r, root.currentBackgroundColor.g, root.currentBackgroundColor.b, 0.8)
        clip: true
        visible: !root.fullScreenLoupe

        RowLayout {
            id: footerRow
            spacing: 10
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: parent.left
            anchors.right: parent.right

            Label {
                Layout.leftMargin: 10
                text: uiState ? `Image: ${uiState.currentIndex + 1} / ${uiState.imageCount}` : "Image: - / -"
                color: root.currentTextColor
            }
            Label {
                text: (uiState && uiState.imageCount > 0)
                      ? (uiState.currentFilename || "N/A")
                      : "N/A"
                color: root.currentTextColor
            }
            Label {
                visible: (uiState && uiState.imageCount > 0 && uiState.exifBrief && uiState.exifBrief.length > 0)
                text: uiState ? (uiState.exifBrief || "") : ""
                color: root.currentTextColor
            }
            Label {
                id: directoryPathLabel
                visible: uiState && uiState.currentDirectory !== ""
                text: uiState ? uiState.currentDirectory : ""
                color: root.isDarkTheme ? "#888888" : "#777777"
                font.pixelSize: 11
                elide: Text.ElideMiddle
                Layout.maximumWidth: 300

                ToolTip.visible: directoryPathMouse.containsMouse && text !== ""
                ToolTip.text: uiState ? uiState.currentDirectory : ""
                ToolTip.delay: 500

                MouseArea {
                    id: directoryPathMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    acceptedButtons: Qt.NoButton
                }
            }
            Item { Layout.fillWidth: true }
            Label {
                text: uiState ? ` Stacked: ${uiState.stackedDate}` : ""
                color: "lightgreen"
                visible: uiState ? (uiState.imageCount > 0 && uiState.isStacked) : false
            }
            Label {
                text: uiState ? ` Uploaded on ${uiState.uploadedDate}` : ""
                color: "lightgreen"
                visible: uiState ? (uiState.imageCount > 0 && uiState.isUploaded) : false
            }
            Label {
                text: uiState ? (uiState.todoDate ? ` Todo since ${uiState.todoDate}` : " Todo") : ""
                color: "#64B5F6"
                visible: uiState ? (uiState.imageCount > 0 && uiState.isTodo) : false
            }
            Label {
                text: uiState ? ` Edited on ${uiState.editedDate}` : ""
                color: "lightgreen"
                visible: uiState ? (uiState.imageCount > 0 && uiState.isEdited) : false
            }
            Label {
                text: uiState ? ` Restacked on ${uiState.restackedDate}` : ""
                color: "cyan"
                visible: uiState ? (uiState.imageCount > 0 && uiState.isRestacked) : false
            }
            Label {
                text: " Favorite"
                color: "gold"
                visible: uiState ? (uiState.imageCount > 0 && uiState.isFavorite) : false
            }
            Label {
                text: uiState ? ` Filter: "${uiState.filterString}"` : ""
                color: "yellow"
                font.bold: true
                visible: uiState ? (uiState.filterString !== "") : false
            }
            Rectangle {
                visible: uiState ? uiState.isPreloading : false
                Layout.preferredWidth: 200
                height: 10 // give it some height
                color: "gray"
                border.color: "red"
                border.width: 1

                Rectangle {
                    color: "lightblue"
                    width: parent.width * (uiState ? uiState.preloadProgress / 100 : 0)
                    height: parent.height
                }
            }
            Rectangle {
                color: (uiState && uiState.imageCount > 0 && uiState.stackInfoText) ? "orange" : "transparent"
                radius: 3
                implicitWidth: stackInfoLabel.implicitWidth + 10
                implicitHeight: stackInfoLabel.implicitHeight + 5
                visible: uiState ? (uiState.imageCount > 0 && uiState.stackInfoText) : false

                Label {
                    id: stackInfoLabel
                    anchors.centerIn: parent
                    text: uiState ? `Stack: ${uiState.stackInfoText}` : ""
                    color: "black"
                    font.bold: true
                    font.pixelSize: 16
                }
            }
            Rectangle {
                color: (uiState && uiState.imageCount > 0 && uiState.batchInfoText) ? "#4fb360" : "transparent"
                radius: 3
                implicitWidth: batchInfoLabel.implicitWidth + 10
                implicitHeight: batchInfoLabel.implicitHeight + 5
                visible: uiState ? (uiState.imageCount > 0 && uiState.batchInfoText) : false

                Label {
                    id: batchInfoLabel
                    anchors.centerIn: parent
                    text: uiState ? `Batch: ${uiState.batchInfoText}` : ""
                    color: "white"
                    font.bold: true
                    font.pixelSize: 16
                }
            }
            // Variant badges (loupe view only, when multiple variants exist)
            Row {
                spacing: 4
                visible: uiState && !uiState.isGridViewActive && uiState.variantBadges.length > 1

                Repeater {
                    model: uiState ? uiState.variantBadges : []

                    delegate: Rectangle {
                        width: badgeLabel.implicitWidth + 12
                        height: 22
                        radius: 3
                        color: modelData.active ? "white" : "#555"
                        border.color: modelData.active ? "#333" : "transparent"
                        border.width: modelData.active ? 1 : 0

                        Text {
                            id: badgeLabel
                            anchors.centerIn: parent
                            text: modelData.label
                            font.pixelSize: 11
                            font.bold: true
                            color: modelData.active ? "black" : "white"
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                if (uiState) uiState.setVariantOverride(modelData.path)
                            }
                        }
                    }
                }

                Label {
                    text: uiState ? uiState.variantSaveHint : ""
                    color: root.isDarkTheme ? "#aaa" : "#666"
                    font.pixelSize: 11
                    font.italic: true
                    visible: text !== ""
                    anchors.verticalCenter: parent.verticalCenter
                }
            }

            Rectangle {
                Layout.fillWidth: true
                color: "transparent"
            }

            Label {
                text: uiState ? uiState.cacheStats : ""
                color: "#00FFFF" // Cyan
                font.family: "Monospace"
                visible: uiState ? uiState.debugCache : false
                Layout.rightMargin: 10
            }


            // Saturation slider (only visible in saturation mode)
            Row {
                visible: uiState && uiState.colorMode === "saturation"
                spacing: 5
                Layout.rightMargin: 10

                Label {
                    text: "Saturation:"
                    color: root.currentTextColor
                    anchors.verticalCenter: parent.verticalCenter
                }

                Slider {
                    id: saturationSlider
                    from: 0.0
                    to: 1.0
                    value: uiState ? uiState.saturationFactor : 1.0
                    stepSize: 0.01
                    width: 150

                    onMoved: {
                        if (controller) controller.set_saturation_factor(value)
                    }
                }

                Label {
                    text: Math.round(saturationSlider.value * 100) + "%"
                    color: root.currentTextColor
                    anchors.verticalCenter: parent.verticalCenter
                    Layout.preferredWidth: 40
                }
            }

            Label {
                id: statusMessageLabel
                text: uiState ? uiState.statusMessage : ""
                color: root.currentTextColor
                visible: uiState ? (uiState.statusMessage !== "") : false
                Layout.rightMargin: 10
            }

            // Grid view controls (visible when in grid view) - right side
            Row {
                visible: uiState && uiState.isGridViewActive
                spacing: 10
                Layout.rightMargin: 15

                // Selection info (uses efficient count property, not full list)
                Label {
                    property int selCount: uiState ? uiState.gridSelectedCount : 0
                    text: selCount > 0 ? selCount + " selected" : ""
                    color: "#4CAF50"
                    font.bold: true
                    visible: selCount > 0
                    anchors.verticalCenter: parent.verticalCenter
                }

                // Clear selection button
                Rectangle {
                    visible: uiState ? uiState.gridSelectedCount > 0 : false
                    width: clearLabel.implicitWidth + 16
                    height: 26
                    radius: 4
                    color: clearMouseArea.containsMouse ? "#d32f2f" : "#c62828"
                    anchors.verticalCenter: parent.verticalCenter

                    Label {
                        id: clearLabel
                        anchors.centerIn: parent
                        text: "Clear Selection"
                        color: "white"
                        font.pixelSize: 12
                    }

                    MouseArea {
                        id: clearMouseArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { if (uiState) uiState.gridClearSelection() }
                    }
                }

                // Back button (only shown when there's history)
                Rectangle {
                    visible: uiState && uiState.gridCanGoBack
                    width: backLabel.implicitWidth + 16
                    height: 26
                    radius: 4
                    color: backMouseArea.containsMouse ? "#616161" : "#424242"
                    anchors.verticalCenter: parent.verticalCenter

                    Label {
                        id: backLabel
                        anchors.centerIn: parent
                        text: "← Back"
                        color: "white"
                        font.pixelSize: 12
                    }

                    MouseArea {
                        id: backMouseArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { if (uiState) uiState.gridGoBack() }
                    }
                }

                // Refresh button
                Rectangle {
                    width: refreshLabel.implicitWidth + 16
                    height: 26
                    radius: 4
                    color: refreshMouseArea.containsMouse ? "#1976D2" : "#1565C0"
                    anchors.verticalCenter: parent.verticalCenter

                    Label {
                        id: refreshLabel
                        anchors.centerIn: parent
                        text: "Refresh"
                        color: "white"
                        font.pixelSize: 12
                    }

                    MouseArea {
                        id: refreshMouseArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { if (uiState) uiState.gridRefresh() }
                    }
                }

                // Single Image View button
                Rectangle {
                    width: singleViewLabel.implicitWidth + 16
                    height: 26
                    radius: 4
                    color: singleViewMouseArea.containsMouse ? "#388E3C" : "#2E7D32"
                    anchors.verticalCenter: parent.verticalCenter

                    Label {
                        id: singleViewLabel
                        anchors.centerIn: parent
                        text: "Single View"
                        color: "white"
                        font.pixelSize: 12
                    }

                    MouseArea {
                        id: singleViewMouseArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { if (uiState) uiState.toggleGridView() }
                    }
                }
            }
        }
    }

    // -------- DIALOGS --------

    // Old, more robust About dialog
    Dialog {
        id: aboutDialog
        title: "Key Bindings"
        standardButtons: Dialog.Ok
        modal: true
        closePolicy: Popup.CloseOnEscape
        focus: true
        width: 1000
        height: 750

        background: Rectangle {
            color: root.currentBackgroundColor
        }

        contentItem: ScrollView {
            clip: true
            
            Row {
                spacing: 20
                
                // Column 1
                Text {
                    width: 450
                    text: "<b>FastStack Keyboard and Mouse Commands</b><br><br>" +
                          "<b>Navigation:</b><br>" +
                          "&nbsp;&nbsp;J / Right Arrow: Next Image<br>" +
                          "&nbsp;&nbsp;K / Left Arrow: Previous Image<br>" +
                          "&nbsp;&nbsp;G: Jump to Image Number<br>" +
                          "&nbsp;&nbsp;Alt+U: Jump to Last Uploaded<br>" +
                          "&nbsp;&nbsp;I: Show EXIF Data<br>" +
                          "&nbsp;&nbsp;T: Toggle Thumbnail Grid / Single Image View<br>" +
                          "&nbsp;&nbsp;F11: Toggle Fullscreen (Loupe View)<br><br>" +
                          "<b>Thumbnail Grid View:</b><br>" +
                          "&nbsp;&nbsp;Arrow Keys: Navigate between images<br>" +
                          "&nbsp;&nbsp;Enter: Open current image in single view<br>" +
                          "&nbsp;&nbsp;Space: Toggle selection on current image<br>" +
                          "&nbsp;&nbsp;Click: Open image in single view<br>" +
                          "&nbsp;&nbsp;Right-click / Ctrl+Click: Toggle selection<br>" +
                          "&nbsp;&nbsp;Shift+Click: Select range<br>" +
                          "&nbsp;&nbsp;B: Add selected images to batch<br>" +
                          "&nbsp;&nbsp;Delete/Backspace: Delete selected or cursor image<br>" +
                          "&nbsp;&nbsp;Esc: Clear selection or switch to single view<br><br>" +
                          "<b>Viewing:</b><br>" +
                          "&nbsp;&nbsp;Mouse Wheel: Zoom in/out<br>" +
                          "&nbsp;&nbsp;Left-click + Drag: Pan image<br>" +
                          "&nbsp;&nbsp;Ctrl+0: Reset zoom and pan to fit window<br>" +
                          "&nbsp;&nbsp;Ctrl+1/2/3/4: Zoom to 100%/200%/300%/400%<br><br>" +
                          "<b>Stacking:</b><br>" +
                          "&nbsp;&nbsp;[: Begin new stack<br>" +
                          "&nbsp;&nbsp;]: End current stack<br>" +
                          "&nbsp;&nbsp;C: Clear all stacks<br>" +
                          "&nbsp;&nbsp;S: Toggle current image in/out of stack<br>" +
                          "&nbsp;&nbsp;X: Remove current image from batch/stack"
                    padding: 10
                    wrapMode: Text.WordWrap
                    color: root.currentTextColor
                }

                // Column 2
                Text {
                    width: 450
                    text: "<br><br>" + // Spacer to align with first section under title
                          "<b>Batch Selection (for drag-and-drop):</b><br>" +
                          "&nbsp;&nbsp;{: Begin new batch<br>" +
                          "&nbsp;&nbsp;B: Toggle current image in/out of batch<br>" +
                          "&nbsp;&nbsp;}: End current batch<br>" +
                          "&nbsp;&nbsp;\\: Clear all batches<br><br>" +
                          "<b>Flag Toggles:</b><br>" +
                          "&nbsp;&nbsp;D: Toggle todo flag<br>" +
                          "&nbsp;&nbsp;F: Toggle favorite flag<br>" +
                          "&nbsp;&nbsp;U: Toggle uploaded flag<br>" +
                          "&nbsp;&nbsp;Ctrl+E: Toggle edited flag<br>" +
                          "&nbsp;&nbsp;Ctrl+S: Toggle stacked flag<br><br>" +
                          "<b>File Management:</b><br>" +
                          "&nbsp;&nbsp;Delete/Backspace: Move current image to recycle bin<br>" +
                          "&nbsp;&nbsp;Ctrl+Z: Undo last action<br><br>" +
                          "<b>Image Editing:</b><br>" +
                          "&nbsp;&nbsp;E: Toggle Image Editor<br>" +
                          "&nbsp;&nbsp;Ctrl+S (in editor): Save edited image<br>" +
                          "&nbsp;&nbsp;A: Quick auto white balance<br>" +
                          "&nbsp;&nbsp;L: Quick auto levels<br>" +
                          "&nbsp;&nbsp;O (or right-click): Toggle crop mode<br>" +
                          "&nbsp;&nbsp;&nbsp;&nbsp;1/2/3/4: Set aspect ratio (1:1, 4:3, 3:2, 16:9)<br>" +
                          "&nbsp;&nbsp;&nbsp;&nbsp;Enter: Execute crop<br>" +
                          "&nbsp;&nbsp;&nbsp;&nbsp;Esc: Cancel crop<br><br>" +
                          "<b>Other Actions:</b><br>" +
                          "&nbsp;&nbsp;Enter: Launch Helicon Focus<br>" +
                          "&nbsp;&nbsp;P: Edit in Photoshop<br>" +
                          "&nbsp;&nbsp;H: Toggle histogram window<br>" +
                          "&nbsp;&nbsp;Ctrl+C: Copy image path to clipboard<br>" +
                          "&nbsp;&nbsp;Esc: Close dialog/editor, switch to grid view, or exit fullscreen"
                    padding: 10
                    wrapMode: Text.WordWrap
                    color: root.currentTextColor
                }
            }
        }
    }

    Dialog {
        id: showStacksDialog
        title: "Stack Information"
        standardButtons: Dialog.Ok
        modal: true
        closePolicy: Popup.CloseOnEscape
        focus: true
        width: 400
        height: 300

        background: Rectangle {
            color: root.currentBackgroundColor
        }

        contentItem: Text {
            text: (uiState && uiState.stackSummary) ? uiState.stackSummary : "No stacks defined."
            padding: 10
            wrapMode: Text.WordWrap
            color: root.currentTextColor
        }
    }

    SettingsDialog {
        id: settingsDialog
    }

    FilterDialog {
        id: filterDialog
        backgroundColor: root.currentBackgroundColor
        textColor: root.currentTextColor
        onAccepted: {
            if (uiState) uiState.applyFilter(filterString, filterFlags)
        }
    }

    JumpToImageDialog {
        id: jumpToImageDialog
        backgroundColor: root.currentBackgroundColor
        textColor: root.currentTextColor
        maxImageCount: uiState ? uiState.imageCount : 0
    }

    DeleteBatchDialog {
        id: deleteBatchDialog
        backgroundColor: root.currentBackgroundColor
        textColor: root.currentTextColor
    }
    
    HistogramWindow {
        id: histogramWindow
        windowBackgroundColor: root.currentBackgroundColor
        primaryTextColor: root.currentTextColor
        gridLineColor: root.isDarkTheme ? "#454545" : "#dcdcdc"
    }

    ImageEditorDialog {
        id: imageEditorDialog
        backgroundColor: root.currentBackgroundColor
        textColor: root.currentTextColor
        onVisibleChanged: {
            if (!visible) {
                mainViewLoader.forceActiveFocus()
            }
        }
    }

    function show_jump_to_image_dialog() {
        jumpToImageDialog.open()
    }

    function show_delete_batch_dialog(count) {
        deleteBatchDialog.batchCount = count
        deleteBatchDialog.open()
    }

    ExifDialog {
        id: exifDialog
        backgroundColor: root.currentBackgroundColor
        textColor: root.currentTextColor
    }

    BatchProgressDialog {
        id: batchProgressDialog
        backgroundColor: root.currentBackgroundColor
        textColor: root.currentTextColor
    }

    // Debug Cache Indicator (Yellow Square)
    Rectangle {
        id: debugIndicator
        width: 30
        height: 30
        color: "yellow"
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 20
        z: 9999 // Ensure it is on top of everything, including footer
        visible: uiState ? (uiState.debugCache && uiState.isDecoding) : false
        
        Text {
            anchors.centerIn: parent
            text: "D"
            font.bold: true
            color: "black"
        }
    }
    Dialog {
        id: recycleBinCleanupDialog
        title: "Clean up Recycle Bins?"
        x: (parent.width - width) / 2
        y: (parent.height - height) / 2
        width: Math.min(600, parent.width * 0.9)
        modal: true
        standardButtons: Dialog.NoButton

        // Single source of truth for per-bin restore info.
        // Populated on open and after each restore action.
        property var binInfo: []

        function refreshBinInfo() {
            if (uiState) {
                binInfo = uiState.getPerBinRestoreInfo()
            }
        }

        onOpened: refreshBinInfo()

        // Ensure the dialog is fully opaque and has a solid background
        background: Rectangle {
            color: root.isDarkTheme ? "#1e1e1e" : "#fdfdfd"
            border.color: root.isDarkTheme ? "#444444" : "#dddddd"
            border.width: 1
            radius: 12
        }

        header: Rectangle {
            implicitHeight: 60
            color: root.isDarkTheme ? "#252525" : "#f2f2f2"
            radius: 12
            // Bottom corners should not be rounded to merge with body
            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: 12
                color: parent.color
            }
            Text {
                anchors.centerIn: parent
                text: "Clean up Recycle Bins?"
                color: root.currentTextColor
                font.bold: true
                font.pixelSize: 20
            }
        }

        contentItem: Column {
            id: dialogContent
            width: recycleBinCleanupDialog.width
            spacing: 16
            topPadding: 10
            bottomPadding: 10
            leftPadding: 20
            rightPadding: 20

            // Summary line
            Label {
                width: dialogContent.width - 40
                text: uiState ? uiState.recycleBinStatsText : "Loading..."
                color: root.isDarkTheme ? "#efefef" : "#333333"
                wrapMode: Text.WordWrap
                font.pixelSize: 15
                lineHeight: 1.3
            }

            // ---- Per-bin restore rows (restorable bins only) ----
            Repeater {
                id: restorableRepeater
                model: recycleBinCleanupDialog.binInfo.filter(function(b) { return b.status === "restorable" })

                delegate: Rectangle {
                    width: dialogContent.width - 40
                    height: binRowLayout.implicitHeight + 20
                    radius: 8
                    color: root.isDarkTheme ? "#252525" : "#f2f2f2"
                    border.color: root.isDarkTheme ? "#333333" : "#e0e0e0"
                    border.width: 1

                    RowLayout {
                        id: binRowLayout
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.margins: 12
                        spacing: 12

                        Column {
                            Layout.fillWidth: true
                            spacing: 2

                            Label {
                                text: modelData.label
                                color: root.isDarkTheme ? "#efefef" : "#333333"
                                font.pixelSize: 14
                                font.bold: true
                                elide: Text.ElideMiddle
                                width: parent.width
                            }
                            Label {
                                text: modelData.dest_dir
                                color: root.isDarkTheme ? "#888888" : "#999999"
                                font.pixelSize: 11
                                elide: Text.ElideMiddle
                                width: parent.width
                            }
                            Label {
                                text: {
                                    var parts = []
                                    if (modelData.jpg_count > 0) parts.push(modelData.jpg_count + " JPG")
                                    if (modelData.raw_count > 0) parts.push(modelData.raw_count + " RAW")
                                    if (modelData.other_count > 0) parts.push(modelData.other_count + " other")
                                    var s = parts.join(", ")
                                    if (modelData.legacy_count > 0)
                                        s += " + " + modelData.legacy_count + " legacy"
                                    return s + " \u2014 " + modelData.total_restorable + " restorable"
                                }
                                color: root.isDarkTheme ? "#aaaaaa" : "#666666"
                                font.pixelSize: 13
                            }
                        }

                        // Per-bin Restore button
                        Rectangle {
                            width: restoreBinBtnText.implicitWidth + 30
                            height: 34
                            radius: 17
                            color: "#4fb360"
                            Layout.alignment: Qt.AlignVCenter

                            Text {
                                id: restoreBinBtnText
                                anchors.centerIn: parent
                                text: "Restore"
                                color: "white"
                                font.pixelSize: 13
                                font.bold: true
                            }
                            MouseArea {
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (uiState) {
                                        uiState.restoreSingleBin(modelData.bin_path)
                                        recycleBinCleanupDialog.refreshBinInfo()
                                        // Auto-close if nothing left
                                        if (recycleBinCleanupDialog.binInfo.length === 0) {
                                            recycleBinCleanupDialog.close()
                                        }
                                    }
                                }
                                onEntered: parent.color = "#5cc46d"
                                onExited: parent.color = "#4fb360"
                            }
                        }
                    }
                }
            }

            // ---- Unavailable bins section ----
            Column {
                width: dialogContent.width - 40
                spacing: 6
                visible: {
                    var items = recycleBinCleanupDialog.binInfo.filter(function(b) { return b.status === "unavailable" })
                    return items.length > 0
                }

                Label {
                    text: "Not auto-restorable (legacy format)"
                    color: root.isDarkTheme ? "#ff8a65" : "#bf360c"
                    font.pixelSize: 14
                    font.bold: true
                }
                Label {
                    width: parent.width
                    text: "These bins contain files from an older version without restore metadata. They can only be deleted."
                    color: root.isDarkTheme ? "#999999" : "#777777"
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }

                Repeater {
                    model: recycleBinCleanupDialog.binInfo.filter(function(b) { return b.status === "unavailable" })

                    delegate: Label {
                        width: dialogContent.width - 40
                        text: modelData.dest_dir + " \u2014 " + modelData.total_files + " file" + (modelData.total_files !== 1 ? "s" : "")
                        color: root.isDarkTheme ? "#aaaaaa" : "#666666"
                        font.pixelSize: 13
                        elide: Text.ElideMiddle
                        topPadding: 2
                    }
                }
            }

            // ---- Expandable details section ----
            property bool detailsExpanded: false

            Row {
                width: dialogContent.width - 40
                spacing: 12

                Label {
                    text: "All files in recycle bins:"
                    color: "#81C784"
                    font.pixelSize: 15
                    font.bold: true
                    anchors.verticalCenter: parent.verticalCenter
                }

                Rectangle {
                    width: detailsToggleText.implicitWidth + 20
                    height: 28
                    radius: 14
                    color: toggleMouseArea.containsMouse ? (root.isDarkTheme ? "#333333" : "#e0e0e0") : "transparent"
                    border.color: root.isDarkTheme ? "#555555" : "#cccccc"
                    border.width: 1
                    anchors.verticalCenter: parent.verticalCenter

                    Text {
                        id: detailsToggleText
                        anchors.centerIn: parent
                        text: dialogContent.detailsExpanded ? "Hide Details" : "Show Details"
                        color: root.currentTextColor
                        font.pixelSize: 12
                    }

                    MouseArea {
                        id: toggleMouseArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: dialogContent.detailsExpanded = !dialogContent.detailsExpanded
                    }
                }
            }

            Rectangle {
                id: detailedSection
                width: dialogContent.width - 40
                height: dialogContent.detailsExpanded ? Math.min(250, root.height * 0.4) : 0
                visible: height > 0
                color: root.isDarkTheme ? "#121212" : "#f9f9f9"
                border.color: root.isDarkTheme ? "#333333" : "#eeeeee"
                border.width: 1
                radius: 8
                clip: true

                Behavior on height { NumberAnimation { duration: 250; easing.type: Easing.OutCubic } }

                ScrollView {
                    id: detailsScrollView
                    anchors.fill: parent
                    anchors.margins: 8

                    TextArea {
                        id: detailsText
                        width: detailsScrollView.availableWidth
                        text: uiState ? uiState.recycleBinDetailedText : ""
                        color: root.isDarkTheme ? "#efefef" : "#333333"
                        font.family: "Consolas, 'Courier New', monospace"
                        font.pixelSize: 13
                        padding: 10
                        wrapMode: Text.WrapAnywhere
                        readOnly: true
                        selectByMouse: true
                        background: Rectangle {
                            color: "transparent"
                        }
                    }
                }
            }

            // ---- Action buttons ----
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 15
                topPadding: 10

                // Cancel Button
                Rectangle {
                    width: cancelBtnText.implicitWidth + 40
                    height: 44
                    radius: 22
                    color: "transparent"
                    border.color: root.isDarkTheme ? "#555555" : "#cccccc"
                    border.width: 1

                    Text {
                        id: cancelBtnText
                        anchors.centerIn: parent
                        text: "Cancel"
                        color: root.currentTextColor
                        font.pixelSize: 15
                        font.bold: true
                    }
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: recycleBinCleanupDialog.close()
                        cursorShape: Qt.PointingHandCursor
                        onEntered: parent.color = root.isDarkTheme ? "#2a2a2a" : "#eeeeee"
                        onExited: parent.color = "transparent"
                    }
                }

                // Keep and Quit Button
                Rectangle {
                    width: keepBtnText.implicitWidth + 40
                    height: 44
                    radius: 22
                    color: root.isDarkTheme ? "#333333" : "#e0e0e0"

                    Text {
                        id: keepBtnText
                        anchors.centerIn: parent
                        text: "Keep and Quit"
                        color: root.currentTextColor
                        font.pixelSize: 15
                        font.bold: true
                    }
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: {
                            allowCloseWithRecycleBins = true
                            recycleBinCleanupDialog.close()
                            Qt.quit()
                        }
                        cursorShape: Qt.PointingHandCursor
                        onEntered: parent.color = root.isDarkTheme ? "#444444" : "#d0d0d0"
                        onExited: parent.color = root.isDarkTheme ? "#333333" : "#e0e0e0"
                    }
                }

                // Delete and Quit Button (Primary Action)
                Rectangle {
                    width: deleteBtnText.implicitWidth + 40
                    height: 44
                    radius: 22
                    color: "#ef5350"

                    Text {
                        id: deleteBtnText
                        anchors.centerIn: parent
                        text: "Delete and Quit"
                        color: "white"
                        font.pixelSize: 15
                        font.bold: true
                    }
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: {
                            if (uiState) uiState.cleanupRecycleBins()
                            allowCloseWithRecycleBins = true
                            recycleBinCleanupDialog.close()
                            Qt.quit()
                        }
                        cursorShape: Qt.PointingHandCursor
                        onEntered: parent.color = "#f44336"
                        onExited: parent.color = "#ef5350"
                    }
                }
            }
        }
    }
}
