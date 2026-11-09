pragma ComponentBehavior: Bound

import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import QtCore
import "WindowPlacement.js" as WP

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
    property bool windowGeometryReady: false
    property bool windowWasVisible: false

    visible: compactEditor.uiStateRef
             ? (compactEditor.uiStateRef.isEditorOpen && !compactEditor.uiStateRef.isEditorExpanded)
             : false

    Settings {
        id: compactSettings
        category: "compactEditor"
        property bool overlaidHistogram: true
        property real savedX: -1
        property real savedY: -1
        property real savedWidth: -1
        property real savedHeight: -1
    }

    Component.onCompleted: {
        compactEditor.controllerRef = controller
        if (compactEditor.controllerRef)
            compactEditor.controllerRef.register_auxiliary_navigation_window(compactEditor)
        compactEditor.restoreWindowPlacement()
        compactEditor.windowGeometryReady = true
        compactEditor.uiStateRef = uiState
        compactEditor.keyboardHandlerReady = true
        Qt.callLater(compactEditor.focusKeyboardHandler)
    }

    function hasSavedPosition() {
        return compactSettings.savedX !== -1 && compactSettings.savedY !== -1
    }

    function hasSavedSize() {
        return compactSettings.savedWidth > 0 && compactSettings.savedHeight > 0
    }

    function positionAtRightGutter() {
        var mainWin = compactEditor.transientParent
        if (mainWin) {
            compactEditor.x = mainWin.x + mainWin.width - compactEditor.width - 10
            compactEditor.y = mainWin.y + 40
        }
    }

    function clampWindowToVisibleScreen(force) {
        if (!force && !compactEditor.visible) return
        // Clamp against the monitor the coordinates fall on (not just the
        // current screen), so a window saved on a secondary monitor is not
        // dragged back to the primary before it has been mapped.
        var geometry = compactEditor.controllerRef
                     ? compactEditor.controllerRef.available_screen_geometry(
                           compactEditor.x, compactEditor.y)
                     : null
        WP.clampToScreen(compactEditor, geometry, {
            minWidth: compactEditor.minimumWidth,
            maxWidth: compactEditor.maximumWidth,
            minHeight: compactEditor.minimumHeight
        })
    }

    function restoreWindowPlacement() {
        // No suppress flag needed: this only runs from Component.onCompleted
        // before windowGeometryReady is set, and saveWindowPlacement bails out
        // until then.
        if (compactEditor.hasSavedSize()) {
            compactEditor.width = compactSettings.savedWidth
            compactEditor.height = compactSettings.savedHeight
        }

        if (compactEditor.hasSavedPosition()) {
            compactEditor.x = compactSettings.savedX
            compactEditor.y = compactSettings.savedY
        } else {
            compactEditor.positionAtRightGutter()
        }

        compactEditor.clampWindowToVisibleScreen(true)
    }

    function saveWindowPlacement(force) {
        if (!compactEditor.windowGeometryReady) return
        if (!compactEditor.windowWasVisible && !compactEditor.hasSavedPosition()) return
        if (!force && !compactEditor.visible) return
        if (compactEditor.visibility === Window.Minimized) return

        compactSettings.savedX = compactEditor.x
        compactSettings.savedY = compactEditor.y
        compactSettings.savedWidth = compactEditor.width
        compactSettings.savedHeight = compactEditor.height
    }

    // Debounce continuous geometry changes during drags/resizes.
    Timer {
        id: saveGeometryTimer
        interval: 400
        onTriggered: compactEditor.saveWindowPlacement()
    }

    onXChanged: saveGeometryTimer.restart()
    onYChanged: saveGeometryTimer.restart()
    onWidthChanged: saveGeometryTimer.restart()
    onHeightChanged: saveGeometryTimer.restart()
    onActiveChanged: {
        if (active) {
            compactEditor.focusKeyboardHandler()
        } else if (compactEditor.controllerRef) {
            compactEditor.controllerRef.release_auxiliary_navigation_hold(compactEditor)
        }
    }
    Component.onDestruction: {
        if (compactEditor.controllerRef)
            compactEditor.controllerRef.unregister_auxiliary_navigation_window(compactEditor)
        compactEditor.saveWindowPlacement(true)
    }

    // --- Color Palette (matches full editor) ---
    readonly property color backgroundColor: "#1e1e1e"
    readonly property color textColor: "white"
    // Brush mode has two tools. Dodge & burn (recipeMode) tints the sliders
    // orange: they edit a local recipe instead of the global edit. Stack
    // paint (stackMode) paints a stack input frame; the sliders stay global.
    readonly property bool brushMode: compactEditor.uiStateRef ? compactEditor.uiStateRef.isBrushMode : false
    readonly property bool stackMode: compactEditor.brushMode && !!compactEditor.uiStateRef && compactEditor.uiStateRef.brushTool === "stack"
    readonly property bool recipeMode: compactEditor.brushMode && !compactEditor.stackMode
    // Stack paint needs an in-camera stacked JPG whose input JPGs were found.
    readonly property bool stackPaintAvailable: !!compactEditor.uiStateRef && compactEditor.uiStateRef.stackInputs.length > 0
    readonly property int stackInputIndex: {
        var inputs = compactEditor.uiStateRef ? compactEditor.uiStateRef.stackInputs : []
        var name = compactEditor.uiStateRef ? compactEditor.uiStateRef.stackInputName : ""
        for (var i = 0; i < inputs.length; ++i) {
            if (inputs[i].name === name) return i
        }
        return -1
    }
    readonly property string stackInputLabel: compactEditor.stackInputIndex >= 0 ? compactEditor.uiStateRef.stackInputs[compactEditor.stackInputIndex].label : ""
    readonly property color stackAccent: "#2dd4bf"
    readonly property color stackAccentHover: "#5eead4"
    readonly property color accentColor: compactEditor.recipeMode ? "#f59e0b" : "#6366f1"
    readonly property color accentColorHover: compactEditor.recipeMode ? "#fbbf24" : "#818cf8"
    // Accent of the brush panel (brush size, mask toggle, layer rows).
    readonly property color panelAccent: compactEditor.stackMode ? compactEditor.stackAccent : compactEditor.accentColor
    readonly property color panelAccentHover: compactEditor.stackMode ? compactEditor.stackAccentHover : compactEditor.accentColorHover
    readonly property color controlBg: "#10ffffff"
    readonly property color controlBorder: "#30ffffff"
    readonly property color separatorColor: "#20ffffff"
    readonly property color mutedText: "#6b6764"

    color: compactEditor.backgroundColor
    Material.theme: Material.Dark
    Material.accent: compactEditor.accentColor

    property int updatePulse: 0
    // Whether the per-hue COLOR MIX section is expanded (collapsed by default).
    property bool colorMixExpanded: false
    readonly property bool cropActive: compactEditor.uiStateRef ? compactEditor.uiStateRef.isCropping : false
    // Let the controller own plain Left/Right so presses and holds navigate
    // images with the same cadence as they do in the loupe window.
    readonly property bool plainArrowNavigationEnabled: visible
                                                        && controllerRef
                                                        && uiStateRef
                                                        && !cropActive
                                                        && !discardDialog.opened
    onPlainArrowNavigationEnabledChanged: {
        if (!plainArrowNavigationEnabled && compactEditor.controllerRef)
            compactEditor.controllerRef.release_auxiliary_navigation_hold(compactEditor)
    }
    property int lastLoadedIndex: -1
    property bool lastLoadWasFull: false
    property bool forceNextPreviewLoad: false
    property string closeTooltip: "Close editor"

    // Key of the slider that the Up/Down arrow keys will adjust. The matching
    // row is highlighted so the user can see which control is targeted.
    // Clicking a slider's label (or value) retargets it. Defaults to the first
    // slider so Up/Down work immediately when the editor opens.
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
        compactEditor.ensureEditorLoaded("slider-key")
        var key = compactEditor.highlightedSliderKey
        var scale = compactEditor.sliderEditScale(key)
        // Convert the stored edit-space value into slider space, step it, clamp,
        // then convert back to edit space the way the sliders do.
        var sliderVal = compactEditor.getBackendValue(key) / scale * 100
        sliderVal = Math.max(-100, Math.min(100, sliderVal + delta))
        compactEditor.setEditValue(key, sliderVal / 100 * scale)
        compactEditor.updatePulse++  // refreshes sliders + histogram
    }

    // Scroll the editor so `item` (a slider row) is fully visible.
    function ensureRowVisible(item) {
        var flick = editorScroll.contentItem
        if (!item || !flick || flick.contentY === undefined) return
        var top = item.mapToItem(flick.contentItem, 0, 0).y
        var bottom = top + item.height
        var maxY = Math.max(0, flick.contentHeight - flick.height)
        if (top < flick.contentY)
            flick.contentY = Math.max(0, top - 4)
        else if (bottom > flick.contentY + flick.height)
            flick.contentY = Math.min(maxY, bottom - flick.height + 4)
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

    function handleArrowKey(key, modifiers) {
        if (modifiers === undefined) modifiers = Qt.NoModifier
        if (compactEditor.cropActive || discardDialog.opened) return false
        if (key === Qt.Key_Left || key === Qt.Key_Right) {
            var step = key === Qt.Key_Right ? 1 : -1
            if (modifiers === Qt.NoModifier) {
                // Normally the controller's event filter handles physical
                // plain arrows. Keep this fallback for synthetic QML events.
                if (compactEditor.uiStateRef) {
                    if (step > 0) compactEditor.uiStateRef.nextImage()
                    else compactEditor.uiStateRef.prevImage()
                }
                return true
            }
            if (modifiers === Qt.ControlModifier) {
                // Ctrl+Left/Right: previous/next image while editing.
                if (compactEditor.uiStateRef) {
                    if (step > 0) compactEditor.uiStateRef.nextImage()
                    else compactEditor.uiStateRef.prevImage()
                }
                return true
            }
            // Other modifiers (Shift = jump 10 images) keep the main-window
            // bindings.
            if (compactEditor.controllerRef)
                compactEditor.controllerRef.handle_key_from_compact_editor(key, modifiers, "")
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
        interval: 250
        repeat: false
        onTriggered: {
            if (!compactEditor.visible || !compactEditor.controllerRef || !compactEditor.uiStateRef) return
            var idx = compactEditor.uiStateRef.currentIndex
            var forceLoad = compactEditor.forceNextPreviewLoad
            compactEditor.forceNextPreviewLoad = false
            if (!forceLoad && idx === compactEditor.lastLoadedIndex) {
                compactEditor.controllerRef.note_compact_editor_reload_skipped(idx, "already-loaded")
                return
            }
            // The result can be discarded without an editorPreviewLoaded
            // signal, so don't keep claiming the previous load is current.
            compactEditor.lastLoadedIndex = -1
            compactEditor.lastLoadWasFull = false
            var loaded = compactEditor.controllerRef.load_image_for_editing_preview()
            if (!loaded) {
                compactEditor.lastLoadedIndex = -1
                compactEditor.lastLoadWasFull = false
                return
            }
        }
    }

    function schedulePreviewLoad(reason, forceLoad) {
        if (!compactEditor.visible || !compactEditor.controllerRef || !compactEditor.uiStateRef) return
        var idx = compactEditor.uiStateRef.currentIndex
        var coalesced = deferredLoadTimer.running
        compactEditor.forceNextPreviewLoad = compactEditor.forceNextPreviewLoad || !!forceLoad
        if (idx !== compactEditor.lastLoadedIndex) compactEditor.lastLoadWasFull = false
        compactEditor.controllerRef.note_compact_editor_reload_scheduled(idx, coalesced, deferredLoadTimer.interval, reason)
        deferredLoadTimer.restart()
    }

    function ensureEditorLoaded(reason) {
        if (!compactEditor.controllerRef || !compactEditor.uiStateRef) return
        var idx = compactEditor.uiStateRef.currentIndex
        if (idx !== compactEditor.lastLoadedIndex || !compactEditor.lastLoadWasFull) {
            deferredLoadTimer.stop()
            compactEditor.controllerRef.note_compact_editor_full_load_required(idx, reason || "user-action")
            var loaded = compactEditor.controllerRef.load_image_for_editing()
            if (!loaded) {
                compactEditor.lastLoadedIndex = -1
                compactEditor.lastLoadWasFull = false
                return
            }
            compactEditor.lastLoadedIndex = idx
            compactEditor.lastLoadWasFull = true
            compactEditor.controllerRef.update_histogram()
            compactEditor.updatePulse++
        }
    }

    Connections {
        target: compactEditor.controllerRef
        function onEditorPreviewLoaded(index, loaded) {
            if (!compactEditor.visible || !compactEditor.uiStateRef || index !== compactEditor.uiStateRef.currentIndex) return
            compactEditor.lastLoadedIndex = loaded ? index : -1
            compactEditor.lastLoadWasFull = false
            if (loaded) {
                compactEditor.controllerRef.update_histogram()
                compactEditor.updatePulse++
            }
        }
    }

    Connections {
        target: compactEditor.uiStateRef
        function onCurrentIndexChanged() {
            compactEditor.schedulePreviewLoad("navigation")
        }
    }

    onVisibleChanged: {
        if (visible && compactEditor.controllerRef) {
            compactEditor.windowWasVisible = true
            compactEditor.schedulePreviewLoad("open", true)
            if (!compactEditor.hasSavedPosition()) positionAtRightGutter()
            compactEditor.clampWindowToVisibleScreen(true)
            Qt.callLater(compactEditor.focusKeyboardHandler)
        } else {
            if (compactEditor.controllerRef) {
                compactEditor.controllerRef.release_auxiliary_navigation_hold(compactEditor)
                compactEditor.controllerRef.set_editor_slider_drag_active(false)
                compactEditor.controllerRef.exit_local_brush_mode()
            }
            compactEditor.saveWindowPlacement(true)
            deferredLoadTimer.stop()
        }
    }

    onUpdatePulseChanged: {
        if (visible && compactEditor.controllerRef) {
            compactEditor.controllerRef.update_histogram()
        }
    }

    function getBackendValue(key) {
        var _dependency = updatePulse;
        if (compactEditor.recipeMode) {
            var recipe = compactEditor.uiStateRef.localRecipe
            return (recipe && recipe[key] !== undefined) ? recipe[key] : 0.0
        }
        if (compactEditor.uiStateRef && key in compactEditor.uiStateRef) return compactEditor.uiStateRef[key];
        return 0.0;
    }

    // Slider writes go to the global edit, or in dodge & burn to the local recipe.
    function setEditValue(key, value) {
        if (!compactEditor.controllerRef) return
        if (compactEditor.recipeMode) compactEditor.controllerRef.set_local_recipe_param(key, value)
        else compactEditor.controllerRef.set_edit_parameter(key, value)
    }

    function sliderEditScale(key) {
        if (key === "contrast") return 0.5
        return (key === "exposure" || key === "whites") ? 2.0 : 1.0
    }

    Shortcut {
        sequence: "Shift+Left"
        context: Qt.WindowShortcut
        enabled: compactEditor.visible && !compactEditor.cropActive && !discardDialog.opened
        onActivated: compactEditor.handleArrowKey(Qt.Key_Left, Qt.ShiftModifier)
    }

    Shortcut {
        sequence: "Shift+Right"
        context: Qt.WindowShortcut
        enabled: compactEditor.visible && !compactEditor.cropActive && !discardDialog.opened
        onActivated: compactEditor.handleArrowKey(Qt.Key_Right, Qt.ShiftModifier)
    }

    onClosing: (close) => {
        if (compactEditor.uiStateRef && compactEditor.controllerRef) {
            close.accepted = false
            compactEditor.requestClose()
        }
    }

    // Keyboard handling for the compact editor window.
    //
    // Arrow keys (see handleArrowKey): Left / Right switch images using the
    // controller's loupe-navigation hold behavior, Up / Down adjust the
    // highlighted slider, Ctrl+Left / Right also switch images, and
    // Shift+Left / Right keep the main window's jump-by-10 binding.
    //
    // This focus scope handles the remaining compact-editor keys:
    //   - Esc / E / S / O  -> editor-local actions (close / save / crop)
    //   - everything else  -> forwarded to the main window key bindings so
    //                         keys like B (batch), F, D, I, etc. still work
    //                         while the editor is focused.
    FocusScope {
        id: keyScope
        anchors.fill: parent
        focus: compactEditor.visible && !discardDialog.opened

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

            if (compactEditor.handleArrowKey(event.key, event.modifiers)) {
                event.accepted = true
            } else if (event.key === Qt.Key_Escape) {
                if (compactEditor.brushMode) {
                    if (compactEditor.controllerRef) compactEditor.controllerRef.exit_local_brush_mode()
                } else {
                    compactEditor.requestClose()
                }
                event.accepted = true
            } else if (event.key === Qt.Key_E && !(event.modifiers & Qt.ControlModifier)) {
                compactEditor.requestClose()
                event.accepted = true
            } else if (event.key === Qt.Key_S) {
                // S or Ctrl+S both save the live edits from the compact editor.
                compactEditor.ensureEditorLoaded("save")
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

    readonly property string stackHelpText:
        "<b>Stack Paint</b><br>" +
        "Paint one of the stack's input frames over the stacked photo, e.g. to bring back a clean background or remove stacking halos.<br><br>" +
        "<b>Choosing the frame</b><br>" +
        "• The dropdown next to <i>Stack</i> lists the input frames, near focus (1) to far focus. It starts on the frame focused furthest back.<br>" +
        "• <i>S: Stack output</i> paints the stacked photo itself back over frames painted earlier.<br>" +
        "• Each frame is aligned to the stack automatically (takes a second the first time).<br>" +
        "• Painting from a different frame starts a new layer on top. Click a layer to pick its frame again.<br>" +
        "• ◉ hides/shows a layer, × deletes it.<br><br>" +
        "<b>Painting</b><br>" +
        "• Hover: the brush circle previews the frame (with your edits) before you click<br>" +
        "• Left-drag: paint the frame in<br>" +
        "• Right-drag or Alt+drag: restore the stack (erases all stack layers there)<br>" +
        "• Hardness: how soft the brush edge is<br><br>" +
        "<b>Keys</b><br>" +
        "• <b>[ ]</b> brush size &nbsp;·&nbsp; <b>Ctrl+Z</b>: undo last stroke<br>" +
        "• hold <b>Space</b>: show original &nbsp;·&nbsp; <b>Space</b>+drag or middle-drag: pan &nbsp;·&nbsp; scroll: zoom<br>" +
        "• <b>M</b>: toggle mask overlay &nbsp;·&nbsp; hold <b>Ctrl</b>: show it briefly<br>" +
        "• <b>Esc</b>: leave stack paint<br><br>" +
        "<b>Good to know</b><br>" +
        "• The sliders keep editing the whole photo, painted frame included, and dodge &amp; burn layers apply on top.<br>" +
        "• Layers are saved with the edit; reopening the photo re-aligns the frames."

    // Brush quick reference (the ? button in brush mode).
    Popup {
        id: brushHelpPopup
        x: 8
        y: 40
        width: compactEditor.width - 16
        height: Math.min(compactEditor.height - 56, brushHelpText.implicitHeight + 24)
        padding: 12
        modal: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onClosed: compactEditor.focusKeyboardHandler()
        // Leaving brush mode (Done, Esc, Shift+D) takes the help with it.
        Connections {
            target: compactEditor
            function onBrushModeChanged() { if (!compactEditor.brushMode) brushHelpPopup.close() }
        }
        background: Rectangle {
            color: "#262626"
            radius: 6
            border.color: compactEditor.panelAccent
            border.width: 1
        }

        Flickable {
            anchors.fill: parent
            contentHeight: brushHelpText.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            Text {
                id: brushHelpText
                width: parent.width
                wrapMode: Text.WordWrap
                textFormat: Text.StyledText
                color: "#e8e6e3"
                font.pixelSize: 11
                linkColor: compactEditor.panelAccentHover
                text: compactEditor.stackMode ? compactEditor.stackHelpText : "<b>Dodge &amp; Burn</b><br>" +
                      "Paint any of the slider effects onto part of the photo.<br><br>" +
                      "<b>Choosing the effect</b><br>" +
                      "• With <i>New strokes</i> selected, the orange sliders set the recipe for the next stroke (e.g. Exposure −30 to burn, +30 to dodge; mix in any sliders).<br>" +
                      "• If you change the sliders after painting, the next stroke starts a new layer, so earlier strokes keep their look.<br>" +
                      "• Click a layer to change its sliders after painting; all of its strokes update. Click it again (or <i>New strokes</i>) to go back.<br>" +
                      "• ◉ hides/shows a layer, × deletes it.<br><br>" +
                      "<b>Painting</b><br>" +
                      "• Left-drag: paint one coat<br>" +
                      "• Paint over the same spot again to build it up (2 coats = twice the recipe)<br>" +
                      "• Right-drag or Alt+drag: erase from the selected / last-painted layer<br>" +
                      "• Hardness: how soft the brush edge is (inner ring on the cursor = full strength)<br><br>" +
                      "<b>Keys</b><br>" +
                      "• <b>[ ]</b> brush size<br>" +
                      "• hold <b>Space</b>: show original &nbsp;·&nbsp; <b>Space</b>+drag or middle-drag: pan &nbsp;·&nbsp; scroll: zoom<br>" +
                      "• <b>Ctrl+Z</b>: undo last stroke<br>" +
                      "• <b>M</b>: toggle mask overlay &nbsp;·&nbsp; hold <b>Ctrl</b>: show it briefly<br>" +
                      "• <b>Shift+D</b> or <b>Esc</b>: leave dodge &amp; burn<br><br>" +
                      "<b>Good to know</b><br>" +
                      "• Reset clears the global sliders and all layers.<br>" +
                      "• Layers are saved with the edit; come back to the photo to change them.<br>" +
                      "• Strokes follow crop and rotation."
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

        onAboutToShow: {
            if (compactEditor.controllerRef)
                compactEditor.controllerRef.dialog_opened("qml-compact-discard")
            if (compactEditor.controllerRef)
                compactEditor.controllerRef.release_auxiliary_navigation_hold(compactEditor)
        }
        onClosed: if (compactEditor.controllerRef) compactEditor.controllerRef.dialog_closed("qml-compact-discard")
        Component.onDestruction: if (compactEditor.controllerRef) compactEditor.controllerRef.dialog_closed("qml-compact-discard")

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
                spacing: 4

                // Brush tools: dodge & burn ("Burn", Shift+D) and stack paint
                // with its input-frame picker.
                RowLayout {
                    spacing: 2

                    Button {
                        id: brushModeBtn
                        implicitHeight: 26
                        Layout.preferredHeight: 26
                        enabled: !compactEditor.cropActive
                        padding: 0
                        leftPadding: 6; rightPadding: 6
                        flat: true
                        ToolTip.visible: hovered
                        ToolTip.delay: 500
                        ToolTip.text: compactEditor.recipeMode ? "Leave dodge & burn (Shift+D)" : "Dodge & burn brush (Shift+D)"
                        onClicked: {
                            if (compactEditor.controllerRef) compactEditor.controllerRef.toggle_local_brush_mode()
                            compactEditor.focusKeyboardHandler()
                        }
                        contentItem: Text {
                            text: "◐ Burn"
                            font.pixelSize: 11
                            font.weight: compactEditor.recipeMode ? Font.DemiBold : Font.Normal
                            color: compactEditor.recipeMode ? "#1e1e1e"
                                   : (brushModeBtn.hovered ? compactEditor.accentColorHover : compactEditor.textColor)
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            radius: 4
                            color: compactEditor.recipeMode ? compactEditor.accentColor
                                   : (brushModeBtn.hovered ? "#30ffffff" : "transparent")
                            border.color: compactEditor.recipeMode ? "transparent" : compactEditor.controlBorder
                            border.width: 1
                        }
                    }

                    // Stack paint: grey unless this is an in-camera stacked JPG
                    // whose input JPGs were found.
                    Button {
                        id: stackModeBtn
                        implicitHeight: 26
                        Layout.preferredHeight: 26
                        enabled: !compactEditor.cropActive && compactEditor.stackPaintAvailable
                        padding: 0
                        leftPadding: 6; rightPadding: 6
                        flat: true
                        ToolTip.visible: hovered
                        ToolTip.delay: 500
                        ToolTip.text: compactEditor.stackMode ? "Leave stack paint" : "Stack paint: paint an input frame over the stack"
                        onClicked: {
                            if (compactEditor.controllerRef) compactEditor.controllerRef.toggle_stack_brush_mode()
                            compactEditor.focusKeyboardHandler()
                        }
                        contentItem: Text {
                            text: "Stack"
                            font.pixelSize: 11
                            font.weight: compactEditor.stackMode ? Font.DemiBold : Font.Normal
                            color: !stackModeBtn.enabled ? "#55524f"
                                   : compactEditor.stackMode ? "#1e1e1e"
                                   : (stackModeBtn.hovered ? compactEditor.stackAccentHover : compactEditor.textColor)
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            radius: 4
                            color: compactEditor.stackMode ? compactEditor.stackAccent
                                   : (stackModeBtn.hovered ? "#30ffffff" : "transparent")
                            border.color: compactEditor.stackMode ? "transparent"
                                          : (stackModeBtn.enabled ? compactEditor.controlBorder : "#18ffffff")
                            border.width: 1
                        }
                    }

                    // Which input frame stack paint reveals (1 = nearest focus).
                    ComboBox {
                        id: stackInputCombo
                        implicitWidth: 34; implicitHeight: 26
                        Layout.minimumWidth: 34; Layout.preferredWidth: 34; Layout.maximumWidth: 34
                        Layout.preferredHeight: 26
                        enabled: !compactEditor.cropActive && compactEditor.stackPaintAvailable
                        focusPolicy: Qt.NoFocus
                        model: compactEditor.uiStateRef ? compactEditor.uiStateRef.stackInputs : []
                        textRole: "label"
                        currentIndex: compactEditor.stackInputIndex
                        onActivated: function(index) {
                            var inputs = compactEditor.uiStateRef ? compactEditor.uiStateRef.stackInputs : []
                            if (compactEditor.controllerRef && index >= 0 && index < inputs.length)
                                compactEditor.controllerRef.set_stack_input(inputs[index].name)
                            currentIndex = Qt.binding(function() { return compactEditor.stackInputIndex })
                            compactEditor.focusKeyboardHandler()
                        }
                        ToolTip.visible: hovered && !stackInputCombo.popup.visible
                        ToolTip.delay: 500
                        ToolTip.text: compactEditor.stackInputLabel !== ""
                                      ? "Stack input to paint: " + compactEditor.stackInputLabel
                                      : "Stack input to paint"
                        contentItem: Text {
                            leftPadding: 4
                            text: stackInputCombo.currentIndex >= 0 && compactEditor.uiStateRef
                                  ? compactEditor.uiStateRef.stackInputs[stackInputCombo.currentIndex].short
                                  : "–"
                            font.pixelSize: 10
                            color: stackInputCombo.enabled ? compactEditor.textColor : "#55524f"
                            verticalAlignment: Text.AlignVCenter
                        }
                        indicator: Text {
                            x: stackInputCombo.width - width - 4
                            y: (stackInputCombo.height - height) / 2
                            text: "▾"
                            font.pixelSize: 9
                            color: stackInputCombo.enabled ? "#9a9795" : "#55524f"
                        }
                        background: Rectangle {
                            radius: 4
                            color: stackInputCombo.hovered && stackInputCombo.enabled ? "#30ffffff" : "transparent"
                            border.color: stackInputCombo.enabled ? compactEditor.controlBorder : "#18ffffff"
                            border.width: 1
                        }
                        delegate: ItemDelegate {
                            id: stackInputDelegate
                            required property var modelData
                            required property int index
                            width: 176
                            height: 22
                            highlighted: stackInputCombo.highlightedIndex === stackInputDelegate.index
                            contentItem: Text {
                                text: stackInputDelegate.modelData.label
                                font.pixelSize: 10
                                color: stackInputDelegate.index === stackInputCombo.currentIndex ? compactEditor.stackAccentHover : "#e8e6e3"
                                verticalAlignment: Text.AlignVCenter
                            }
                            background: Rectangle {
                                color: stackInputDelegate.highlighted ? "#30ffffff" : "transparent"
                            }
                        }
                        popup: Popup {
                            y: stackInputCombo.height
                            width: 180
                            margins: 4
                            padding: 2
                            implicitHeight: contentItem.implicitHeight + 4
                            contentItem: ListView {
                                clip: true
                                implicitHeight: Math.min(contentHeight, 360)
                                model: stackInputCombo.popup.visible ? stackInputCombo.delegateModel : null
                                currentIndex: stackInputCombo.highlightedIndex
                                ScrollIndicator.vertical: ScrollIndicator { }
                            }
                            background: Rectangle {
                                color: "#262626"
                                radius: 4
                                border.color: "#3a3a3a"
                                border.width: 1
                            }
                        }
                    }
                }

                Item { Layout.fillWidth: true }

                Button {
                    id: rotateCcwBtn
                    implicitWidth: 26; implicitHeight: 26
                    Layout.minimumWidth: 26; Layout.preferredWidth: 26; Layout.maximumWidth: 26
                    Layout.minimumHeight: 26; Layout.preferredHeight: 26; Layout.maximumHeight: 26
                    enabled: !compactEditor.cropActive
                    padding: 0
                    flat: true
                    ToolTip.visible: hovered
                    ToolTip.delay: 500
                    ToolTip.text: "Rotate counter-clockwise"
                    onClicked: {
                        compactEditor.ensureEditorLoaded("rotate-ccw")
                        if (compactEditor.controllerRef) compactEditor.controllerRef.rotate_image_ccw()
                    }
                    contentItem: Text {
                        text: "↺"
                        font.pixelSize: 16
                        color: rotateCcwBtn.hovered ? compactEditor.accentColorHover : compactEditor.textColor
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: 4
                        color: rotateCcwBtn.hovered ? "#30ffffff" : "transparent"
                    }
                }

                Button {
                    id: rotateCwBtn
                    implicitWidth: 26; implicitHeight: 26
                    Layout.minimumWidth: 26; Layout.preferredWidth: 26; Layout.maximumWidth: 26
                    Layout.minimumHeight: 26; Layout.preferredHeight: 26; Layout.maximumHeight: 26
                    enabled: !compactEditor.cropActive
                    padding: 0
                    flat: true
                    ToolTip.visible: hovered
                    ToolTip.delay: 500
                    ToolTip.text: "Rotate clockwise"
                    onClicked: {
                        compactEditor.ensureEditorLoaded("rotate-cw")
                        if (compactEditor.controllerRef) compactEditor.controllerRef.rotate_image_cw()
                    }
                    contentItem: Text {
                        text: "↻"
                        font.pixelSize: 16
                        color: rotateCwBtn.hovered ? compactEditor.accentColorHover : compactEditor.textColor
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: 4
                        color: rotateCwBtn.hovered ? "#30ffffff" : "transparent"
                    }
                }

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
                        compactEditor.ensureEditorLoaded("expand")
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

            // --- DODGE & BURN (brush mode, Shift+D) ---
            // While this is shown, the LIGHT / COLOR sliders edit a local
            // recipe: the selected layer's, or the one new strokes get.
            ColumnLayout {
                id: brushPanel
                visible: compactEditor.brushMode
                Layout.fillWidth: true
                spacing: 4

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 4

                    Text {
                        text: compactEditor.stackMode ? "STACK PAINT" : "DODGE & BURN"
                        font.pixelSize: 9
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.0
                        color: compactEditor.panelAccentHover
                    }

                    Item { Layout.fillWidth: true }

                    // Show-mask toggle (same as M).
                    CheckBox {
                        id: brushMaskToggle
                        text: "Mask"
                        padding: 0
                        checked: compactEditor.uiStateRef ? compactEditor.uiStateRef.localOverlayVisible : false
                        onToggled: {
                            if (compactEditor.controllerRef) compactEditor.controllerRef.toggle_local_overlay()
                            checked = Qt.binding(function() { return compactEditor.uiStateRef ? compactEditor.uiStateRef.localOverlayVisible : false })
                        }
                        ToolTip.visible: hovered
                        ToolTip.delay: 500
                        ToolTip.text: "Show mask overlay (M)"
                        indicator: Rectangle {
                            implicitWidth: 12; implicitHeight: 12
                            x: 0; y: (brushMaskToggle.height - height) / 2
                            radius: 2
                            color: brushMaskToggle.checked ? compactEditor.panelAccent : "transparent"
                            border.color: brushMaskToggle.checked ? compactEditor.panelAccent : "#6a6866"
                            border.width: 1
                        }
                        contentItem: Text {
                            text: brushMaskToggle.text
                            leftPadding: 16
                            font.pixelSize: 9
                            color: brushMaskToggle.checked ? "#e8e6e3" : "#9a9795"
                            verticalAlignment: Text.AlignVCenter
                        }
                    }

                    Button {
                        id: brushHelpBtn
                        implicitWidth: 18; implicitHeight: 18
                        Layout.minimumWidth: 18; Layout.preferredWidth: 18; Layout.maximumWidth: 18
                        Layout.minimumHeight: 18; Layout.preferredHeight: 18; Layout.maximumHeight: 18
                        padding: 0
                        flat: true
                        ToolTip.visible: hovered
                        ToolTip.delay: 500
                        ToolTip.text: compactEditor.stackMode ? "How stack paint works" : "How dodge & burn works"
                        onClicked: brushHelpPopup.opened ? brushHelpPopup.close() : brushHelpPopup.open()
                        contentItem: Text {
                            text: "?"
                            font.pixelSize: 10
                            font.weight: Font.DemiBold
                            color: brushHelpBtn.hovered || brushHelpPopup.opened ? "#e8e6e3" : "#9a9795"
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            radius: 9
                            color: brushHelpBtn.hovered || brushHelpPopup.opened ? "#30ffffff" : "#18ffffff"
                            border.color: "#3a3a3a"; border.width: 1
                        }
                    }

                    Button {
                        id: brushDoneBtn
                        implicitWidth: 40; implicitHeight: 18
                        Layout.minimumWidth: 40; Layout.preferredWidth: 40; Layout.maximumWidth: 40
                        Layout.minimumHeight: 18; Layout.preferredHeight: 18; Layout.maximumHeight: 18
                        padding: 0
                        flat: true
                        ToolTip.visible: hovered
                        ToolTip.delay: 500
                        ToolTip.text: "Leave brush mode (Shift+D / Esc)"
                        onClicked: if (compactEditor.controllerRef) compactEditor.controllerRef.exit_local_brush_mode()
                        contentItem: Text {
                            text: "Done"
                            font.pixelSize: 9
                            color: brushDoneBtn.hovered ? "#e8e6e3" : "#9a9795"
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            radius: 2
                            color: brushDoneBtn.hovered ? "#30ffffff" : "#18ffffff"
                            border.color: "#3a3a3a"; border.width: 1
                        }
                    }
                }

                // Stack paint: the input frame new strokes reveal.
                Rectangle {
                    visible: compactEditor.stackMode
                    Layout.fillWidth: true
                    implicitHeight: 22
                    radius: 3
                    color: "#332dd4bf"
                    border.color: compactEditor.stackAccent
                    border.width: 1

                    Text {
                        anchors.fill: parent
                        anchors.leftMargin: 6
                        anchors.rightMargin: 6
                        verticalAlignment: Text.AlignVCenter
                        text: "Painting from: " + compactEditor.stackInputLabel
                        color: compactEditor.textColor
                        font.pixelSize: 10
                        elide: Text.ElideRight
                    }
                }

                // The recipe the next new layer gets. Selected whenever no
                // layer is being edited.
                Rectangle {
                    id: newStrokesRow
                    visible: !compactEditor.stackMode
                    readonly property bool selected: !compactEditor.uiStateRef || compactEditor.uiStateRef.localEditingLayerId === ""
                    Layout.fillWidth: true
                    implicitHeight: 22
                    radius: 3
                    color: newStrokesRow.selected ? "#33f59e0b" : "transparent"
                    border.color: newStrokesRow.selected ? compactEditor.panelAccent : "#30ffffff"
                    border.width: 1

                    Text {
                        anchors.fill: parent
                        anchors.leftMargin: 6
                        anchors.rightMargin: 6
                        verticalAlignment: Text.AlignVCenter
                        text: "New strokes: " + (compactEditor.uiStateRef ? compactEditor.uiStateRef.localBrushSummary : "")
                        color: compactEditor.textColor
                        font.pixelSize: 10
                        elide: Text.ElideRight
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (compactEditor.controllerRef) compactEditor.controllerRef.select_local_layer("")
                            compactEditor.focusKeyboardHandler()
                        }
                    }
                }

                Repeater {
                    model: compactEditor.uiStateRef ? compactEditor.uiStateRef.localLayers : []

                    delegate: Rectangle {
                        id: layerRow
                        required property var modelData
                        readonly property bool isStack: layerRow.modelData.kind === "stack"
                        // Each tool lists only its own layers.
                        visible: layerRow.isStack === compactEditor.stackMode
                        Layout.fillWidth: true
                        implicitHeight: 22
                        radius: 3
                        color: layerRow.modelData.editing ? "#33f59e0b" : (layerMouse.containsMouse ? "#14ffffff" : "transparent")
                        border.color: layerRow.modelData.editing ? compactEditor.panelAccent
                                      : (layerRow.modelData.active ? "#60f59e0b" : "#30ffffff")
                        border.width: 1

                        MouseArea {
                            id: layerMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                if (compactEditor.controllerRef) {
                                    // A stack layer has no recipe: clicking it
                                    // picks its frame for the next strokes.
                                    if (layerRow.isStack) compactEditor.controllerRef.set_stack_input(layerRow.modelData.source)
                                    else compactEditor.controllerRef.select_local_layer(layerRow.modelData.id)
                                }
                                compactEditor.focusKeyboardHandler()
                            }
                        }

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 2
                            anchors.rightMargin: 2
                            spacing: 4

                            Text {
                                Layout.preferredWidth: 18
                                horizontalAlignment: Text.AlignHCenter
                                text: layerRow.modelData.visible ? "◉" : "○"
                                color: layerRow.modelData.visible ? "#e8e6e3" : compactEditor.mutedText
                                font.pixelSize: 11
                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: if (compactEditor.controllerRef) compactEditor.controllerRef.set_local_layer_visible(layerRow.modelData.id, !layerRow.modelData.visible)
                                }
                            }

                            Text {
                                text: layerRow.modelData.name
                                color: layerRow.modelData.visible ? compactEditor.textColor : compactEditor.mutedText
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                            }

                            Text {
                                Layout.fillWidth: true
                                text: layerRow.modelData.summary
                                color: "#9a9795"
                                font.pixelSize: 9
                                elide: Text.ElideRight
                            }

                            Text {
                                Layout.preferredWidth: 16
                                horizontalAlignment: Text.AlignHCenter
                                text: "×"
                                color: deleteMouse.containsMouse ? "#ff8080" : "#9a9795"
                                font.pixelSize: 13
                                MouseArea {
                                    id: deleteMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: if (compactEditor.controllerRef) compactEditor.controllerRef.delete_local_layer(layerRow.modelData.id)
                                }
                            }
                        }
                    }
                }

                // Brush size (log scale: 0.2% .. 25% of the image diagonal)
                // and hardness.
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 6

                    Text {
                        Layout.preferredWidth: 70
                        text: "Size  [ ]"
                        color: compactEditor.textColor
                        font.pixelSize: 11
                    }

                    Slider {
                        id: brushSizeSlider
                        Layout.preferredHeight: 20
                        background: Rectangle {
                            x: brushSizeSlider.leftPadding
                            y: brushSizeSlider.topPadding + brushSizeSlider.availableHeight / 2 - height / 2
                            width: brushSizeSlider.availableWidth
                            height: 4; radius: 2
                            color: "#2e2e2e"; border.color: "#383838"; border.width: 1
                            Rectangle {
                                width: brushSizeSlider.visualPosition * parent.width
                                height: parent.height; radius: 2
                                color: compactEditor.panelAccent; opacity: 0.6
                            }
                        }
                        handle: Rectangle {
                            x: brushSizeSlider.leftPadding + brushSizeSlider.visualPosition * (brushSizeSlider.availableWidth - width)
                            y: brushSizeSlider.topPadding + brushSizeSlider.availableHeight / 2 - height / 2
                            width: 10; height: 10; radius: 5
                            color: "#e8e6e3"
                            border.color: brushSizeSlider.pressed ? compactEditor.panelAccent : "#5a5755"
                            border.width: 1
                        }
                        readonly property real minRadius: 0.002
                        readonly property real maxRadius: 0.25
                        Layout.fillWidth: true
                        focusPolicy: Qt.NoFocus
                        from: 0; to: 100
                        function radiusFromValue(v) { return minRadius * Math.pow(maxRadius / minRadius, v / 100) }
                        Binding {
                            target: brushSizeSlider
                            property: "value"
                            when: !brushSizeSlider.pressed
                            value: compactEditor.uiStateRef
                                   ? 100 * Math.log(compactEditor.uiStateRef.brushRadius / brushSizeSlider.minRadius) / Math.log(brushSizeSlider.maxRadius / brushSizeSlider.minRadius)
                                   : 50
                        }
                        onMoved: if (compactEditor.controllerRef) compactEditor.controllerRef.set_brush_radius(radiusFromValue(value))
                    }

                    Text {
                        Layout.preferredWidth: 32
                        horizontalAlignment: Text.AlignRight
                        text: compactEditor.uiStateRef ? (compactEditor.uiStateRef.brushRadius * 100).toFixed(1) : ""
                        font.family: "IBM Plex Mono"
                        font.pixelSize: 10
                        color: "#e8e6e3"
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 6

                    Text {
                        Layout.preferredWidth: 70
                        text: "Hardness"
                        color: compactEditor.textColor
                        font.pixelSize: 11
                    }

                    Slider {
                        id: brushHardnessSlider
                        Layout.preferredHeight: 20
                        background: Rectangle {
                            x: brushHardnessSlider.leftPadding
                            y: brushHardnessSlider.topPadding + brushHardnessSlider.availableHeight / 2 - height / 2
                            width: brushHardnessSlider.availableWidth
                            height: 4; radius: 2
                            color: "#2e2e2e"; border.color: "#383838"; border.width: 1
                            Rectangle {
                                width: brushHardnessSlider.visualPosition * parent.width
                                height: parent.height; radius: 2
                                color: compactEditor.panelAccent; opacity: 0.6
                            }
                        }
                        handle: Rectangle {
                            x: brushHardnessSlider.leftPadding + brushHardnessSlider.visualPosition * (brushHardnessSlider.availableWidth - width)
                            y: brushHardnessSlider.topPadding + brushHardnessSlider.availableHeight / 2 - height / 2
                            width: 10; height: 10; radius: 5
                            color: "#e8e6e3"
                            border.color: brushHardnessSlider.pressed ? compactEditor.panelAccent : "#5a5755"
                            border.width: 1
                        }
                        Layout.fillWidth: true
                        focusPolicy: Qt.NoFocus
                        from: 0; to: 100
                        Binding {
                            target: brushHardnessSlider
                            property: "value"
                            when: !brushHardnessSlider.pressed
                            value: compactEditor.uiStateRef ? compactEditor.uiStateRef.brushHardness * 100 : 50
                        }
                        onMoved: if (compactEditor.controllerRef) compactEditor.controllerRef.set_brush_hardness(value / 100)
                    }

                    Text {
                        Layout.preferredWidth: 32
                        horizontalAlignment: Text.AlignRight
                        text: compactEditor.uiStateRef ? Math.round(compactEditor.uiStateRef.brushHardness * 100) : ""
                        font.family: "IBM Plex Mono"
                        font.pixelSize: 10
                        color: "#e8e6e3"
                    }
                }

                Text {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: "Drag to paint, right-drag to erase. Press ? for all controls."
                    color: compactEditor.mutedText
                    font.pixelSize: 9
                }

                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: compactEditor.separatorColor; Layout.topMargin: 2 }
            }

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
                    visible: !compactEditor.recipeMode  // auto tools act on the global edit
                    implicitWidth: 36; implicitHeight: 16
                    Layout.minimumWidth: 36; Layout.preferredWidth: 36; Layout.maximumWidth: 36
                    Layout.minimumHeight: 16; Layout.preferredHeight: 16; Layout.maximumHeight: 16
                    enabled: !compactEditor.cropActive
                    padding: 0
                    flat: true
                    onClicked: {
                        compactEditor.ensureEditorLoaded("auto-levels")
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
                ListElement { name: "Brightness"; key: "brightness"; min: -100; max: 100 }
                ListElement { name: "Contrast"; key: "contrast"; min: -100; max: 100 }
                ListElement { name: "Highlights"; key: "highlights"; min: -100; max: 100 }
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
                    visible: !compactEditor.recipeMode  // auto tools act on the global edit
                    implicitWidth: 36; implicitHeight: 16
                    Layout.minimumWidth: 36; Layout.preferredWidth: 36; Layout.maximumWidth: 36
                    Layout.minimumHeight: 16; Layout.preferredHeight: 16; Layout.maximumHeight: 16
                    enabled: !compactEditor.cropActive
                    padding: 0
                    flat: true
                    onClicked: {
                        compactEditor.ensureEditorLoaded("auto-white-balance")
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

            // --- COLOR MIX Section (per-hue saturation) ---
            // Eight hue bands; collapsed by default to keep the narrow panel tidy.
            MouseArea {
                Layout.fillWidth: true
                Layout.topMargin: 4
                implicitHeight: 16
                cursorShape: Qt.PointingHandCursor
                onClicked: compactEditor.colorMixExpanded = !compactEditor.colorMixExpanded

                Text {
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    text: (compactEditor.colorMixExpanded ? "▾ " : "▸ ") + "COLOR MIX"
                    font.pixelSize: 9
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1.0
                    color: "#9a9795"
                }
            }

            ListModel {
                id: colorMixModel
                ListElement { name: "Red"; key: "color_sat_red"; min: -100; max: 100 }
                ListElement { name: "Orange"; key: "color_sat_orange"; min: -100; max: 100 }
                ListElement { name: "Yellow"; key: "color_sat_yellow"; min: -100; max: 100 }
                ListElement { name: "Green"; key: "color_sat_green"; min: -100; max: 100 }
                ListElement { name: "Aqua"; key: "color_sat_aqua"; min: -100; max: 100 }
                ListElement { name: "Blue"; key: "color_sat_blue"; min: -100; max: 100 }
                ListElement { name: "Purple"; key: "color_sat_purple"; min: -100; max: 100 }
                ListElement { name: "Magenta"; key: "color_sat_magenta"; min: -100; max: 100 }
            }
            Repeater {
                model: compactEditor.colorMixExpanded ? colorMixModel : 0
                delegate: compactSlider
            }

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
                        compactEditor.ensureEditorLoaded("reset")
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
                        compactEditor.ensureEditorLoaded("save")
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

            readonly property bool isHighlighted: compactEditor.highlightedSliderKey === sliderRow.key
            onIsHighlightedChanged: {
                if (sliderRow.isHighlighted) Qt.callLater(compactEditor.ensureRowVisible, sliderRow)
            }

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
                            compactEditor.setEditValue(sliderRow.key, slider.editValueFromSliderValue(slider._pendingValue))
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
                    compactEditor.ensureEditorLoaded("slider-reset")
                    compactEditor.highlightedSliderKey = sliderRow.key
                    slider.isResetting = true
                    sendTimer.stop()
                    compactEditor.setEditValue(sliderRow.key, 0.0)
                    slider.value = 0.0
                    _pendingValue = 0.0
                    slider._lastSentValue = 0.0
                    compactEditor.updatePulse++
                    resetTimer.restart()
                }

                onPressedChanged: {
                    if (pressed) {
                        if (compactEditor.controllerRef) compactEditor.controllerRef.set_editor_slider_drag_active(true)
                        compactEditor.ensureEditorLoaded("slider-change")
                        compactEditor.highlightedSliderKey = sliderRow.key
                        compactEditor.focusKeyboardHandler()
                        if (!slider.isResetting) {
                            _pendingValue = value
                            slider._lastSentValue = value
                            if (!sendTimer.running) sendTimer.start()
                        }
                    } else {
                        sendTimer.stop()
                        if (slider.isResetting) {
                            compactEditor.setEditValue(sliderRow.key, 0.0)
                        } else {
                            compactEditor.setEditValue(sliderRow.key, slider.editValueFromSliderValue(value))
                        }
                        if (compactEditor.controllerRef) compactEditor.controllerRef.update_histogram()
                        if (compactEditor.controllerRef) compactEditor.controllerRef.set_editor_slider_drag_active(false)
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
