pragma ComponentBehavior: Bound

import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import QtCore

Window {
    id: compactEditor
    width: 320
    height: Screen.desktopAvailableHeight * 0.85
    minimumWidth: 280
    maximumWidth: 400
    minimumHeight: 500
    title: "Image Editor"
    flags: Qt.Tool | Qt.WindowTitleHint | Qt.WindowCloseButtonHint

    property var uiStateRef: null
    property var controllerRef: null

    visible: compactEditor.uiStateRef
             ? (compactEditor.uiStateRef.isEditorOpen && !compactEditor.uiStateRef.isEditorExpanded)
             : false

    Settings {
        id: compactSettings
        category: "compactEditor"
        property bool overlaidHistogram: true
        property real savedX: -1
        property real savedY: -1
    }

    Component.onCompleted: {
        compactEditor.uiStateRef = uiState
        compactEditor.controllerRef = controller
        if (compactSettings.savedX >= 0 && compactSettings.savedY >= 0) {
            compactEditor.x = compactSettings.savedX
            compactEditor.y = compactSettings.savedY
        } else {
            positionAtRightGutter()
        }
        compactEditor.keyboardHandlerReady = true
        Qt.callLater(compactEditor.focusKeyboardHandler)
    }

    function positionAtRightGutter() {
        var mainWin = Application.windows[0]
        if (mainWin) {
            compactEditor.x = mainWin.x + mainWin.width - compactEditor.width - 10
            compactEditor.y = mainWin.y + 40
        }
    }

    onXChanged: if (visible) compactSettings.savedX = x
    onYChanged: if (visible) compactSettings.savedY = y
    onActiveChanged: if (active) compactEditor.focusKeyboardHandler()

    // --- Color Palette (matches full editor) ---
    readonly property color backgroundColor: "#1e1e1e"
    readonly property color textColor: "white"
    readonly property color accentColor: "#6366f1"
    readonly property color accentColorHover: "#818cf8"
    readonly property color controlBg: "#10ffffff"
    readonly property color controlBorder: "#30ffffff"
    readonly property color separatorColor: "#20ffffff"
    readonly property color mutedText: "#6b6764"

    color: compactEditor.backgroundColor
    Material.theme: Material.Dark
    Material.accent: compactEditor.accentColor

    property int updatePulse: 0
    readonly property bool cropActive: compactEditor.uiStateRef ? compactEditor.uiStateRef.isCropping : false
    property int lastLoadedIndex: -1
    property string closeTooltip: "Close editor"

    // Key of the slider that the Up/Down arrow keys will adjust. The matching
    // row is highlighted so the user can see which control is targeted. Click a
    // slider's label (or value) to retarget. Defaults to the first slider so the
    // arrow keys work immediately when the editor opens.
    property string highlightedSliderKey: "exposure"
    property bool keyboardHandlerReady: false

    function focusKeyboardHandler() {
        if (compactEditor.visible && compactEditor.keyboardHandlerReady) keyScope.forceActiveFocus()
    }

    // Adjust the currently highlighted slider by `delta` slider-space steps
    // (the sliders all run -100..100). Driven by the Up/Down arrow keys.
    function adjustHighlightedSlider(delta) {
        if (compactEditor.cropActive) return
        if (!compactEditor.controllerRef || !compactEditor.highlightedSliderKey) return
        compactEditor.ensureEditorLoaded()
        var key = compactEditor.highlightedSliderKey
        var scale = compactEditor.sliderEditScale(key)
        // Convert the stored edit-space value into slider space, step it, clamp,
        // then convert back to edit space the way the sliders do.
        var sliderVal = compactEditor.getBackendValue(key) / scale * 100
        sliderVal = Math.max(-100, Math.min(100, sliderVal + delta))
        compactEditor.controllerRef.set_edit_parameter(key, sliderVal / 100 * scale)
        compactEditor.updatePulse++  // refreshes sliders + histogram
    }

    function refreshCloseTooltip() {
        if (compactEditor.controllerRef && compactEditor.controllerRef.has_unsaved_edits())
            compactEditor.closeTooltip = "Discard unsaved edits and close"
        else
            compactEditor.closeTooltip = "Close editor"
    }

    function requestClose() {
        if (compactEditor.cropActive) {
            if (compactEditor.controllerRef) compactEditor.controllerRef.cancel_crop_mode()
            return
        }
        if (compactEditor.controllerRef && compactEditor.controllerRef.has_unsaved_edits()) {
            if (!discardDialog.opened) discardDialog.open()
        } else {
            if (compactEditor.uiStateRef) compactEditor.uiStateRef.isEditorOpen = false
        }
    }

    function handleArrowKey(key) {
        if (compactEditor.cropActive || discardDialog.opened) return false
        if (key === Qt.Key_Left || key === Qt.Key_Right) {
            if (compactEditor.controllerRef)
                compactEditor.controllerRef.handle_key_from_compact_editor(key, Qt.NoModifier, "")
            return true
        }
        if (key === Qt.Key_Up) {
            compactEditor.adjustHighlightedSlider(1)
            return true
        }
        if (key === Qt.Key_Down) {
            compactEditor.adjustHighlightedSlider(-1)
            return true
        }
        return false
    }

    Timer {
        id: deferredLoadTimer
        interval: 200
        repeat: false
        onTriggered: {
            if (!compactEditor.visible || !compactEditor.controllerRef || !compactEditor.uiStateRef) return
            var idx = compactEditor.uiStateRef.currentIndex
            if (idx === compactEditor.lastLoadedIndex) return
            compactEditor.lastLoadedIndex = idx
            compactEditor.controllerRef.load_image_for_editing()
            compactEditor.controllerRef.update_histogram()
            compactEditor.updatePulse++
        }
    }

    function ensureEditorLoaded() {
        if (!compactEditor.controllerRef || !compactEditor.uiStateRef) return
        var idx = compactEditor.uiStateRef.currentIndex
        if (idx !== compactEditor.lastLoadedIndex) {
            deferredLoadTimer.stop()
            compactEditor.lastLoadedIndex = idx
            compactEditor.controllerRef.load_image_for_editing()
            compactEditor.controllerRef.update_histogram()
            compactEditor.updatePulse++
        }
    }

    Connections {
        target: compactEditor.uiStateRef
        function onCurrentIndexChanged() {
            if (!compactEditor.visible) return
            deferredLoadTimer.restart()
        }
    }

    onVisibleChanged: {
        if (visible && compactEditor.controllerRef) {
            ensureEditorLoaded()
            if (compactSettings.savedX < 0) positionAtRightGutter()
            Qt.callLater(compactEditor.focusKeyboardHandler)
        }
    }

    onUpdatePulseChanged: {
        if (visible && compactEditor.controllerRef) {
            compactEditor.controllerRef.update_histogram()
        }
    }

    property int slidersPressedCount: 0
    onSlidersPressedCountChanged: {
        if (compactEditor.uiStateRef) compactEditor.uiStateRef.setAnySliderPressed(slidersPressedCount > 0)
    }

    function getBackendValue(key) {
        var _dependency = updatePulse;
        if (compactEditor.uiStateRef && key in compactEditor.uiStateRef) return compactEditor.uiStateRef[key];
        return 0.0;
    }

    function sliderEditScale(key) {
        if (key === "contrast") return 0.5
        return (key === "exposure" || key === "whites") ? 2.0 : 1.0
    }

    Shortcut {
        sequence: "Left"
        context: Qt.WindowShortcut
        enabled: compactEditor.visible && !compactEditor.cropActive && !discardDialog.opened
        onActivated: compactEditor.handleArrowKey(Qt.Key_Left)
    }

    Shortcut {
        sequence: "Right"
        context: Qt.WindowShortcut
        enabled: compactEditor.visible && !compactEditor.cropActive && !discardDialog.opened
        onActivated: compactEditor.handleArrowKey(Qt.Key_Right)
    }

    Shortcut {
        sequence: "Up"
        context: Qt.WindowShortcut
        enabled: compactEditor.visible && !compactEditor.cropActive && !discardDialog.opened
        onActivated: compactEditor.adjustHighlightedSlider(1)
    }

    Shortcut {
        sequence: "Down"
        context: Qt.WindowShortcut
        enabled: compactEditor.visible && !compactEditor.cropActive && !discardDialog.opened
        onActivated: compactEditor.adjustHighlightedSlider(-1)
    }

    onClosing: (close) => {
        if (compactEditor.uiStateRef && compactEditor.controllerRef) {
            close.accepted = false
            compactEditor.requestClose()
        }
    }

    // Keyboard handling for the compact editor window.
    //
    // Arrow keys are handled by WindowShortcut entries above so they still work
    // when a child control has focus:
    //   - Left / Right     -> previous / next image
    //   - Up / Down        -> raise / lower the highlighted slider
    //
    // This focus scope handles the remaining compact-editor keys:
    //   - Esc / E / S / O  -> editor-local actions (close / save / crop)
    //   - everything else  -> forwarded to the main window key bindings so
    //                         keys like B (batch), F, D, I, etc. still work
    //                         while the editor is focused.
    FocusScope {
        id: keyScope
        anchors.fill: parent
        focus: compactEditor.visible

        Keys.onPressed: function(event) {
            if (compactEditor.cropActive) {
                if (event.key === Qt.Key_Escape) {
                    if (compactEditor.controllerRef) compactEditor.controllerRef.cancel_crop_mode()
                    event.accepted = true
                    return
                } else if (event.key === Qt.Key_Enter || event.key === Qt.Key_Return) {
                    if (compactEditor.controllerRef) compactEditor.controllerRef.execute_crop()
                    event.accepted = true
                    return
                } else if (event.key === Qt.Key_O) {
                    if (compactEditor.controllerRef) compactEditor.controllerRef.toggle_crop_mode()
                    event.accepted = true
                    return
                } else if (event.key === Qt.Key_S) {
                    if (compactEditor.uiStateRef) compactEditor.uiStateRef.statusMessage = "Apply or cancel the crop before saving"
                    event.accepted = true
                    return
                } else if (event.key === Qt.Key_Left || event.key === Qt.Key_Right || event.key === Qt.Key_Up || event.key === Qt.Key_Down) {
                    event.accepted = true
                    return
                }
            }

            if (compactEditor.handleArrowKey(event.key)) {
                event.accepted = true
            } else if (event.key === Qt.Key_Escape) {
                compactEditor.requestClose()
                event.accepted = true
            } else if (event.key === Qt.Key_E && !(event.modifiers & Qt.ControlModifier)) {
                compactEditor.requestClose()
                event.accepted = true
            } else if (event.key === Qt.Key_S) {
                // S or Ctrl+S both save the live edits from the compact editor.
                compactEditor.ensureEditorLoaded()
                if (compactEditor.uiStateRef && !compactEditor.uiStateRef.isSaving && compactEditor.controllerRef)
                    compactEditor.controllerRef.save_edited_image()
                event.accepted = true
            } else {
                // Forward every other key (B, F, D, I, G, etc.) to the main
                // window's key bindings.
                if (compactEditor.controllerRef)
                    compactEditor.controllerRef.handle_key_from_compact_editor(event.key, event.modifiers, event.text)
                event.accepted = true
            }
        }
    }

    // Discard confirmation dialog
    Dialog {
        id: discardDialog
        title: "Discard Edits?"
        modal: true
        anchors.centerIn: parent
        width: 260
        standardButtons: Dialog.Yes | Dialog.No

        Label {
            text: "You have unsaved edits.\nDiscard and close?"
            wrapMode: Text.WordWrap
        }

        onAccepted: {
            if (compactEditor.controllerRef) compactEditor.controllerRef.discard_edit_parameters()
            if (compactEditor.uiStateRef) compactEditor.uiStateRef.isEditorOpen = false
        }
    }

    ScrollView {
        id: editorScroll
        anchors.fill: parent
        anchors.margins: 8
        rightPadding: editorScroll.ScrollBar.vertical.visible ? editorScroll.ScrollBar.vertical.width + 4 : 0
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: ScrollBar.AsNeeded
        contentWidth: editorScroll.availableWidth

        ColumnLayout {
            width: editorScroll.availableWidth
            spacing: 6

            // --- Header ---
            RowLayout {
                Layout.fillWidth: true
                spacing: 6

                Item { Layout.fillWidth: true }

                // Expand button
                Button {
                    id: expandBtn
                    implicitWidth: 26; implicitHeight: 26
                    Layout.minimumWidth: 26; Layout.preferredWidth: 26; Layout.maximumWidth: 26
                    Layout.minimumHeight: 26; Layout.preferredHeight: 26; Layout.maximumHeight: 26
                    enabled: !compactEditor.cropActive
                    padding: 0
                    flat: true
                    onClicked: {
                        if (compactEditor.uiStateRef) compactEditor.uiStateRef.isEditorExpanded = true
                    }
                    contentItem: Text {
                        text: "⤢"
                        font.pixelSize: 16
                        color: compactEditor.textColor
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: 4
                        color: expandBtn.hovered ? "#30ffffff" : "transparent"
                    }
                }

                // Close/discard button
                Button {
                    id: discardBtn
                    implicitWidth: 26; implicitHeight: 26
                    Layout.minimumWidth: 26; Layout.preferredWidth: 26; Layout.maximumWidth: 26
                    Layout.minimumHeight: 26; Layout.preferredHeight: 26; Layout.maximumHeight: 26
                    padding: 0
                    flat: true
                    ToolTip.visible: hovered
                    ToolTip.delay: 500
                    ToolTip.text: compactEditor.closeTooltip
                    onHoveredChanged: if (hovered) compactEditor.refreshCloseTooltip()
                    onClicked: compactEditor.requestClose()
                    contentItem: Text {
                        text: "✕"
                        font.pixelSize: 13
                        color: discardBtn.hovered ? "#ff6060" : compactEditor.mutedText
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: 4
                        color: discardBtn.hovered ? "#40ff4040" : "transparent"
                    }
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: compactEditor.separatorColor }

            // --- Histogram Section ---
            RowLayout {
                Layout.fillWidth: true
                spacing: 4

                Text {
                    text: "HISTOGRAM"
                    font.pixelSize: 9
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1.0
                    color: "#9a9795"
                }

                Item { Layout.fillWidth: true }

                // Tiny toggle for histogram mode
                Row {
                    spacing: 0

                    Rectangle {
                        width: 52; height: 16
                        radius: 2
                        color: compactSettings.overlaidHistogram ? "#2c2c2c" : "transparent"
                        border.color: "#3a3a3a"; border.width: 1

                        Text {
                            anchors.centerIn: parent
                            text: "Overlay"
                            font.pixelSize: 8
                            color: compactSettings.overlaidHistogram ? "#e8e6e3" : "#6b6764"
                        }

                        MouseArea {
                            anchors.fill: parent
                            onClicked: compactSettings.overlaidHistogram = true
                        }
                    }

                    Rectangle {
                        width: 48; height: 16
                        radius: 2
                        color: !compactSettings.overlaidHistogram ? "#2c2c2c" : "transparent"
                        border.color: "#3a3a3a"; border.width: 1

                        Text {
                            anchors.centerIn: parent
                            text: "R G B"
                            font.pixelSize: 8
                            color: !compactSettings.overlaidHistogram ? "#e8e6e3" : "#6b6764"
                        }

                        MouseArea {
                            anchors.fill: parent
                            onClicked: compactSettings.overlaidHistogram = false
                        }
                    }
                }
            }

            // Overlaid histogram
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: compactSettings.overlaidHistogram ? 100 : 0
                visible: compactSettings.overlaidHistogram

                OverlaidHistogram {
                    anchors.fill: parent
                    rData: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["r"] || []) : []
                    gData: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["g"] || []) : []
                    bData: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["b"] || []) : []
                    rClip: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["r_clip"] || 0) : 0
                    gClip: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["g_clip"] || 0) : 0
                    bClip: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["b_clip"] || 0) : 0
                    rPreClip: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["r_preclip"] || 0) : 0
                    gPreClip: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["g_preclip"] || 0) : 0
                    bPreClip: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["b_preclip"] || 0) : 0
                    gridLineColor: compactEditor.controlBorder
                }
            }

            // Stacked channel histograms (vertical stack for narrow panel)
            ColumnLayout {
                Layout.fillWidth: true
                visible: !compactSettings.overlaidHistogram
                spacing: 2

                SingleChannelHistogram {
                    Layout.fillWidth: true; Layout.preferredHeight: 50
                    channelName: "Red"; channelColor: "#e15050"
                    gridLineColor: compactEditor.controlBorder
                    dangerColor: "#40ff0000"; textColor: compactEditor.textColor; minimal: true
                    histogramData: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["r"] || []) : []
                    clipCount: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["r_clip"] || 0) : 0
                    preClipCount: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["r_preclip"] || 0) : 0
                }
                SingleChannelHistogram {
                    Layout.fillWidth: true; Layout.preferredHeight: 50
                    channelName: "Green"; channelColor: "#50e150"
                    gridLineColor: compactEditor.controlBorder
                    dangerColor: "#40ff0000"; textColor: compactEditor.textColor; minimal: true
                    histogramData: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["g"] || []) : []
                    clipCount: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["g_clip"] || 0) : 0
                    preClipCount: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["g_preclip"] || 0) : 0
                }
                SingleChannelHistogram {
                    Layout.fillWidth: true; Layout.preferredHeight: 50
                    channelName: "Blue"; channelColor: "#5050e1"
                    gridLineColor: compactEditor.controlBorder
                    dangerColor: "#40ff0000"; textColor: compactEditor.textColor; minimal: true
                    histogramData: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["b"] || []) : []
                    clipCount: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["b_clip"] || 0) : 0
                    preClipCount: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData["b_preclip"] || 0) : 0
                }
            }

            // Clip stats (only in channel mode; overlay mode has built-in stats)
            RowLayout {
                Layout.fillWidth: true
                spacing: 4
                visible: !compactSettings.overlaidHistogram

                Repeater {
                    model: [
                        { label: "R", preKey: "r_preclip", clipKey: "r_clip", dimColor: "#804040", hotColor: "#ff6060" },
                        { label: "G", preKey: "g_preclip", clipKey: "g_clip", dimColor: "#407040", hotColor: "#60ff60" },
                        { label: "B", preKey: "b_preclip", clipKey: "b_clip", dimColor: "#404080", hotColor: "#8080ff" },
                    ]

                    delegate: Column {
                        required property var modelData
                        Layout.fillWidth: true
                        spacing: 0

                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: modelData.label + " pre:" + (compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData[modelData.preKey] || 0) : 0)
                            font.pixelSize: 8; font.family: "IBM Plex Mono"
                            color: modelData.dimColor
                        }
                        Text {
                            property int clipVal: compactEditor.uiStateRef && compactEditor.uiStateRef.histogramData ? (compactEditor.uiStateRef.histogramData[modelData.clipKey] || 0) : 0
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "clip:" + clipVal
                            font.pixelSize: 8; font.family: "IBM Plex Mono"
                            font.bold: clipVal > 0
                            color: clipVal > 0 ? modelData.hotColor : modelData.dimColor
                        }
                    }
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: compactEditor.separatorColor; Layout.topMargin: 2 }

            // --- LIGHT Section ---
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 2
                spacing: 4

                Text {
                    text: "LIGHT"
                    font.pixelSize: 9
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1.0
                    color: "#9a9795"
                }

                Item { Layout.fillWidth: true }

                Button {
                    id: autoLevelsBtn
                    implicitWidth: 36; implicitHeight: 16
                    Layout.minimumWidth: 36; Layout.preferredWidth: 36; Layout.maximumWidth: 36
                    Layout.minimumHeight: 16; Layout.preferredHeight: 16; Layout.maximumHeight: 16
                    enabled: !compactEditor.cropActive
                    padding: 0
                    flat: true
                    onClicked: {
                        compactEditor.ensureEditorLoaded()
                        if (compactEditor.controllerRef) compactEditor.controllerRef.auto_levels()
                        compactEditor.updatePulse++
                    }
                    contentItem: Text {
                        text: "Auto"
                        font.pixelSize: 8
                        color: autoLevelsBtn.hovered ? "#e8e6e3" : "#9a9795"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: 2
                        color: autoLevelsBtn.hovered ? "#30ffffff" : "#18ffffff"
                        border.color: "#3a3a3a"; border.width: 1
                    }
                }
            }

            ListModel {
                id: lightModel
                ListElement { name: "Exposure"; key: "exposure"; min: -100; max: 100 }
                ListElement { name: "Contrast"; key: "contrast"; min: -100; max: 100 }
                ListElement { name: "Whites"; key: "whites"; min: -100; max: 100 }
                ListElement { name: "Shadows"; key: "shadows"; min: -100; max: 100 }
                ListElement { name: "Blacks"; key: "blacks"; min: -100; max: 100 }
            }
            Repeater { model: lightModel; delegate: compactSlider }

            Rectangle { Layout.fillWidth: true; height: 1; color: compactEditor.separatorColor; Layout.topMargin: 4 }

            // --- COLOR Section ---
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 2
                spacing: 4

                Text {
                    text: "COLOR"
                    font.pixelSize: 9
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1.0
                    color: "#9a9795"
                }

                Item { Layout.fillWidth: true }

                Button {
                    id: autoWbBtn
                    implicitWidth: 36; implicitHeight: 16
                    Layout.minimumWidth: 36; Layout.preferredWidth: 36; Layout.maximumWidth: 36
                    Layout.minimumHeight: 16; Layout.preferredHeight: 16; Layout.maximumHeight: 16
                    enabled: !compactEditor.cropActive
                    padding: 0
                    flat: true
                    onClicked: {
                        compactEditor.ensureEditorLoaded()
                        if (compactEditor.controllerRef) compactEditor.controllerRef.auto_white_balance()
                        compactEditor.updatePulse++
                    }
                    contentItem: Text {
                        text: "Auto"
                        font.pixelSize: 8
                        color: autoWbBtn.hovered ? "#e8e6e3" : "#9a9795"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: 2
                        color: autoWbBtn.hovered ? "#30ffffff" : "#18ffffff"
                        border.color: "#3a3a3a"; border.width: 1
                    }
                }
            }

            ListModel {
                id: colorModel
                ListElement { name: "Temp (B/Y)"; key: "white_balance_by"; min: -100; max: 100 }
                ListElement { name: "Tint (G/M)"; key: "white_balance_mg"; min: -100; max: 100 }
                ListElement { name: "Vibrance"; key: "vibrance"; min: -100; max: 100 }
            }
            Repeater { model: colorModel; delegate: compactSlider }

            // --- Footer Buttons ---
            Item { Layout.fillHeight: true; Layout.minimumHeight: 10 }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                Button {
                    text: "Reset"
                    flat: true
                    Layout.preferredWidth: 60
                    Layout.preferredHeight: 28
                    enabled: !compactEditor.cropActive
                    font.pixelSize: 11
                    Material.foreground: compactEditor.mutedText
                    onClicked: {
                        compactEditor.ensureEditorLoaded()
                        if (compactEditor.controllerRef) compactEditor.controllerRef.reset_edit_parameters()
                        compactEditor.updatePulse++
                    }
                    background: Rectangle {
                        color: "transparent"; radius: 4
                    }
                }

                Item { Layout.fillWidth: true }

                Button {
                    id: closeBtn
                    text: "Close"
                    Layout.preferredWidth: 60
                    Layout.preferredHeight: 28
                    font.pixelSize: 11
                    ToolTip.visible: hovered
                    ToolTip.delay: 500
                    ToolTip.text: compactEditor.closeTooltip
                    onHoveredChanged: if (hovered) compactEditor.refreshCloseTooltip()
                    onClicked: compactEditor.requestClose()
                    contentItem: Text {
                        text: closeBtn.text
                        font: closeBtn.font
                        color: compactEditor.textColor
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: closeBtn.down ? "#20ffffff" : "transparent"
                        radius: 4
                        border.color: closeBtn.hovered ? "#60ffffff" : compactEditor.controlBorder
                        border.width: 1
                    }
                }

                Button {
                    id: saveBtn
                    text: compactEditor.uiStateRef && compactEditor.uiStateRef.isSaving ? "Saving..." : "Save"
                    Layout.preferredWidth: 80
                    Layout.preferredHeight: 28
                    font.pixelSize: 11
                    enabled: compactEditor.uiStateRef ? (!compactEditor.uiStateRef.isSaving && !compactEditor.cropActive) : true
                    onClicked: {
                        compactEditor.ensureEditorLoaded()
                        if (compactEditor.controllerRef) compactEditor.controllerRef.save_edited_image()
                    }
                    contentItem: Text {
                        text: saveBtn.text
                        font: saveBtn.font
                        color: "white"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        opacity: saveBtn.enabled ? 1.0 : 0.5
                    }
                    background: Rectangle {
                        color: saveBtn.enabled
                            ? (saveBtn.down ? Qt.darker(compactEditor.accentColor, 1.1) : compactEditor.accentColor)
                            : Qt.darker(compactEditor.accentColor, 1.5)
                        radius: 4
                    }
                }
            }
        }
    }

    // --- Compact Slider Component ---
    Component {
        id: compactSlider
        RowLayout {
            id: sliderRow
            required property string name
            required property string key
            required property real min
            required property real max

            Layout.fillWidth: true
            spacing: 6

            // Clickable label. Clicking it makes this the slider that the
            // Up/Down arrow keys adjust; the highlighted row is tinted.
            Rectangle {
                Layout.preferredWidth: 70
                Layout.preferredHeight: 18
                Layout.alignment: Qt.AlignVCenter
                radius: 3
                property bool isActive: compactEditor.highlightedSliderKey === sliderRow.key
                color: isActive ? "#332f6df0" : "transparent"
                border.color: isActive ? compactEditor.accentColor : "transparent"
                border.width: 1

                Text {
                    anchors.fill: parent
                    anchors.leftMargin: 4
                    verticalAlignment: Text.AlignVCenter
                    text: sliderRow.name
                    color: parent.isActive ? compactEditor.accentColorHover : compactEditor.textColor
                    font.pixelSize: 11
                    font.weight: parent.isActive ? Font.DemiBold : Font.Medium
                    elide: Text.ElideRight
                }

                MouseArea {
                    anchors.fill: parent
                    enabled: !compactEditor.cropActive
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        compactEditor.highlightedSliderKey = sliderRow.key
                        compactEditor.focusKeyboardHandler()
                    }
                }
            }

            Slider {
                id: slider
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
                enabled: !compactEditor.cropActive
                focusPolicy: Qt.NoFocus
                from: sliderRow.min
                to: sliderRow.max
                stepSize: 1
                property real editScale: compactEditor.sliderEditScale(sliderRow.key)

                property real backendValue: compactEditor.getBackendValue(sliderRow.key) / slider.editScale * sliderRow.max

                function editValueFromSliderValue(sliderValue) {
                    return sliderValue / sliderRow.max * slider.editScale
                }

                Binding {
                    target: slider
                    property: "value"
                    value: slider.backendValue
                    when: !slider.pressed && !slider.isResetting
                }

                property real _pendingValue: 0
                property real _lastSentValue: 0
                Timer {
                    id: sendTimer
                    interval: 16
                    repeat: true
                    onTriggered: {
                        if (Math.abs(slider._pendingValue - slider._lastSentValue) > 0.001) {
                            if (compactEditor.controllerRef) compactEditor.controllerRef.set_edit_parameter(sliderRow.key, slider.editValueFromSliderValue(slider._pendingValue))
                            slider._lastSentValue = slider._pendingValue
                        }
                    }
                }

                TapHandler {
                    acceptedButtons: Qt.LeftButton
                    gesturePolicy: TapHandler.DragThreshold
                    onDoubleTapped: {
                        if (!slider.isResetting) slider.triggerReset()
                    }
                }

                property bool isResetting: false
                Timer {
                    id: resetTimer
                    interval: 100
                    repeat: false
                    onTriggered: slider.isResetting = false
                }

                function triggerReset() {
                    compactEditor.ensureEditorLoaded()
                    compactEditor.highlightedSliderKey = sliderRow.key
                    slider.isResetting = true
                    sendTimer.stop()
                    if (compactEditor.controllerRef) compactEditor.controllerRef.set_edit_parameter(sliderRow.key, 0.0)
                    slider.value = 0.0
                    _pendingValue = 0.0
                    slider._lastSentValue = 0.0
                    compactEditor.updatePulse++
                    resetTimer.restart()
                }

                onPressedChanged: {
                    if (pressed) {
                        compactEditor.ensureEditorLoaded()
                        compactEditor.highlightedSliderKey = sliderRow.key
                        compactEditor.focusKeyboardHandler()
                        compactEditor.slidersPressedCount++
                        if (!slider.isResetting) {
                            _pendingValue = value
                            slider._lastSentValue = value
                            if (!sendTimer.running) sendTimer.start()
                        }
                    } else {
                        compactEditor.slidersPressedCount--
                        sendTimer.stop()
                        if (slider.isResetting) {
                            if (compactEditor.controllerRef) compactEditor.controllerRef.set_edit_parameter(sliderRow.key, 0.0)
                        } else {
                            if (compactEditor.controllerRef) compactEditor.controllerRef.set_edit_parameter(sliderRow.key, slider.editValueFromSliderValue(value))
                        }
                        if (compactEditor.controllerRef) compactEditor.controllerRef.update_histogram()
                    }
                }

                onMoved: {
                    if (slider.isResetting) return
                    compactEditor.highlightedSliderKey = sliderRow.key
                    _pendingValue = value
                    if (!sendTimer.running) sendTimer.start()
                }

                Behavior on value {
                    enabled: !slider.pressed && !slider.isResetting
                    NumberAnimation { duration: 200; easing.type: Easing.OutQuad }
                }

                background: Item {
                    x: slider.leftPadding
                    y: slider.topPadding + slider.availableHeight / 2 - height / 2
                    width: slider.availableWidth
                    height: 4

                    Rectangle {
                        anchors.fill: parent; radius: 2
                        color: "#2e2e2e"; border.color: "#383838"; border.width: 1
                    }

                    Rectangle {
                        property real range: slider.to - slider.from
                        property real anchorVal: Math.max(slider.from, Math.min(slider.to, 0))
                        property real anchorPos: (anchorVal - slider.from) / range
                        x: Math.min(slider.visualPosition, anchorPos) * parent.width
                        width: Math.abs(slider.visualPosition - anchorPos) * parent.width
                        height: parent.height; radius: 2
                        color: compactEditor.accentColor; opacity: 0.6
                        Behavior on width { NumberAnimation { duration: 100 } }
                        Behavior on x { NumberAnimation { duration: 100 } }
                    }
                }

                handle: Rectangle {
                    x: slider.leftPadding + slider.visualPosition * (slider.availableWidth - width)
                    y: slider.topPadding + slider.availableHeight / 2 - height / 2
                    width: 10; height: 10; radius: 5
                    color: "#e8e6e3"
                    border.color: slider.pressed ? compactEditor.accentColor : "#5a5755"
                    border.width: 1
                    scale: handleHover.hovered || slider.pressed ? 1.3 : 1.0
                    Behavior on scale { NumberAnimation { duration: 150; easing.type: Easing.OutBack } }

                    HoverHandler { id: handleHover }
                }
            }

            Text {
                id: valueReadout
                property int displayValue: Math.round(slider.value)
                text: displayValue === 0 ? "0" : (displayValue > 0 ? "+" + displayValue : "−" + Math.abs(displayValue))
                Layout.preferredWidth: 32
                Layout.alignment: Qt.AlignVCenter
                horizontalAlignment: Text.AlignRight
                font.family: "IBM Plex Mono"
                font.pixelSize: 10
                color: displayValue === 0 ? compactEditor.mutedText : "#e8e6e3"

                MouseArea {
                    anchors.fill: parent
                    enabled: !compactEditor.cropActive
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        compactEditor.highlightedSliderKey = sliderRow.key
                        compactEditor.focusKeyboardHandler()
                        if (!slider.isResetting) slider.triggerReset()
                    }
                }
            }
        }
    }
}
