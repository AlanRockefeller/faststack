import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15

Window {
    id: darkenPanel
    width: 380
    height: 700
    title: "Background Darkening"
    visible: uiState ? uiState.isDarkening : false
    flags: Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint

    property color backgroundColor: "#1e1e1e"
    property color textColor: "white"

    readonly property color accentColor: "#6366f1"
    readonly property color accentColorHover: "#818cf8"
    readonly property color controlBg: "#10ffffff"
    readonly property color controlBorder: "#30ffffff"
    readonly property color separatorColor: "#20ffffff"

    Material.theme: Material.Dark
    Material.accent: accentColor

    color: backgroundColor

    onClosing: (close) => {
        if (uiState) uiState.isDarkening = false
    }

    Shortcut {
        sequence: "Escape"
        context: Qt.WindowShortcut
        onActivated: {
            if (uiState) uiState.isDarkening = false
        }
    }

    ScrollView {
        anchors.fill: parent
        anchors.margins: 12
        clip: true
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
            spacing: 10

            // --- Mode Selector ---
            Label {
                text: "Mode"
                color: darkenPanel.accentColorHover
                font.bold: true
                font.pixelSize: 14
                font.letterSpacing: 1.0
                Layout.bottomMargin: 4
            }

            ComboBox {
                id: modeCombo
                Layout.fillWidth: true
                model: ["Assisted", "Paint Only", "Strong Subject", "Border Auto"]
                currentIndex: {
                    var m = uiState ? uiState.darkenMode : "assisted"
                    if (m === "paint_only") return 1
                    if (m === "strong_subject") return 2
                    if (m === "border_auto") return 3
                    return 0
                }
                onActivated: (index) => {
                    var modes = ["assisted", "paint_only", "strong_subject", "border_auto"]
                    if (controller) controller.set_darken_mode(modes[index])
                }

                ToolTip.visible: hovered
                ToolTip.delay: 500
                ToolTip.text: {
                    var m = uiState ? uiState.darkenMode : "assisted"
                    if (m === "paint_only")
                        return "Paint Only: Only your brush strokes define the mask.\nNo automatic detection — full manual control.\nBest for precise, targeted darkening."
                    if (m === "strong_subject")
                        return "Strong Subject: Uses edge detection to strongly protect\nthe subject. Your brush strokes guide which areas to\ndarken, but edges are aggressively preserved.\nBest for images with clear foreground subjects."
                    if (m === "border_auto")
                        return "Border Auto: Automatically darkens edges/borders of\nthe image. Minimal brushwork needed — just adjust\nthe sliders. Best for quick vignette-like darkening."
                    return "Assisted: Your brush strokes are combined with\nautomatic edge detection to create a natural mask.\nThe algorithm helps blend your strokes smoothly.\nBest general-purpose mode for most images."
                }
            }

            // --- Separator ---
            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: 8
                Layout.bottomMargin: 4
                height: 1
                color: darkenPanel.separatorColor
            }

            // --- Darkening Controls ---
            Label {
                text: "Darkening"
                color: darkenPanel.accentColorHover
                font.bold: true
                font.pixelSize: 14
                font.letterSpacing: 1.0
                Layout.bottomMargin: 4
            }

            DarkenSlider {
                label: "Amount"
                paramKey: "darken_amount"
                value: uiState ? uiState.darkenAmount * 100 : 50
                tooltip: "How much to darken the masked background areas.\n0 = no darkening, 100 = maximum darkening.\nStart around 30–50 and adjust to taste."
            }
            DarkenSlider {
                label: "Edge Protection"
                paramKey: "edge_protection"
                value: uiState ? uiState.darkenEdgeProtection * 100 : 50
                tooltip: "Prevents darkening near strong edges (subject outlines).\nHigher values keep a brighter halo around sharp\nedges, avoiding unnatural dark fringing.\nUseful when the mask bleeds into the subject."
            }
            DarkenSlider {
                label: "Subject Protection"
                paramKey: "subject_protection"
                value: uiState ? uiState.darkenSubjectProtection * 100 : 50
                tooltip: "Protects bright, saturated areas from darkening.\nHigher values preserve subject colors and highlights.\nHelps when the mask accidentally covers the subject."
            }

            // --- Separator ---
            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: 8
                Layout.bottomMargin: 4
                height: 1
                color: darkenPanel.separatorColor
            }

            // --- Mask Refinement ---
            Label {
                text: "Mask Refinement"
                color: darkenPanel.accentColorHover
                font.bold: true
                font.pixelSize: 14
                font.letterSpacing: 1.0
                Layout.bottomMargin: 4
            }

            DarkenSlider {
                label: "Feather"
                paramKey: "feather"
                value: uiState ? uiState.darkenFeather * 100 : 50
                tooltip: "Softens the mask edges for a gradual transition.\n0 = hard edge (sharp boundary between dark and light),\n100 = very soft edge (wide gradient).\nHigher values give a more natural, blended look."
            }
            DarkenSlider {
                label: "Dark Range"
                paramKey: "dark_range"
                value: uiState ? uiState.darkenDarkRange * 100 : 50
                tooltip: "Controls how the mask interacts with already-dark areas.\nHigher values extend the mask into darker tones,\nlower values focus darkening on midtones and highlights.\nUseful for controlling shadow depth."
            }
            DarkenSlider {
                label: "Neutrality"
                paramKey: "neutrality_sensitivity"
                value: uiState ? uiState.darkenNeutrality * 100 : 50
                tooltip: "Sensitivity to neutral (grey/unsaturated) colors.\nHigher values cause the mask to prefer darkening\nneutral areas while leaving colorful areas alone.\nHelps isolate plain backgrounds from colorful subjects."
            }
            DarkenSlider {
                label: "Expand / Contract"
                paramKey: "expand_contract"
                minVal: -100
                value: uiState ? uiState.darkenExpandContract * 100 : 0
                tooltip: "Grows or shrinks the mask boundary.\nPositive values expand the darkened area outward,\nnegative values contract it inward.\nUse to fine-tune where darkening starts and stops."
            }
            // Auto From Edges — plumbed but not yet wired into resolve_mask(); hidden until implemented
            // DarkenSlider { label: "Auto From Edges"; paramKey: "auto_from_edges"; value: uiState ? uiState.darkenAutoEdges * 100 : 0; minVal: 0 }

            // --- Separator ---
            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: 8
                Layout.bottomMargin: 4
                height: 1
                color: darkenPanel.separatorColor
            }

            // --- Brush ---
            Label {
                text: "Brush"
                color: darkenPanel.accentColorHover
                font.bold: true
                font.pixelSize: 14
                font.letterSpacing: 1.0
                Layout.bottomMargin: 4
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                Label {
                    text: "Size"
                    color: darkenPanel.textColor
                    font.pixelSize: 13
                    Layout.preferredWidth: 90

                    ToolTip.visible: brushSizeMA.containsMouse
                    ToolTip.delay: 500
                    ToolTip.text: "Brush radius for painting mask strokes.\nLarger brush = faster coverage of big areas.\nSmaller brush = more precise control."
                    MouseArea { id: brushSizeMA; anchors.fill: parent; hoverEnabled: true }
                }
                Slider {
                    id: brushSlider
                    Layout.fillWidth: true
                    from: 1; to: 100; stepSize: 1
                    value: uiState ? uiState.darkenBrushRadius * 1000 : 30
                    onMoved: {
                        if (controller) controller.set_darken_param("brush_radius", value / 1000.0)
                    }
                }
                Label {
                    text: Math.round(brushSlider.value).toString()
                    color: darkenPanel.textColor
                    font.pixelSize: 12
                    Layout.preferredWidth: 30
                }
            }

            Label {
                text: "Left-click: paint background | Right-click: protect subject"
                color: darkenPanel.textColor
                opacity: 0.6
                font.pixelSize: 11
                font.italic: true
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // --- Separator ---
            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: 8
                Layout.bottomMargin: 4
                height: 1
                color: darkenPanel.separatorColor
            }

            // --- Overlay Controls ---
            Label {
                text: "Overlay"
                color: darkenPanel.accentColorHover
                font.bold: true
                font.pixelSize: 14
                font.letterSpacing: 1.0
                Layout.bottomMargin: 4
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                CheckBox {
                    text: "Show Overlay"
                    checked: uiState ? uiState.darkenOverlayVisible : true
                    onToggled: {
                        if (controller) controller.set_darken_overlay_visible(checked)
                    }
                    Material.accent: darkenPanel.accentColor

                    ToolTip.visible: hovered
                    ToolTip.delay: 500
                    ToolTip.text: "Show or hide the colored mask overlay on the image.\nThe overlay helps you see exactly which areas will\nbe darkened. Toggle off to see the actual result."
                }
            }

            // Colour swatches
            RowLayout {
                Layout.fillWidth: true
                spacing: 6
                Label {
                    text: "Color:"
                    color: darkenPanel.textColor
                    font.pixelSize: 13

                    ToolTip.visible: colorLabelMA.containsMouse
                    ToolTip.delay: 500
                    ToolTip.text: "Choose the overlay color.\nThis only affects the preview overlay — it does\nnot change the actual darkening result."
                    MouseArea { id: colorLabelMA; anchors.fill: parent; hoverEnabled: true }
                }
                Repeater {
                    model: [
                        {"name": "Blue", "r": 80, "g": 120, "b": 255},
                        {"name": "Red", "r": 255, "g": 80, "b": 80},
                        {"name": "Green", "r": 80, "g": 255, "b": 120},
                        {"name": "Yellow", "r": 255, "g": 255, "b": 80},
                        {"name": "Magenta", "r": 255, "g": 80, "b": 255},
                        {"name": "Cyan", "r": 80, "g": 255, "b": 255}
                    ]
                    Rectangle {
                        width: 24; height: 24; radius: 4
                        color: Qt.rgba(modelData.r / 255, modelData.g / 255, modelData.b / 255, 1.0)
                        border.color: "white"
                        border.width: 1
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                if (controller) controller.set_darken_overlay_color(modelData.r, modelData.g, modelData.b)
                            }
                        }
                    }
                }
            }

            // --- Separator ---
            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: 8
                Layout.bottomMargin: 4
                height: 1
                color: darkenPanel.separatorColor
            }

            // --- Action Buttons ---
            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                Button {
                    text: "Undo Stroke"
                    Layout.fillWidth: true
                    onClicked: { if (controller) controller.undo_darken_stroke() }
                    contentItem: Text { text: parent.text; font: parent.font; color: darkenPanel.textColor; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                    background: Rectangle { color: parent.pressed ? "#40ffffff" : "#20ffffff"; radius: 4; border.color: parent.hovered ? "#60ffffff" : "transparent" }

                    ToolTip.visible: hovered
                    ToolTip.delay: 500
                    ToolTip.text: "Remove the last brush stroke you painted."
                }

                Button {
                    text: "Clear All"
                    Layout.fillWidth: true
                    onClicked: { if (controller) controller.clear_darken_strokes() }
                    contentItem: Text { text: parent.text; font: parent.font; color: darkenPanel.textColor; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                    background: Rectangle { color: parent.pressed ? "#40ffffff" : "#20ffffff"; radius: 4; border.color: parent.hovered ? "#60ffffff" : "transparent" }

                    ToolTip.visible: hovered
                    ToolTip.delay: 500
                    ToolTip.text: "Remove all brush strokes and start fresh."
                }
            }

            // --- Close Button ---
            Button {
                Layout.fillWidth: true
                Layout.topMargin: 6
                text: "Close (K)"
                onClicked: { if (controller) controller.toggle_darken_mode() }
                contentItem: Text { text: parent.text; font: parent.font; color: darkenPanel.textColor; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                background: Rectangle { color: parent.pressed ? "#40ffffff" : "#20ffffff"; radius: 4; border.color: parent.hovered ? darkenPanel.accentColor : "#60ffffff" }

                ToolTip.visible: hovered
                ToolTip.delay: 500
                ToolTip.text: "Close the darkening panel.\nThe darkening effect stays applied to the image.\nPress K again to reopen."
            }

            // Spacer
            Item { Layout.fillHeight: true; Layout.minimumHeight: 10 }
        }
    }

    // --- Darken Slider Component ---
    component DarkenSlider: RowLayout {
        property string label: ""
        property string paramKey: ""
        property real value: 0
        property real minVal: 0
        property real maxVal: 100
        property string tooltip: ""

        Layout.fillWidth: true
        spacing: 10

        Label {
            text: label
            color: darkenPanel.textColor
            font.pixelSize: 13
            Layout.preferredWidth: 110
            elide: Text.ElideRight

            ToolTip.visible: sliderLabelMA.containsMouse && tooltip !== ""
            ToolTip.delay: 500
            ToolTip.text: tooltip
            MouseArea { id: sliderLabelMA; anchors.fill: parent; hoverEnabled: true }
        }

        Slider {
            id: dSlider
            Layout.fillWidth: true
            from: minVal; to: maxVal; stepSize: 1
            value: parent.value

            property real _pendingValue: 0
            property real _lastSentValue: 0
            Timer {
                id: dsendTimer
                interval: 16
                repeat: true
                onTriggered: {
                    if (Math.abs(dSlider._pendingValue - dSlider._lastSentValue) > 0.001) {
                        if (controller) controller.set_darken_param(paramKey, dSlider._pendingValue / maxVal)
                        dSlider._lastSentValue = dSlider._pendingValue
                    }
                }
            }

            onPressedChanged: {
                if (pressed) {
                    _pendingValue = value
                    _lastSentValue = value
                    if (!dsendTimer.running) dsendTimer.start()
                } else {
                    dsendTimer.stop()
                    if (controller) controller.set_darken_param(paramKey, value / maxVal)
                }
            }
            onMoved: {
                _pendingValue = value
                if (!dsendTimer.running) dsendTimer.start()
            }
        }

        Label {
            text: Math.round(dSlider.value).toString()
            color: darkenPanel.textColor
            font.pixelSize: 12
            Layout.preferredWidth: 30
        }
    }
}
