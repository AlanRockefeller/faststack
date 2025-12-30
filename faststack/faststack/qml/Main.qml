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
    title: "FastStack - " + (uiState ? uiState.currentDirectory : "Loading...")

    Component.onCompleted: {
        // Initialization complete
    }

    Material.theme: (uiState && uiState.theme === 0) ? Material.Dark : Material.Light
    Material.accent: "#4fb360"

    property bool isDarkTheme: uiState ? uiState.theme === 0 : true
    property color currentBackgroundColor: isDarkTheme ? "#2b2b2b" : "#ffffff"
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

    Connections {
        target: uiState
        function onThemeChanged() {
            root.isDarkTheme = uiState.theme === 0
        }
    }

    // -------- FLOATING MENU BAR (overlays content) --------
    Rectangle {
        id: floatingMenuBar
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: 40
        color: "transparent"
        z: 100  // Ensure it's above the content

        // Unified "menu active" flag to avoid flashing
        property bool menuActive: menuBarMouseArea.containsMouse
                                  || fileMouseArea.containsMouse
                                  || viewMouseArea.containsMouse
                                  || actionsMouseArea.containsMouse
                                  || helpMouseArea.containsMouse
                                  || fileMenu.visible
                                  || viewMenu.visible
                                  || actionsMenu.visible
                                  || helpMenu.visible

        // Semi-transparent background that appears on hover
        Rectangle {
            anchors.fill: parent
            color: root.isDarkTheme ? "#333333" : "#f0f0f0"
            opacity: floatingMenuBar.menuActive ? 0.9 : 0.0

            Behavior on opacity {
                NumberAnimation { duration: 150 }
            }
        }

        MouseArea {
            id: menuBarMouseArea
            anchors.fill: parent
            hoverEnabled: true
            propagateComposedEvents: true

            // Don't block clicks - let them pass through to children
            onClicked: function(mouse) { mouse.accepted = false }
            onPressed: function(mouse) { mouse.accepted = false }
            onReleased: function(mouse) { mouse.accepted = false }
        }

        Row {
            id: menuButtonRow
            anchors.left: parent.left
            anchors.leftMargin: 8
            anchors.verticalCenter: parent.verticalCenter
            spacing: 4

            // Show whenever any menu is hovered or open
            visible: floatingMenuBar.menuActive

            // FILE MENU BUTTON
            Rectangle {
                id: fileBtn
                width: fileLabel.width + 20
                height: 30
                color: fileMouseArea.containsMouse ? hoverColor : "transparent"
                radius: 4

                Text {
                    id: fileLabel
                    anchors.centerIn: parent
                    text: "File"
                    color: root.currentTextColor
                    font.pixelSize: 14
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
                width: viewLabel.width + 20
                height: 30
                color: viewMouseArea.containsMouse ? hoverColor : "transparent"
                radius: 4

                Text {
                    id: viewLabel
                    anchors.centerIn: parent
                    text: "View"
                    color: root.currentTextColor
                    font.pixelSize: 14
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
                width: actionsLabel.width + 20
                height: 30
                color: actionsMouseArea.containsMouse ? hoverColor : "transparent"
                radius: 4

                Text {
                    id: actionsLabel
                    anchors.centerIn: parent
                    text: "Actions"
                    color: root.currentTextColor
                    font.pixelSize: 14
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
                width: helpLabel.width + 20
                height: 30
                color: helpMouseArea.containsMouse ? hoverColor : "transparent"
                radius: 4

                Text {
                    id: helpLabel
                    anchors.centerIn: parent
                    text: "Help"
                    color: root.currentTextColor
                    font.pixelSize: 14
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
                    if (uiState) {
                        settingsDialog.heliconPath      = uiState.get_helicon_path()
                        settingsDialog.photoshopPath    = uiState.get_photoshop_path()
                        settingsDialog.cacheSize        = uiState.get_cache_size()
                        settingsDialog.prefetchRadius   = uiState.get_prefetch_radius()
                        settingsDialog.theme            = uiState.theme
                        settingsDialog.defaultDirectory = uiState.get_default_directory()
                        settingsDialog.optimizeFor       = uiState.get_optimize_for()
                        settingsDialog.awbMode          = uiState.awbMode
                        settingsDialog.awbStrength      = uiState.awbStrength
                        settingsDialog.awbWarmBias      = uiState.awbWarmBias
                        settingsDialog.awbLumaLowerBound = uiState.awbLumaLowerBound
                        settingsDialog.awbLumaUpperBound = uiState.awbLumaUpperBound
                        settingsDialog.awbRgbLowerBound = uiState.awbRgbLowerBound
                        settingsDialog.awbRgbUpperBound = uiState.awbRgbUpperBound
                    }
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
                text: "Run Stacks"
                onClicked: { if (uiState) uiState.launch_helicon(); actionsMenu.close() }
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

    // -------- MAIN VIEW --------
    Item {
        id: contentArea
        anchors.fill: parent

        Loader {
            id: mainViewLoader
            anchors.fill: parent
            source: "Components.qml"
            focus: true

            // Key bindings implemented in old Main.qml
            Keys.onPressed: function(event) {
                if (!uiState || !controller) {
                    return
                }

                // Toggle Image Editor with 'E' key
                // If editor is open, close it without saving. Otherwise open it.
                if (event.key === Qt.Key_E && !event.isAutoRepeat) {
                    if (uiState.isEditorOpen) {
                        // Close editor without saving
                        uiState.isEditorOpen = false
                    } else {
                        // Open editor
                        uiState.isEditorOpen = true
                        if (controller) {
                            controller.load_image_for_editing()
                        }
                    }
                    event.accepted = true
                }
                // Global Key for saving edited image (Ctrl+S) when editor is open
                else if (event.key === Qt.Key_S && (event.modifiers & Qt.ControlModifier)) {
                    if (uiState.isEditorOpen) {
                        controller.save_edited_image()
                        event.accepted = true
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
        property int fixedHeight: 60
        height: fixedHeight
        implicitHeight: fixedHeight
        anchors.left: parent.left
        anchors.right: parent.right
        color: Qt.rgba(root.currentBackgroundColor.r, root.currentBackgroundColor.g, root.currentBackgroundColor.b, 0.8)
        clip: true

        RowLayout {
            id: footerRow
            spacing: 10
            anchors.verticalCenter: parent.verticalCenter

            Label {
                Layout.leftMargin: 10
                text: uiState ? `Image: ${uiState.currentIndex + 1} / ${uiState.imageCount}` : "Image: - / -"
                color: root.currentTextColor
            }
            Label {
                text: (uiState && uiState.imageCount > 0)
                      ? ` | File: ${uiState.currentFilename || 'N/A'}`
                      : " | File: N/A"
                color: root.currentTextColor
            }
            Label {
                text: uiState ? ` | Stacked: ${uiState.stackedDate}` : ""
                color: "lightgreen"
                visible: uiState ? (uiState.imageCount > 0 && uiState.isStacked) : false
            }
            Label {
                text: uiState ? ` | Uploaded on ${uiState.uploadedDate}` : ""
                color: "lightgreen"
                visible: uiState ? (uiState.imageCount > 0 && uiState.isUploaded) : false
            }
            Label {
                text: uiState ? ` | Edited on ${uiState.editedDate}` : ""
                color: "lightgreen"
                visible: uiState ? (uiState.imageCount > 0 && uiState.isEdited) : false
            }
            Label {
                text: uiState ? ` | Filter: "${uiState.filterString}"` : ""
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
        width: 600
        height: 750

        background: Rectangle {
            color: root.currentBackgroundColor
        }

        contentItem: ScrollView {
            clip: true
            Text {
                text: "<b>FastStack Keyboard and Mouse Commands</b><br><br>" +
                      "<b>Navigation:</b><br>" +
                      "&nbsp;&nbsp;J / Right Arrow: Next Image<br>" +
                      "&nbsp;&nbsp;K / Left Arrow: Previous Image<br>" +
                      "&nbsp;&nbsp;G: Jump to Image Number<br>" +
                      "&nbsp;&nbsp;I: Show EXIF Data<br><br>" +
                      "<b>Viewing:</b><br>" +
                      "&nbsp;&nbsp;Mouse Wheel: Zoom in/out<br>" +
                      "&nbsp;&nbsp;Left-click + Drag: Pan image<br>" +
                      "&nbsp;&nbsp;Ctrl+0: Reset zoom and pan to fit window<br><br>" +
                      "<b>Stacking:</b><br>" +
                      "&nbsp;&nbsp;[: Begin new stack<br>" +
                      "&nbsp;&nbsp;]: End current stack<br>" +
                      "&nbsp;&nbsp;C: Clear all stacks<br><br>" +
                      "<b>Batch Selection (for drag-and-drop):</b><br>" +
                      "&nbsp;&nbsp;{: Begin new batch<br>" +
                      "&nbsp;&nbsp;}: End current batch<br>" +
                      "&nbsp;&nbsp;\\: Clear all batches<br>" +
                      "&nbsp;&nbsp;X or S: Remove current image from batch/stack<br><br>" +
                      "<b>Flag Toggles:</b><br>" +
                      "&nbsp;&nbsp;U: Toggle uploaded flag<br>" +
                      "&nbsp;&nbsp;Ctrl+E: Toggle edited flag<br>" +
                      "&nbsp;&nbsp;Ctrl+S: Toggle stacked flag<br><br>" +
                      "<b>File Management:</b><br>" +
                      "&nbsp;&nbsp;Delete: Move current image to recycle bin<br>" +
                      "&nbsp;&nbsp;Ctrl+Z: Undo last action (delete, auto white balance, or crop)<br><br>" +
                      "<b>Actions:</b><br>" +
                      "&nbsp;&nbsp;Enter: Launch Helicon Focus<br>" +
                      "&nbsp;&nbsp;P: Edit in Photoshop<br>" +
                      "&nbsp;&nbsp;A: Quick auto white balance (saves automatically)<br>" +
                      "&nbsp;&nbsp;Ctrl+Shift+B: Quick auto white balance (saves automatically)<br>" +
                      "&nbsp;&nbsp;O: Toggle crop mode (Enter to execute crop, ESC to cancel)<br>" +
                      "&nbsp;&nbsp;H: Toggle histogram window<br>" +
                      "&nbsp;&nbsp;E: Toggle Image Editor (closes without saving if open)<br>" +
                      "&nbsp;&nbsp;Ctrl+C: Copy image path to clipboard"
                padding: 10
                wrapMode: Text.WordWrap
                color: root.currentTextColor
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
            if (uiState) uiState.applyFilter(filterString)
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
}
