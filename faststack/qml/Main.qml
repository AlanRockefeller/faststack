pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Window
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

    property var uiStateRef: null
    property var controllerRef: null
    property bool allowCloseWithRecycleBins: false
    property bool allowCloseWithBatches: false
    property bool fullScreenLoupe: false
    property var savedWindowGeometry: ({})

    function enterFullScreenLoupe() {
        if (!root.uiStateRef || root.uiStateRef.isGridViewActive) return

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

    function clampWindowToVisibleScreen() {
        if (root.visibility !== Window.Windowed || !root.screen) return

        var screenX = root.screen.virtualX
        var screenY = root.screen.virtualY
        var screenWidth = root.screen.desktopAvailableWidth
        var screenHeight = root.screen.desktopAvailableHeight
        if (screenWidth <= 0 || screenHeight <= 0) return

        var newWidth = Math.min(root.width, screenWidth)
        var newHeight = Math.min(root.height, screenHeight)
        var newX = Math.max(screenX, Math.min(root.x, screenX + screenWidth - newWidth))
        var newY = Math.max(screenY, Math.min(root.y, screenY + screenHeight - newHeight))

        root.x = newX
        root.y = newY
        root.width = newWidth
        root.height = newHeight
    }

    function openDialogSafely(dialog) {
        root.clampWindowToVisibleScreen()
        dialog.open()
    }

    onClosing: function(close) {
        if (!root.allowCloseWithBatches && root.controllerRef) {
            var definedBatchCount = root.controllerRef.get_defined_batch_count()
            if (definedBatchCount > 0) {
                close.accepted = false
                quitBatchesDialog.batchCount = definedBatchCount
                root.openDialogSafely(quitBatchesDialog)
                return
            }
        }

        if (!root.allowCloseWithRecycleBins
                && root.uiStateRef
                && root.uiStateRef.hasRecycleBinItems) {
            close.accepted = false
            root.uiStateRef.refreshRecycleBinStats()
            root.openDialogSafely(recycleBinCleanupDialog)
            return
        }

        if (root.controllerRef && !root.controllerRef.prepare_for_app_close()) {
            close.accepted = false
            root.allowCloseWithBatches = false
            return
        }

        close.accepted = true
    }

    Component.onCompleted: {
        root.uiStateRef = uiState
        root.controllerRef = controller
        // Initialization complete
    }

    Material.theme: (root.uiStateRef && root.uiStateRef.theme === 0) ? Material.Dark : Material.Light
    Material.accent: "#4fb360"

    // Frameless windows on Windows report FullScreen instead of Maximized
    // after showMaximized(). Treat both as "maximized" unless we are in the
    // app's own fullscreen loupe mode (which is the real FullScreen).
    property bool isMaximized: root.visibility === Window.Maximized
                               || (root.visibility === Window.FullScreen
                                   && !root.fullScreenLoupe)
    property bool isDarkTheme: root.uiStateRef ? root.uiStateRef.theme === 0 : true
    property color currentBackgroundColor: isDarkTheme ? "#000000" : "#ffffff"
    property color currentTextColor: isDarkTheme ? "white" : "black"
    property color hoverColor: isDarkTheme ? Qt.lighter(currentBackgroundColor, 1.5) : Qt.darker(currentBackgroundColor, 1.1)
    property color menuHoverColor: isDarkTheme ? "#555555" : "#e0e0e0"
    property color menuSelectedColor: isDarkTheme ? "#505050" : "#d0ffd0"


    background: Rectangle { color: root.currentBackgroundColor }

    function toggleTheme() {
        if (root.uiStateRef) {
            root.uiStateRef.theme = (root.uiStateRef.theme === 0 ? 1 : 0)
        }
    }

    function openExifDialog(data) {
        exifDialog.summaryData = data.summary
        exifDialog.fullData = data.full
        root.openDialogSafely(exifDialog)
    }

    function openColorInfoDialog(text) {
        colorInfoDialog.infoText = text
        root.openDialogSafely(colorInfoDialog)
    }

    function setGridPrefetch(item, enabled) {
        var methodName = "set" + "PrefetchEnabled"
        var setter = item ? item[methodName] : null
        if (typeof setter === "function") {
            setter.call(item, enabled)
        }
    }

    function toArray(value) {
        if (value === null || value === undefined) {
            return []
        }

        if (Array.isArray(value)) {
            return value
        }

        var valueType = typeof value
        if (valueType === "string" || valueType === "number"
                || valueType === "boolean" || valueType === "function") {
            return []
        }

        if (typeof value.length === "number") {
            var result = []
            for (var i = 0; i < value.length; ++i) {
                result.push(value[i])
            }
            return result
        }

        return []
    }

    function stringOrEmpty(value) {
        if (typeof value === "string") {
            return value
        }

        if (value === null || value === undefined) {
            return ""
        }

        if (typeof value === "number" || typeof value === "boolean") {
            return String(value)
        }

        // Avoid rendering unexpected objects as "[object Object]".
        return ""
    }

    // The EXIF brief shown in the status bar may end with a GPS-derived
    // distance segment formatted as "<n> m" (see metadata._format_distance_meters).
    // These helpers let the status bar render the distance as its own label so a
    // tooltip can explain what it means.
    function exifBriefDistanceRegExp() {
        return /^\d+ m$/
    }

    function exifBriefWithoutDistance(brief) {
        var s = root.stringOrEmpty(brief)
        if (s === "") return ""
        var re = root.exifBriefDistanceRegExp()
        var kept = []
        var parts = s.split(" | ")
        for (var i = 0; i < parts.length; i++) {
            if (!re.test(parts[i])) kept.push(parts[i])
        }
        return kept.join(" | ")
    }

    function exifBriefDistance(brief) {
        var s = root.stringOrEmpty(brief)
        if (s === "") return ""
        var re = root.exifBriefDistanceRegExp()
        var parts = s.split(" | ")
        for (var i = 0; i < parts.length; i++) {
            if (re.test(parts[i])) return parts[i]
        }
        return ""
    }

    function itemsWithStatus(items, status) {
        var source = root.toArray(items)
        var result = []

        for (var i = 0; i < source.length; ++i) {
            var item = source[i]
            if (item && item.status === status) {
                result.push(item)
            }
        }

        return result
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
                    if (root.uiStateRef && root.uiStateRef.debugMode)
                        console.log("[TitleBar] double-tap: visibility =", root.visibility,
                                    "isMaximized =", root.isMaximized,
                                    "fullScreenLoupe =", root.fullScreenLoupe)
                    if (root.isMaximized) {
                        root.showNormal()
                    } else {
                        root.showMaximized()
                    }
                    if (root.uiStateRef && root.uiStateRef.debugMode)
                        console.log("[TitleBar] double-tap: after visibility =", root.visibility,
                                    "isMaximized =", root.isMaximized)
                }
            }

            DragHandler {
                id: titleBarDragHandler
                target: null  // we move the window, not this item
                onActiveChanged: {
                    if (active) {
                        if (root.uiStateRef && root.uiStateRef.debugMode)
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
            property real ps: loupe ? loupe.currentPixelZoomScale : 0

            text: {
                if (!loupe || fs <= 0 || zs <= 0 || ps <= 0) return ""
                if (root.uiStateRef && root.uiStateRef.isGridViewActive) return ""
                var ratio = zs / fs
                if (Math.abs(ratio - 1.0) < 0.03) return "Zoom: Fit to window (" + Math.round(ps * 100) + "%)"
                return "Zoom: " + Math.round(ps * 100) + "%"
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

            MenuActionItem {
                width: 200
                text: "Open Folder..."
                hoverFillColor: root.hoverColor
                onClicked: {
                    if (root.uiStateRef) {
                        root.uiStateRef.open_folder()
                    }
                    fileMenu.close()
                }
                defaultTextColor: root.currentTextColor
            }
            MenuActionItem {
                width: 200
                text: "Settings..."
                hoverFillColor: root.menuHoverColor
                onClicked: {
                    root.openDialogSafely(settingsDialog)
                    fileMenu.close()
                }
                defaultTextColor: root.currentTextColor
            }
            Rectangle {
                width: 200
                height: 1
                color: root.isDarkTheme ? "#666666" : "#cccccc"
            }
            MenuActionItem {
                width: 200
                text: "Exit"
                hoverFillColor: root.menuHoverColor
                onClicked: Qt.quit()
                defaultTextColor: root.currentTextColor
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
            MenuActionItem {
                width: 220
                text: "Toggle Light/Dark Mode"
                hoverFillColor: root.menuHoverColor
                onClicked: {
                    root.toggleTheme()
                    viewMenu.close()
                }
                defaultTextColor: root.currentTextColor
            }

            // Separator
            Rectangle {
                width: 220
                height: 1
                color: root.isDarkTheme ? "#666666" : "#cccccc"
            }

            // Color: None (Original)
            MenuActionItem {
                width: 220
                text: "Color: None (Original)"
                hoverFillColor: root.menuHoverColor
                selectedFillColor: root.menuSelectedColor
                selected: root.uiStateRef && root.uiStateRef.colorMode === "none"
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.controllerRef) root.controllerRef.set_color_mode("none")
                    viewMenu.close()
                }
            }

            // Color: Saturation Compensation
            MenuActionItem {
                width: 220
                text: "Color: Saturation Compensation"
                hoverFillColor: root.menuHoverColor
                selectedFillColor: root.menuSelectedColor
                selected: root.uiStateRef && root.uiStateRef.colorMode === "saturation"
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.controllerRef) root.controllerRef.set_color_mode("saturation")
                    viewMenu.close()
                }
            }

            // Color: Full ICC Profile
            MenuActionItem {
                width: 220
                text: "Color: Full ICC Profile"
                hoverFillColor: root.menuHoverColor
                selectedFillColor: root.menuSelectedColor
                selected: root.uiStateRef && root.uiStateRef.colorMode === "icc"
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.controllerRef) root.controllerRef.set_color_mode("icc")
                    viewMenu.close()
                }
            }

            MenuActionItem {
                width: 220
                text: "Color Information..."
                enabled: root.uiStateRef ? !root.uiStateRef.isGridViewActive : false
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                disabledTextColor: root.isDarkTheme ? "#666666" : "#999999"
                onClicked: {
                    if (root.controllerRef) root.controllerRef.show_color_information_dialog()
                    viewMenu.close()
                }
            }
        }
    }

    Menu {
        id: actionsMenu
        parent: Overlay.overlay
        implicitWidth: 220
        onClosed: sortSubMenu.close()

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
            MenuActionItem {
                width: 220
                text: (root.uiStateRef && root.uiStateRef.isRawDeveloping) ? "Developing RAW..." : ((root.uiStateRef && root.uiStateRef.hasWorkingTif) ? "Re-develop RAW" : "Develop RAW")
                enabled: root.uiStateRef ? (root.uiStateRef.hasRaw && !root.uiStateRef.isRawDeveloping) : false
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                disabledTextColor: root.isDarkTheme ? "#666666" : "#999999"
                onClicked: {
                    if (root.uiStateRef) root.uiStateRef.developRaw()
                    actionsMenu.close()
                }
            }

            // Edit Image (from old Main.qml)
            MenuActionItem {
                width: 220
                text: "Edit Image"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.uiStateRef) {
                        if (root.uiStateRef.isCropping) {
                            root.uiStateRef.statusMessage = "Apply or cancel the crop before editing"
                            actionsMenu.close()
                            return
                        }
                        if (root.uiStateRef.isEditorOpen) {
                            root.uiStateRef.isEditorOpen = false
                        } else {
                            root.uiStateRef.isEditorExpanded = false
                            root.uiStateRef.isEditorOpen = true
                            if (root.controllerRef) root.controllerRef.load_image_for_editing()
                        }
                    }
                    actionsMenu.close()
                }
            }
            MenuActionItem {
                width: 220
                text: "Crop Image"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.controllerRef) {
                        root.controllerRef.toggle_crop_mode()
                    }
                    actionsMenu.close()
                }
            }
            MenuActionItem {
                width: 220
                text: "Duplicate Image"
                enabled: root.uiStateRef ? root.uiStateRef.imageCount > 0 : false
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                disabledTextColor: root.isDarkTheme ? "#666666" : "#999999"
                onClicked: {
                    if (root.controllerRef) root.controllerRef.duplicate_current_image()
                    actionsMenu.close()
                }
            }

            MenuActionItem {
                width: 220
                text: "Run Stacks (raw)"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: { if (root.uiStateRef) root.uiStateRef.launch_helicon(true); actionsMenu.close() }
            }
            MenuActionItem {
                width: 220
                text: "Run Stacks (jpg)"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: { if (root.uiStateRef) root.uiStateRef.launch_helicon(false); actionsMenu.close() }
            }
            MenuActionItem {
                width: 220
                text: "Clear Stacks"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: { if (root.uiStateRef) root.uiStateRef.clear_all_stacks(); actionsMenu.close() }
            }
            MenuActionItem {
                width: 220
                text: "Show Stacks"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: { root.openDialogSafely(showStacksDialog); actionsMenu.close() }
            }
            MenuActionItem {
                width: 220
                text: "Preload All Images"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: { if (root.uiStateRef) root.uiStateRef.preloadAllImages(); actionsMenu.close() }
            }
            MenuActionItem {
                width: 220
                text: "Filter Images..."
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: { root.openDialogSafely(filterDialog); actionsMenu.close() }
            }

            // Separator before Sort options
            Rectangle {
                width: 220
                height: 1
                color: root.isDarkTheme ? "#666666" : "#cccccc"
            }

            ItemDelegate {
                id: sortPhotosLauncher
                width: 220
                height: 36
                hoverEnabled: true
                background: Rectangle {
                    color: sortPhotosLauncher.hovered ? root.menuHoverColor : "transparent"
                }
                contentItem: Item {
                    Text {
                        anchors.left: parent.left
                        anchors.leftMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        text: "Sort Photos"
                        color: root.currentTextColor
                        verticalAlignment: Text.AlignVCenter
                    }
                    Text {
                        anchors.right: parent.right
                        anchors.rightMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        text: "\u25B6" // Right-pointing triangle
                        font.pixelSize: 10
                        color: root.currentTextColor
                        opacity: 0.6
                        verticalAlignment: Text.AlignVCenter
                    }
                }
                onHoveredChanged: {
                    if (hovered) {
                        sortSubMenu.popupAt(sortPhotosLauncher, sortPhotosLauncher.width - 4, 0)
                    }
                }
                onClicked: {
                    sortSubMenu.popupAt(sortPhotosLauncher, sortPhotosLauncher.width - 4, 0)
                }
                // Ensure keyboard activation works reliably
                Keys.onReturnPressed: clicked()
                Keys.onEnterPressed: clicked()
                Keys.onSpacePressed: clicked()
            }

            // Clear Filename Filter (from old Main.qml)
            MenuActionItem {
                width: 220
                text: "Clear Filename Filter"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.controllerRef) root.controllerRef.clear_filter()
                    actionsMenu.close()
                }
            }
            MenuActionItem {
                width: 220
                text: "Add Favorites to Batch"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.uiStateRef) root.uiStateRef.addFavoritesToBatch()
                    actionsMenu.close()
                }
            }
            MenuActionItem {
                width: 220
                text: "Add Uploaded to Batch"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.uiStateRef) root.uiStateRef.addUploadedToBatch()
                    actionsMenu.close()
                }
            }
            MenuActionItem {
                width: 220
                text: "Add Edited to Batch"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.uiStateRef) root.uiStateRef.addEditedToBatch()
                    actionsMenu.close()
                }
            }

            MenuActionItem {
                width: 220
                text: "Automatically add edited photos to batch"
                showCheckbox: true
                checkboxChecked: root.uiStateRef ? root.uiStateRef.autoAddEditedToBatch : true
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.uiStateRef) {
                        root.uiStateRef.autoAddEditedToBatch = !root.uiStateRef.autoAddEditedToBatch
                    }
                    actionsMenu.close()
                }
            }

            MenuActionItem {
                width: 220
                text: "Jump to Last Uploaded"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.uiStateRef) root.uiStateRef.jumpToLastUploaded()
                    actionsMenu.close()
                }
            }
            MenuActionItem {
                width: 220
                text: "Auto-Level Batch"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.uiStateRef) root.uiStateRef.batchAutoLevels()
                    actionsMenu.close()
                }
            }
            MenuActionItem {
                width: 220
                text: "Stack Source RAWs"
                enabled: root.uiStateRef ? root.uiStateRef.isStackedJpg : false
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                disabledTextColor: root.isDarkTheme ? "#666666" : "#999999"
                onClicked: {
                    if (root.uiStateRef) root.uiStateRef.stack_source_raws();
                    actionsMenu.close()
                }
            }

            // Separator before grid view toggle
            Rectangle {
                width: 220
                height: 1
                color: root.isDarkTheme ? "#666666" : "#cccccc"
            }

            // Toggle Grid/Loupe View
            MenuActionItem {
                width: 220
                text: root.uiStateRef && root.uiStateRef.isGridViewActive ? "Single Image View" : "Thumbnail View"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.uiStateRef) root.uiStateRef.toggleGridView();
                    actionsMenu.close()
                }
            }
        }
    }

    // A Popup, not a Menu: opening a sibling Menu would auto-close
    // actionsMenu (Qt menu mutual-exclusion). Popups don't exclude.
    Popup {
        id: sortSubMenu
        parent: Overlay.overlay
        implicitWidth: 180
        padding: 0
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        // Position relative to a source item, mapped into the overlay.
        function popupAt(item, dx, dy) {
            var p = item.mapToItem(Overlay.overlay, dx, dy)
            x = p.x
            y = p.y
            open()
        }

        background: Rectangle {
            implicitWidth: 180
            implicitHeight: sortSubMenuColumn.implicitHeight
            color: root.currentBackgroundColor
            border.color: root.isDarkTheme ? "#666666" : "#cccccc"
            radius: 4
        }

        contentItem: Column {
            id: sortSubMenuColumn

            MenuActionItem {
                width: 180
                text: "Default"
                hoverFillColor: root.menuHoverColor
                selectedFillColor: root.menuSelectedColor
                selected: root.uiStateRef && root.uiStateRef.sortMode === "default"
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.controllerRef) root.controllerRef.set_sort_mode("default")
                    sortSubMenu.close()
                    actionsMenu.close()
                }
            }
            MenuActionItem {
                width: 180
                text: "By Filename"
                hoverFillColor: root.menuHoverColor
                selectedFillColor: root.menuSelectedColor
                selected: root.uiStateRef && root.uiStateRef.sortMode === "filename"
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.controllerRef) root.controllerRef.set_sort_mode("filename")
                    sortSubMenu.close()
                    actionsMenu.close()
                }
            }
            MenuActionItem {
                width: 180
                text: "By Date"
                hoverFillColor: root.menuHoverColor
                selectedFillColor: root.menuSelectedColor
                selected: root.uiStateRef && root.uiStateRef.sortMode === "date"
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.controllerRef) root.controllerRef.set_sort_mode("date")
                    sortSubMenu.close()
                    actionsMenu.close()
                }
            }
            MenuActionItem {
                width: 180
                text: "By Date (reverse)"
                hoverFillColor: root.menuHoverColor
                selectedFillColor: root.menuSelectedColor
                selected: root.uiStateRef && root.uiStateRef.sortMode === "date_reverse"
                defaultTextColor: root.currentTextColor
                onClicked: {
                    if (root.controllerRef) root.controllerRef.set_sort_mode("date_reverse")
                    sortSubMenu.close()
                    actionsMenu.close()
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

            MenuActionItem {
                width: 200
                text: "Key Bindings"
                hoverFillColor: root.menuHoverColor
                defaultTextColor: root.currentTextColor
                onClicked: { root.openDialogSafely(aboutDialog); helpMenu.close() }
            }
        }
    }

    property int footerHeight: 60
    property int effectiveFooterHeight: fullScreenLoupe ? 0 : footerHeight

    Shortcut {
        sequence: "F11"
        context: Qt.ApplicationShortcut
        enabled: root.uiStateRef ? !root.uiStateRef.isGridViewActive && !root.uiStateRef.isDialogOpen : false
        onActivated: root.toggleFullScreenLoupe()
    }

    Shortcut {
        sequence: "Escape"
        context: Qt.ApplicationShortcut
        enabled: root.fullScreenLoupe && (!root.uiStateRef || !root.uiStateRef.isCropping)
        onActivated: root.exitFullScreenLoupe()
    }

    Shortcut {
        sequence: "Escape"
        context: Qt.ApplicationShortcut
        enabled: root.uiStateRef ? root.uiStateRef.isCropping && !root.uiStateRef.isCropRotating && !root.uiStateRef.isDialogOpen : false
        onActivated: {
            if (root.controllerRef) root.controllerRef.cancel_crop_mode()
        }
    }

    Shortcut {
        sequence: "E"
        context: Qt.WindowShortcut
        enabled: root.uiStateRef ? !root.uiStateRef.isDialogOpen : true
        onActivated: {
            if (!root.uiStateRef) return

            if (root.uiStateRef.isCropping) {
                root.uiStateRef.statusMessage = "Apply or cancel the crop before editing"
                return
            }

            if (root.uiStateRef.isEditorOpen) {
                root.uiStateRef.isEditorOpen = false
            } else {
                root.uiStateRef.isEditorExpanded = false
                root.uiStateRef.isEditorOpen = true
                if (root.controllerRef) {
                    root.controllerRef.load_image_for_editing()
                }
            }
        }
    }

    // Background Darkening Tool (K) — independent of the editor sidebar
    Shortcut {
        sequence: "K"
        context: Qt.ApplicationShortcut
        enabled: root.uiStateRef ? !root.uiStateRef.isDialogOpen && !root.uiStateRef.isCropping : false
        onActivated: {
            if (!root.uiStateRef || !root.controllerRef) return
            if (root.uiStateRef.isDarkening) {
                root.controllerRef.toggle_darken_mode()
            } else {
                root.controllerRef.open_darken_tool()
            }
        }
    }

    // Grid View Toggle (T for Thumbnails)
    Shortcut {
        sequence: "T"
        context: Qt.ApplicationShortcut
        enabled: root.uiStateRef ? !root.uiStateRef.isDialogOpen : true
        onActivated: {
            if (root.uiStateRef) root.uiStateRef.toggleGridView()
        }
    }

    // Handle View Switching and Prefetch Gating
    Connections {
        target: root.uiStateRef
        function onIsGridViewActiveChanged() {
            if (root.uiStateRef.isGridViewActive && root.fullScreenLoupe) {
                root.exitFullScreenLoupe()
            }

            var gridItem = gridViewLoader.item
            if (!gridItem) return

            if (root.uiStateRef.isGridViewActive) {
                // Switching TO grid:
                // 1. Immediately disable prefetch to block transient top-of-list requests
                //    that happen before the view layout/scroll is restored.
                root.setGridPrefetch(gridItem, false)

                // 2. Re-enable on next event loop tick.
                //    This allows the GridView to restore its currentIndex/contentY position.
                Qt.callLater(function() {
                    var it = gridViewLoader.item
                    if (root.uiStateRef.isGridViewActive && it) {
                        root.setGridPrefetch(it, true)
                    }
                })
            } else {
                // Switching AWAY from grid:
                // Disable immediately to stop background work.
                root.setGridPrefetch(gridItem, false)
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
        currentIndex: root.uiStateRef && root.uiStateRef.isGridViewActive ? 1 : 0

        // Index 0: Loupe View (single image)
        Item {
            id: loupeViewContainer
            Layout.fillWidth: true
            Layout.fillHeight: true

            Loader {
                id: mainViewLoader
                anchors.fill: parent
                source: "Components.qml"
                focus: !root.uiStateRef || !root.uiStateRef.isGridViewActive
                onLoaded: {
                    item.footerHeight = Qt.binding(function() { return root.effectiveFooterHeight })
                    item.isDarkTheme = Qt.binding(function() { return root.isDarkTheme })
                }

                // Key bindings implemented in old Main.qml
                Keys.onPressed: function(event) {
                    if (!root.uiStateRef || !root.controllerRef) {
                        return
                    }

                    // Global Key for saving edited image (Ctrl+S) when editor is open
                    if (event.key === Qt.Key_S && (event.modifiers & Qt.ControlModifier)) {
                        if (root.uiStateRef.isEditorOpen && root.uiStateRef.isCropping) {
                            root.uiStateRef.statusMessage = "Apply or cancel the crop before saving"
                            event.accepted = true
                        } else if (root.uiStateRef.isEditorOpen) {
                            root.controllerRef.save_edited_image()
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
                visible: root.uiStateRef && root.uiStateRef.isGridViewActive
                focus: root.uiStateRef && root.uiStateRef.isGridViewActive

                onLoaded: {
                    // Enable prefetch on startup if grid is active (single owner)
                    var loadedItem = item
                    if (root.uiStateRef && root.uiStateRef.isGridViewActive && loadedItem) {
                        // Delay to match the toggle behavior (allow layout to settle)
                        Qt.callLater(function() {
                            if (gridViewLoader.item === loadedItem && root.uiStateRef.isGridViewActive) {
                                root.setGridPrefetch(loadedItem, true)
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
                text: root.uiStateRef ? `Image: ${root.uiStateRef.currentIndex + 1} / ${root.uiStateRef.imageCount}` : "Image: - / -"
                color: root.currentTextColor
            }
            Label {
                text: (root.uiStateRef && root.uiStateRef.imageCount > 0)
                      ? (root.uiStateRef.currentFilename || "N/A")
                      : "N/A"
                color: root.currentTextColor
            }
            // EXIF brief: ISO, aperture, shutter speed, capture time.
            Label {
                id: exifBriefLabel
                visible: root.uiStateRef
                         && root.uiStateRef.imageCount > 0
                         && root.exifBriefWithoutDistance(root.uiStateRef.exifBrief).length > 0
                text: root.uiStateRef ? root.exifBriefWithoutDistance(root.uiStateRef.exifBrief) : ""
                color: root.currentTextColor
            }
            // GPS distance from the previous image, in meters. Shown as its own
            // label so a tooltip can explain it on hover.
            Label {
                id: exifDistanceLabel
                property string distanceText: root.uiStateRef ? root.exifBriefDistance(root.uiStateRef.exifBrief) : ""
                visible: root.uiStateRef
                         && root.uiStateRef.imageCount > 0
                         && exifDistanceLabel.distanceText.length > 0
                text: exifBriefLabel.visible
                      ? ("| " + exifDistanceLabel.distanceText)
                      : exifDistanceLabel.distanceText
                color: root.currentTextColor

                ToolTip.visible: exifDistanceMouse.containsMouse && exifDistanceLabel.visible
                ToolTip.text: "Distance in meters between this image and the previous one (calculated from GPS EXIF data)"
                ToolTip.delay: 500

                MouseArea {
                    id: exifDistanceMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    acceptedButtons: Qt.NoButton
                }
            }
            Label {
                id: directoryPathLabel
                visible: root.uiStateRef && root.uiStateRef.currentDirectory !== ""
                text: root.uiStateRef ? root.uiStateRef.currentDirectory : ""
                color: root.isDarkTheme ? "#888888" : "#777777"
                font.pixelSize: 11
                elide: Text.ElideMiddle
                Layout.maximumWidth: 300

                ToolTip.visible: directoryPathMouse.containsMouse && text !== ""
                ToolTip.text: root.uiStateRef ? root.uiStateRef.currentDirectory : ""
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
                text: root.uiStateRef ? ` Stacked: ${root.uiStateRef.stackedDate}` : ""
                color: "lightgreen"
                visible: root.uiStateRef ? (root.uiStateRef.imageCount > 0 && root.uiStateRef.isStacked) : false
            }
            Label {
                text: root.uiStateRef ? ` Uploaded on ${root.uiStateRef.uploadedDate}` : ""
                color: "lightgreen"
                visible: root.uiStateRef ? (root.uiStateRef.imageCount > 0 && root.uiStateRef.isUploaded) : false
            }
            Label {
                text: root.uiStateRef ? (root.uiStateRef.todoDate ? ` Todo since ${root.uiStateRef.todoDate}` : " Todo") : ""
                color: "#64B5F6"
                visible: root.uiStateRef ? (root.uiStateRef.imageCount > 0 && root.uiStateRef.isTodo) : false
            }
            Label {
                text: root.uiStateRef ? ` Edited on ${root.uiStateRef.editedDate}` : ""
                color: "lightgreen"
                visible: root.uiStateRef ? (root.uiStateRef.imageCount > 0 && root.uiStateRef.isEdited) : false
            }
            Label {
                text: root.uiStateRef ? ` Restacked on ${root.uiStateRef.restackedDate}` : ""
                color: "cyan"
                visible: root.uiStateRef ? (root.uiStateRef.imageCount > 0 && root.uiStateRef.isRestacked) : false
            }
            Label {
                text: " Favorite"
                color: "gold"
                visible: root.uiStateRef ? (root.uiStateRef.imageCount > 0 && root.uiStateRef.isFavorite) : false
            }
            Label {
                text: root.uiStateRef ? ` Filter: "${root.uiStateRef.filterString}"` : ""
                color: "yellow"
                font.bold: true
                visible: root.uiStateRef ? (root.uiStateRef.filterString !== "") : false
            }
            Rectangle {
                visible: root.uiStateRef ? root.uiStateRef.isPreloading : false
                Layout.preferredWidth: 200
                Layout.preferredHeight: 10
                color: "gray"
                border.color: "red"
                border.width: 1

                Rectangle {
                    color: "lightblue"
                    width: parent.width * (root.uiStateRef ? root.uiStateRef.preloadProgress / 100 : 0)
                    height: parent.height
                }
            }
            Rectangle {
                color: (root.uiStateRef && root.uiStateRef.imageCount > 0 && root.uiStateRef.stackInfoText) ? "orange" : "transparent"
                radius: 3
                implicitWidth: stackInfoLabel.implicitWidth + 10
                implicitHeight: stackInfoLabel.implicitHeight + 5
                visible: root.uiStateRef ? (root.uiStateRef.imageCount > 0 && root.uiStateRef.stackInfoText) : false

                Label {
                    id: stackInfoLabel
                    anchors.centerIn: parent
                    text: root.uiStateRef ? `Stack: ${root.uiStateRef.stackInfoText}` : ""
                    color: "black"
                    font.bold: true
                    font.pixelSize: 16
                }
            }
            Rectangle {
                color: (root.uiStateRef && root.uiStateRef.imageCount > 0 && root.uiStateRef.batchInfoText) ? "#4fb360" : "transparent"
                radius: 3
                implicitWidth: batchInfoLabel.implicitWidth + 10
                implicitHeight: batchInfoLabel.implicitHeight + 5
                visible: root.uiStateRef ? (root.uiStateRef.imageCount > 0 && root.uiStateRef.batchInfoText) : false

                Label {
                    id: batchInfoLabel
                    anchors.centerIn: parent
                    text: root.uiStateRef ? `Batch: ${root.uiStateRef.batchInfoText}` : ""
                    color: "white"
                    font.bold: true
                    font.pixelSize: 16
                }
            }
            // Variant badges (loupe view only, when multiple variants exist)
            Row {
                id: variantBadgeRow
                property var badgeItems: root.toArray(root.uiStateRef ? root.uiStateRef.variantBadges : null)

                spacing: 4
                visible: root.uiStateRef
                         && !root.uiStateRef.isGridViewActive
                         && variantBadgeRow.badgeItems.length > 1

                Repeater {
                    model: variantBadgeRow.badgeItems

                    delegate: Rectangle {
                        id: variantBadge
                        required property var modelData

                        width: badgeLabel.implicitWidth + 12
                        height: 22
                        radius: 3
                        color: modelData.active ? "white" : "#555"
                        border.color: modelData.active ? "#333" : "transparent"
                        border.width: modelData.active ? 1 : 0

                        Text {
                            id: badgeLabel
                            anchors.centerIn: parent
                            text: variantBadge.modelData.label
                            font.pixelSize: 11
                            font.bold: true
                            color: variantBadge.modelData.active ? "black" : "white"
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                if (root.uiStateRef) root.uiStateRef.setVariantOverride(variantBadge.modelData.path)
                            }
                        }
                    }
                }

                Label {
                    text: root.uiStateRef ? root.uiStateRef.variantSaveHint : ""
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
                text: root.uiStateRef ? root.uiStateRef.cacheStats : ""
                color: "#00FFFF" // Cyan
                font.family: "Monospace"
                visible: root.uiStateRef ? root.uiStateRef.debugCache : false
                Layout.rightMargin: 10
            }


            // Saturation slider (only visible in saturation mode)
            Row {
                visible: root.uiStateRef && root.uiStateRef.colorMode === "saturation"
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
                    value: root.uiStateRef ? root.uiStateRef.saturationFactor : 1.0
                    stepSize: 0.01
                    width: 150

                    onMoved: {
                        if (root.controllerRef) root.controllerRef.set_saturation_factor(value)
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
                text: root.uiStateRef ? root.uiStateRef.statusMessage : ""
                color: (root.uiStateRef && root.uiStateRef.isSaving) ? "#4CAF50" : root.currentTextColor
                font.bold: (root.uiStateRef && root.uiStateRef.isSaving) ? true : false
                font.pixelSize: (root.uiStateRef && root.uiStateRef.isSaving) ? 14 : 12
                visible: root.uiStateRef ? (root.uiStateRef.statusMessage !== "") : false
                Layout.rightMargin: 10
            }

            // Grid view controls (visible when in grid view) - right side
            Row {
                visible: root.uiStateRef && root.uiStateRef.isGridViewActive
                spacing: 10
                Layout.rightMargin: 15

                // Selection info (uses efficient count property, not full list)
                Label {
                    property int selCount: root.uiStateRef ? root.uiStateRef.gridSelectedCount : 0
                    text: selCount > 0 ? selCount + " selected" : ""
                    color: "#4CAF50"
                    font.bold: true
                    visible: selCount > 0
                    anchors.verticalCenter: parent.verticalCenter
                }

                // Clear selection button
                Rectangle {
                    visible: root.uiStateRef ? root.uiStateRef.gridSelectedCount > 0 : false
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
                        onClicked: { if (root.uiStateRef) root.uiStateRef.gridClearSelection() }
                    }
                }

                // Back button (only shown when there's history)
                Rectangle {
                    visible: root.uiStateRef && root.uiStateRef.gridCanGoBack
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
                        onClicked: { if (root.uiStateRef) root.uiStateRef.gridGoBack() }
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
                        onClicked: { if (root.uiStateRef) root.uiStateRef.gridRefresh() }
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
                        onClicked: { if (root.uiStateRef) root.uiStateRef.toggleGridView() }
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
                          "&nbsp;&nbsp;Right Arrow: Next Image<br>" +
                          "&nbsp;&nbsp;Left Arrow: Previous Image<br>" +
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
                          "&nbsp;&nbsp;Hold Space: Show original with current crop<br>" +
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
                          "&nbsp;&nbsp;Ctrl+Z: Undo last saved action<br><br>" +
                          "<b>Image Editing:</b><br>" +
                          "&nbsp;&nbsp;E: Toggle Image Editor<br>" +
                          "&nbsp;&nbsp;Ctrl+S (in editor): Save current live edits<br>" +
                          "<b>&nbsp;&nbsp;Compact Editor (when focused):</b><br>" +
                          "&nbsp;&nbsp;&nbsp;&nbsp;Left / Right: Previous / Next image<br>" +
                          "&nbsp;&nbsp;&nbsp;&nbsp;Up / Down: Raise / lower the highlighted slider<br>" +
                          "&nbsp;&nbsp;&nbsp;&nbsp;Click a slider label: Highlight it for Up/Down<br>" +
                          "&nbsp;&nbsp;&nbsp;&nbsp;S: Save&nbsp;&nbsp;E / Esc: Close&nbsp;&nbsp;O: Crop<br>" +
                          "&nbsp;&nbsp;&nbsp;&nbsp;B, F, D, I, etc. work as in the main view<br>" +
                          "&nbsp;&nbsp;A: Quick auto white balance (live)<br>" +
                          "&nbsp;&nbsp;l: Quick auto levels + vibrance (live)<br>" +
                          "&nbsp;&nbsp;L: Quick auto white balance + auto levels + vibrance (live)<br>" +
                          "&nbsp;&nbsp;-: Darken current auto-adjust highlights/whites (live)<br>" +
                          "&nbsp;&nbsp;_: Raise current auto-adjust whites (live)<br>" +
                          "&nbsp;&nbsp;+: Raise current auto-adjust shadows/blacks (live)<br>" +
                          "&nbsp;&nbsp;=: Deepen current auto-adjust shadows/background (live)<br>" +
                          "&nbsp;&nbsp;K: Background Darkening Tool<br>" +
                          "&nbsp;&nbsp;O (or right-click): Toggle crop mode<br>" +
                          "&nbsp;&nbsp;&nbsp;&nbsp;1/2/3/4: Set aspect ratio (1:1, 4:3, 3:2, 16:9)<br>" +
                          "&nbsp;&nbsp;&nbsp;&nbsp;Enter: Apply crop to live session<br>" +
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
            text: (root.uiStateRef && root.uiStateRef.stackSummary) ? root.uiStateRef.stackSummary : "No stacks defined."
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
            if (root.uiStateRef) root.uiStateRef.applyFilter(filterString, filterFlags)
        }
    }

    JumpToImageDialog {
        id: jumpToImageDialog
        backgroundColor: root.currentBackgroundColor
        textColor: root.currentTextColor
        maxImageCount: root.uiStateRef ? root.uiStateRef.imageCount : 0
    }

    DeleteBatchDialog {
        id: deleteBatchDialog
        backgroundColor: root.currentBackgroundColor
        textColor: root.currentTextColor
    }

    QuitBatchesDialog {
        id: quitBatchesDialog
        backgroundColor: root.currentBackgroundColor
        textColor: root.currentTextColor
        darkTheme: root.isDarkTheme
        frameBorderColor: root.isDarkTheme ? "#404040" : "#d0d0d0"
        cancelBgColor: root.isDarkTheme ? "#444444" : "#f0f0f0"
        cancelHoverColor: root.isDarkTheme ? "#666666" : "#e0e0e0"
        cancelPressedColor: root.isDarkTheme ? "#555555" : "#d0d0d0"
        quitBgColor: root.isDarkTheme ? "#aa0000" : "#c62828"
        quitHoverColor: root.isDarkTheme ? "#ff0000" : "#d32f2f"
        quitPressedColor: root.isDarkTheme ? "#cc0000" : "#b71c1c"
        controllerRef: root.controllerRef
        onQuitConfirmed: {
            root.allowCloseWithBatches = true
            Qt.quit()
        }
    }
    
    HistogramWindow {
        id: histogramWindow
        windowBackgroundColor: root.currentBackgroundColor
        primaryTextColor: root.currentTextColor
        gridLineColor: root.isDarkTheme ? "#454545" : "#dcdcdc"
    }

    CompactEditorWindow {
        id: compactEditorWindow
        onVisibleChanged: {
            if (!visible) {
                mainViewLoader.forceActiveFocus()
            }
        }
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

    DarkenToolPanel {
        id: darkenToolPanel
    }

    function show_jump_to_image_dialog() {
        root.openDialogSafely(jumpToImageDialog)
    }

    function show_delete_batch_dialog(count) {
        deleteBatchDialog.batchCount = count
        root.openDialogSafely(deleteBatchDialog)
    }

    ExifDialog {
        id: exifDialog
        backgroundColor: root.currentBackgroundColor
        textColor: root.currentTextColor
    }

    ColorInfoDialog {
        id: colorInfoDialog
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
        visible: root.uiStateRef ? (root.uiStateRef.debugCache && root.uiStateRef.isDecoding) : false
        
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
        property var binInfoItems: root.toArray(binInfo)
        property var restorableBins: root.itemsWithStatus(binInfoItems, "restorable")
        property var unavailableBins: root.itemsWithStatus(binInfoItems, "unavailable")

        function refreshBinInfo() {
            if (root.uiStateRef) {
                binInfo = root.uiStateRef.getPerBinRestoreInfo()
            }
        }

        onOpened: refreshBinInfo()
        onClosed: {
            if (!root.allowCloseWithRecycleBins) {
                root.allowCloseWithBatches = false
            }
        }

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
                text: root.uiStateRef ? root.uiStateRef.recycleBinStatsText : "Loading..."
                color: root.isDarkTheme ? "#efefef" : "#333333"
                wrapMode: Text.WordWrap
                font.pixelSize: 15
                lineHeight: 1.3
            }

            // ---- Per-bin restore rows (restorable bins only) ----
            Repeater {
                id: restorableRepeater
                model: recycleBinCleanupDialog.restorableBins

                delegate: Rectangle {
                    id: restorableBin
                    required property var modelData

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
                                text: restorableBin.modelData.label
                                color: root.isDarkTheme ? "#efefef" : "#333333"
                                font.pixelSize: 14
                                font.bold: true
                                elide: Text.ElideMiddle
                                width: parent.width
                            }
                            Label {
                                text: restorableBin.modelData.dest_dir
                                color: root.isDarkTheme ? "#888888" : "#999999"
                                font.pixelSize: 11
                                elide: Text.ElideMiddle
                                width: parent.width
                            }
                            Label {
                                text: {
                                    var parts = []
                                    if (restorableBin.modelData.jpg_count > 0) parts.push(restorableBin.modelData.jpg_count + " JPG")
                                    if (restorableBin.modelData.raw_count > 0) parts.push(restorableBin.modelData.raw_count + " RAW")
                                    if (restorableBin.modelData.other_count > 0) parts.push(restorableBin.modelData.other_count + " other")
                                    var s = parts.join(", ")
                                    if (restorableBin.modelData.legacy_count > 0)
                                        s += " + " + restorableBin.modelData.legacy_count + " legacy"
                                    return s + " \u2014 " + restorableBin.modelData.total_restorable + " restorable"
                                }
                                color: root.isDarkTheme ? "#aaaaaa" : "#666666"
                                font.pixelSize: 13
                            }
                        }

                        // Per-bin Restore button
                        Rectangle {
                            Layout.preferredWidth: restoreBinBtnText.implicitWidth + 30
                            Layout.preferredHeight: 34
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
                                    if (root.uiStateRef) {
                                        root.uiStateRef.restoreSingleBin(restorableBin.modelData.bin_path)
                                        recycleBinCleanupDialog.refreshBinInfo()
                                        // Auto-close if nothing left
                                        if (recycleBinCleanupDialog.binInfoItems.length === 0) {
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
                visible: recycleBinCleanupDialog.unavailableBins.length > 0

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
                    model: recycleBinCleanupDialog.unavailableBins

                    delegate: Label {
                        id: unavailableBin
                        required property var modelData

                        width: dialogContent.width - 40
                        text: unavailableBin.modelData.dest_dir + " \u2014 " + unavailableBin.modelData.total_files + " file" + (unavailableBin.modelData.total_files !== 1 ? "s" : "")
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
                        text: root.uiStateRef ? root.uiStateRef.recycleBinDetailedText : ""
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
                        onClicked: {
                            root.allowCloseWithBatches = false
                            recycleBinCleanupDialog.close()
                        }
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
                            root.allowCloseWithRecycleBins = true
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
                            if (root.uiStateRef) root.uiStateRef.cleanupRecycleBins()
                            root.allowCloseWithRecycleBins = true
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
