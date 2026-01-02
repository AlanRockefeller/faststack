import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15

Window {
    id: imageEditorDialog
    width: 800
    height: 750
    title: "Image Editor"
    visible: uiState ? uiState.isEditorOpen : false
    flags: Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
    property int updatePulse: 0
    property color backgroundColor: "#1e1e1e" // Default dark background
    property color textColor: "white" // Default text color

    // Modern Color Palette
    readonly property color accentColor: "#6366f1" // Modern Indigo
    readonly property color accentColorHover: "#818cf8"
    readonly property color accentColorSubtle: "#306366f1"
    readonly property color controlBg: "#10ffffff"
    readonly property color controlBorder: "#30ffffff"
    readonly property color separatorColor: "#20ffffff"

    Material.theme: (uiState && uiState.theme === 0) ? Material.Dark : Material.Light
    Material.accent: accentColor

    onClosing: (close) => {
        uiState.isEditorOpen = false
    }

    onVisibleChanged: {
        if (visible) {
            if (controller) controller.update_histogram()
        }
    }
    
    // Auto-update histogram when pulse changes (buttons, double-taps, spinbox)
    onUpdatePulseChanged: {
        if (visible && controller) {
            controller.update_histogram()
        }
    }

    property int slidersPressedCount: 0
    onSlidersPressedCountChanged: {
        uiState.setAnySliderPressed(slidersPressedCount > 0)
    }

    function getBackendValue(key) {
        var _dependency = updatePulse;
        if (uiState && key in uiState) return uiState[key];
        return 0.0;
    }

    // Background
    color: imageEditorDialog.backgroundColor

    Shortcut {
        sequence: "Escape"
        context: Qt.WindowShortcut
        onActivated: {
            uiState.isEditorOpen = false
        }
    }
    Shortcut {
        sequence: "S"
        context: Qt.WindowShortcut
        onActivated: {
            controller.save_edited_image()
            uiState.isEditorOpen = false
        }
    }

    // Component for Section Separator
    Component {
        id: sectionSeparator
        Rectangle {
            Layout.fillWidth: true
            Layout.topMargin: 20
            Layout.bottomMargin: 5
            height: 1
            color: imageEditorDialog.separatorColor
        }
    }

    // Component for Section Header
    Component {
        id: sectionHeader
        Label {
            text: headerText
            font.bold: true
            font.pixelSize: 15
            font.letterSpacing: 1.0
            color: imageEditorDialog.accentColorHover
            Layout.topMargin: 5
            Layout.bottomMargin: 10
            
            property string headerText: ""
        }
    }

    ScrollView {
        anchors.fill: parent
        anchors.margins: 10
        anchors.topMargin: 5
        clip: true
        contentWidth: availableWidth

        RowLayout {
            width: parent.width
            spacing: 30

            // --- LEFT COLUMN ---
            ColumnLayout { 
                Layout.fillWidth: true
                Layout.preferredWidth: (parent.width - 30) / 2
                Layout.alignment: Qt.AlignTop
                spacing: 15

                // --- Light Group ---
                Loader { 
                    sourceComponent: sectionHeader 
                    property string headerText: "☀ Light" 
                    Layout.topMargin: 0 // Remove top margin for the very first item
                }
                ListModel {
                    id: lightModel
                    ListElement { name: "Exposure"; key: "exposure" }
                    ListElement { name: "Brightness"; key: "brightness" }
                    ListElement { name: "Highlights"; key: "highlights" }
                    ListElement { name: "Whites"; key: "whites" }
                    ListElement { name: "Shadows"; key: "shadows" }
                    ListElement { name: "Blacks"; key: "blacks" }
                    ListElement { name: "Contrast"; key: "contrast" }
                }
                Repeater { model: lightModel; delegate: editSlider }

                Loader { sourceComponent: sectionSeparator }

                // --- Detail Group ---
                Loader { sourceComponent: sectionHeader; property string headerText: "🔍 Detail" }
                ListModel {
                    id: detailModel
                    ListElement { name: "Clarity"; key: "clarity" }
                    ListElement { name: "Texture"; key: "texture" }
                    ListElement { name: "Sharpness"; key: "sharpness" }
                }
                Repeater { model: detailModel; delegate: editSlider }

                // --- Histogram Group ---
                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 120
                    Layout.topMargin: 5
                    spacing: 5
                    
                    SingleChannelHistogram {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        
                        channelName: "R"
                        channelColor: "#e15050"
                        gridLineColor: imageEditorDialog.controlBorder
                        dangerColor: "#40ff0000"
                        textColor: imageEditorDialog.textColor
                        minimal: true
                        
                        histogramData: uiState && uiState.histogramData ? (uiState.histogramData["r"] || []) : []
                        clipCount: uiState && uiState.histogramData ? (uiState.histogramData["r_clip"] || 0) : 0
                        preClipCount: uiState && uiState.histogramData ? (uiState.histogramData["r_preclip"] || 0) : 0
                    }
                    
                    SingleChannelHistogram {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        
                        channelName: "G"
                        channelColor: "#50e150"
                        gridLineColor: imageEditorDialog.controlBorder
                        dangerColor: "#40ff0000"
                        textColor: imageEditorDialog.textColor
                        minimal: true
                        
                        histogramData: uiState && uiState.histogramData ? (uiState.histogramData["g"] || []) : []
                        clipCount: uiState && uiState.histogramData ? (uiState.histogramData["g_clip"] || 0) : 0
                        preClipCount: uiState && uiState.histogramData ? (uiState.histogramData["g_preclip"] || 0) : 0
                    }

                    SingleChannelHistogram {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        
                        channelName: "B"
                        channelColor: "#5050e1"
                        gridLineColor: imageEditorDialog.controlBorder
                        dangerColor: "#40ff0000"
                        textColor: imageEditorDialog.textColor
                        minimal: true
                        
                        histogramData: uiState && uiState.histogramData ? (uiState.histogramData["b"] || []) : []
                        clipCount: uiState && uiState.histogramData ? (uiState.histogramData["b_clip"] || 0) : 0
                        preClipCount: uiState && uiState.histogramData ? (uiState.histogramData["b_preclip"] || 0) : 0
                    }
                }
            }

            // --- RIGHT COLUMN ---
            ColumnLayout { 
                Layout.fillWidth: true
                Layout.preferredWidth: (parent.width - 30) / 2
                Layout.alignment: Qt.AlignTop
                spacing: 15

                // --- Color Group ---
                Loader { 
                    sourceComponent: sectionHeader 
                    property string headerText: "🎨 Color" 
                    Layout.topMargin: 0 // Remove top margin for the very first item
                }
                ListModel {
                    id: colorModel
                    ListElement { name: "Saturation"; key: "saturation"; reverse: false }
                    ListElement { name: "Vibrance"; key: "vibrance"; reverse: false }
                    ListElement { name: "Temp (Blue/Yel)"; key: "white_balance_by"; reverse: false }
                    ListElement { name: "Tint (Grn/Mag)"; key: "white_balance_mg"; reverse: false }
                }
                Repeater { model: colorModel; delegate: editSlider }
                
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Button {
                        text: "Auto WB"
                        Layout.fillWidth: true
                        font.pixelSize: 12
                        onClicked: {
                            controller.auto_white_balance()
                            imageEditorDialog.updatePulse++
                        }
                    }
                    Button {
                        text: "Auto Levels"
                        Layout.fillWidth: true
                        font.pixelSize: 12
                        onClicked: {
                            controller.auto_levels()
                            imageEditorDialog.updatePulse++
                        }
                    }
                }

                Loader { sourceComponent: sectionSeparator }

                // --- Effects Group ---
                Loader { sourceComponent: sectionHeader; property string headerText: "✨ Effects" }
                ListModel {
                    id: effectsModel
                    ListElement { name: "Vignette"; key: "vignette"; min: 0; max: 100 }
                }
                Repeater { model: effectsModel; delegate: editSlider }

                Loader { sourceComponent: sectionSeparator }

                // --- Transform Group ---
                Loader { sourceComponent: sectionHeader; property string headerText: "🔄 Transform" }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 15
                    Label { 
                        text: "Rotation" 
                        color: imageEditorDialog.textColor 
                        font.pixelSize: 14
                    }
                    Item { Layout.fillWidth: true } // Spacer
                    Button { 
                        text: "↶ -90°" 
                        onClicked: controller.rotate_image_ccw()
                        Layout.preferredWidth: 80
                    }
                    Button { 
                        text: "↷ +90°" 
                        onClicked: controller.rotate_image_cw()
                        Layout.preferredWidth: 80
                    }
                }

                // --- Action Buttons ---
                Item { Layout.fillHeight: true; Layout.minimumHeight: 30 }
                
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10

                    // Reset (Tertiary)
                    Button { 
                        text: "Reset"
                        flat: true
                        Layout.preferredWidth: 80
                        Material.foreground: imageEditorDialog.textColor
                        onClicked: {
                            controller.reset_edit_parameters()
                            imageEditorDialog.updatePulse++
                        }
                        background: Rectangle {
                            color: parent.pressed ? "#20ffffff" : "transparent"
                            radius: 4
                            border.color: parent.hovered ? "#40ffffff" : "transparent"
                        }
                    }

                    Item { Layout.fillWidth: true } // Spacer

                    // Close (Secondary)
                    Button { 
                        text: "Close"
                        Layout.preferredWidth: 100
                        onClicked: { 
                            uiState.isEditorOpen = false
                        }
                        contentItem: Text {
                            text: parent.text
                            font: parent.font
                            opacity: enabled ? 1.0 : 0.3
                            color: imageEditorDialog.textColor
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            color: parent.pressed ? "#40ffffff" : "#20ffffff"
                            radius: 4
                            border.color: parent.hovered ? "#60ffffff" : "transparent"
                        }
                    }

                    // Save (Primary)
                    Button { 
                        text: "Save"
                        Layout.preferredWidth: 100
                        highlighted: true
                        Material.background: imageEditorDialog.accentColor
                        onClicked: {
                            controller.save_edited_image()
                            uiState.isEditorOpen = false
                        }
                        background: Rectangle {
                            color: parent.pressed ? Qt.darker(imageEditorDialog.accentColor, 1.1) : imageEditorDialog.accentColor
                            radius: 4
                            // Subtle shadow simulation
                            layer.enabled: true
                        }
                    }
                }
            }
        }
    }

    Component {
        id: editSlider
        RowLayout {
            Layout.fillWidth: true
            spacing: 15
            
            property bool isReversed: model.reverse !== undefined ? model.reverse : false
            property real minVal: model.min === undefined ? -100 : model.min
            property real maxVal: model.max === undefined ? 100 : model.max
            
            // Label
            Text {
                text: model.name
                color: imageEditorDialog.textColor
                font.pixelSize: 13
                font.weight: Font.Medium
                Layout.preferredWidth: 90
                Layout.alignment: Qt.AlignVCenter
                elide: Text.ElideRight
            }
            
            // Slider
            Slider {
                id: slider
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
                from: minVal
                to: maxVal
                stepSize: 1
                
                property real backendValue: {
                    var val = imageEditorDialog.getBackendValue(model.key) * maxVal
                    return isReversed ? -val : val
                }
                
                value: backendValue
                
                Connections {
                    target: imageEditorDialog
                    function onUpdatePulseChanged() {
                        if (!slider.pressed) {
                            slider.value = slider.backendValue
                        }
                    }
                }
                
                onMoved: {
                    var sendValue = isReversed ? -value : value
                    controller.set_edit_parameter(model.key, sendValue / maxVal)
                    // Trigger live histogram update (throttled by Python backend)
                    if (controller) controller.update_histogram()
                }
                
                property double lastPressTime: 0
                property double lastPressValue: 0
                
                onPressedChanged: {
                    if (pressed) {
                        var now = Date.now()
                        var range = slider.to - slider.from
                        var diff = Math.abs(value - lastPressValue)
                        
                        // Double click detection: <500ms time diff AND <5% value diff
                        // This prevents false positives when dragging quickly
                        if (now - lastPressTime < 500 && diff < (range * 0.05)) { 
                             controller.set_edit_parameter(model.key, 0.0)
                             imageEditorDialog.updatePulse++
                             value = 0.0
                        }
                        lastPressTime = now
                        lastPressValue = value
                        
                        imageEditorDialog.slidersPressedCount++
                    } else {
                        imageEditorDialog.slidersPressedCount--
                        // Update histogram on release
                        if (controller) controller.update_histogram()
                    }
                }
                
                onBackendValueChanged: {
                    if (!pressed) {
                        value = backendValue
                    }
                }

                // Smooth transition for value changes from backend
                Behavior on value {
                    enabled: !slider.pressed
                    NumberAnimation { duration: 200; easing.type: Easing.OutQuad }
                }
                
                background: Item {
                    x: slider.leftPadding
                    y: slider.topPadding + slider.availableHeight / 2 - height / 2
                    width: slider.availableWidth
                    height: 6
                    
                    // Track Background
                    Rectangle {
                        anchors.fill: parent
                        radius: 3
                        color: imageEditorDialog.controlBg
                        border.color: imageEditorDialog.controlBorder
                        border.width: 1
                    }

                    // Fill Indicator (From 0/Center to Value)
                    Rectangle {
                        id: fillRect
                        property real range: slider.to - slider.from
                        // Determine anchor point (0 if within range, else min or max)
                        property real anchorVal: Math.max(slider.from, Math.min(slider.to, 0))
                        property real anchorPos: (anchorVal - slider.from) / range
                        
                        x: Math.min(slider.visualPosition, anchorPos) * parent.width
                        width: Math.abs(slider.visualPosition - anchorPos) * parent.width
                        height: parent.height
                        radius: 3
                        color: imageEditorDialog.accentColor
                        opacity: 0.6 // Reduced opacity as requested
                        
                        Behavior on width { NumberAnimation { duration: 100 } }
                        Behavior on x { NumberAnimation { duration: 100 } }
                    }
                }

                handle: Rectangle {
                     x: slider.leftPadding + slider.visualPosition * (slider.availableWidth - width)
                     y: slider.topPadding + slider.availableHeight / 2 - height / 2
                     width: 12
                     height: 12
                     radius: 6
                     color: slider.pressed ? imageEditorDialog.accentColor : "white"
                     border.color: slider.pressed ? "white" : imageEditorDialog.accentColor
                     border.width: 2
                     
                     // Glow/Scale effect on hover
                     scale: hoverHandler.hovered || slider.pressed ? 1.3 : 1.0
                     Behavior on scale { NumberAnimation { duration: 150; easing.type: Easing.OutBack } }
                     Behavior on color { ColorAnimation { duration: 150 } }

                     HoverHandler {
                         id: hoverHandler
                     }
                }
            }

            // Refined SpinBox
            SpinBox {
                id: valueInput
                from: minVal
                to: maxVal
                stepSize: 1
                editable: true
                Layout.preferredWidth: 80
                Layout.alignment: Qt.AlignVCenter
                
                value: isReversed ? -slider.value : slider.value
                
                onValueModified: {
                     var val = value
                     var sendValue = isReversed ? -val : val
                     controller.set_edit_parameter(model.key, sendValue / maxVal)
                     imageEditorDialog.updatePulse++ 
                }

                contentItem: TextInput {
                    z: 2
                    text: valueInput.textFromValue(valueInput.value, valueInput.locale)
                    font.pixelSize: 12
                    font.family: valueInput.font.family
                    color: imageEditorDialog.textColor
                    selectionColor: imageEditorDialog.accentColor
                    selectedTextColor: "#ffffff"
                    horizontalAlignment: Qt.AlignHCenter
                    verticalAlignment: Qt.AlignVCenter
                    readOnly: !valueInput.editable
                    validator: valueInput.validator
                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                    
                    // Highlight on focus
                    onActiveFocusChanged: {
                        if(activeFocus) valueInputBackground.border.color = imageEditorDialog.accentColor
                        else valueInputBackground.border.color = imageEditorDialog.controlBorder
                    }
                }

                up.indicator: Item {
                    x: valueInput.mirrored ? 0 : parent.width - width
                    height: parent.height
                    width: 16 // Smaller button
                    
                    Rectangle {
                        anchors.centerIn: parent
                        width: 16; height: 16
                        radius: 2
                        color: valueInput.up.pressed ? imageEditorDialog.accentColor : (valueInput.up.hovered ? Qt.lighter(imageEditorDialog.controlBg, 1.5) : "transparent")
                        
                        Text {
                            text: "+"
                            font.pixelSize: 12
                            anchors.centerIn: parent
                            color: valueInput.up.pressed ? "white" : imageEditorDialog.textColor
                        }
                    }
                }

                down.indicator: Item {
                    x: valueInput.mirrored ? parent.width - width : 0
                    height: parent.height
                    width: 16 // Smaller button
                    
                    Rectangle {
                        anchors.centerIn: parent
                        width: 16; height: 16
                        radius: 2
                        color: valueInput.down.pressed ? imageEditorDialog.accentColor : (valueInput.down.hovered ? Qt.lighter(imageEditorDialog.controlBg, 1.5) : "transparent")
                        
                        Text {
                            text: "-"
                            font.pixelSize: 12
                            anchors.centerIn: parent
                            color: valueInput.down.pressed ? "white" : imageEditorDialog.textColor
                        }
                    }
                }

                background: Rectangle {
                    id: valueInputBackground
                    implicitWidth: 80
                    color: "transparent"
                    border.color: imageEditorDialog.controlBorder
                    border.width: 1
                    radius: 4
                    
                    Behavior on border.color { ColorAnimation { duration: 150 } }
                }
            }
        }
    }
}