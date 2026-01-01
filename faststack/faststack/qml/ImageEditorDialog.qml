import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15

Window {
    id: imageEditorDialog
    width: 720
    height: 700
    title: "Image Editor"
    visible: uiState ? uiState.isEditorOpen : false
    flags: Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
    property int updatePulse: 0
    property color backgroundColor: "red" // Placeholder, will be set from Main.qml
    property color textColor: "white" // Placeholder, will be set from Main.qml


    Material.theme: (uiState && uiState.theme === 0) ? Material.Dark : Material.Light
    Material.accent: "#4fb360"

    // When the dialog is closed by the user (e.g. clicking X), update the state
    // Use onClosing to handle the window close event (e.g. usage of the X button)
    // Use onClosing to handle the window close event (e.g. usage of the X button)
    onClosing: (close) => {
        uiState.isEditorOpen = false
        // We accept the close event, letting the window hide/close naturally.
        // The binding to 'visible' will update next time uiState.isEditorOpen becomes true.
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
        sequence: "E"
        context: Qt.WindowShortcut
        onActivated: {
            uiState.isEditorOpen = false
        }
    }
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

    ScrollView {
        anchors.fill: parent
        anchors.margins: 10
        clip: true
        contentWidth: availableWidth

        RowLayout {
            width: parent.width
            spacing: 20

            ColumnLayout { // Left Column
                Layout.fillWidth: true
                Layout.preferredWidth: (parent.width - 20) / 2
                Layout.alignment: Qt.AlignTop
                spacing: 2

                // --- Light Group ---
                Label { text: "Light"; font.bold: true; color: imageEditorDialog.textColor }
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

                // --- Detail Group ---
                Label { text: "Detail"; font.bold: true; color: imageEditorDialog.textColor; Layout.topMargin: 10 }
                ListModel {
                    id: detailModel
                    ListElement { name: "Clarity"; key: "clarity" }
                    ListElement { name: "Texture"; key: "texture" }
                    ListElement { name: "Sharpness"; key: "sharpness" }
                }
                Repeater { model: detailModel; delegate: editSlider }
            }

            ColumnLayout { // Right Column
                Layout.fillWidth: true
                Layout.preferredWidth: (parent.width - 20) / 2
                Layout.alignment: Qt.AlignTop
                spacing: 2

                // --- Color Group ---
                Label { text: "Color"; font.bold: true; color: imageEditorDialog.textColor }
                ListModel {
                    id: colorModel
                    ListElement { name: "Saturation"; key: "saturation"; reverse: false }
                    ListElement { name: "Vibrance"; key: "vibrance"; reverse: false }
                    ListElement { name: "White Balance (Blue/Yellow)"; key: "white_balance_by"; reverse: false }
                    ListElement { name: "White Balance (Green/Magenta)"; key: "white_balance_mg"; reverse: false }
                }
                Repeater { model: colorModel; delegate: editSlider }
                
                Button {
                    text: "Auto White Balance"
                    Layout.fillWidth: true
                    Layout.topMargin: 5
                    onClicked: {
                        controller.auto_white_balance()
                        imageEditorDialog.updatePulse++
                    }
                }
                
                Button {
                    text: "Auto Levels"
                    Layout.fillWidth: true
                    Layout.topMargin: 5
                    onClicked: {
                        controller.auto_levels()
                        imageEditorDialog.updatePulse++
                    }
                }

                // --- Effects Group ---
                Label { text: "Effects"; font.bold: true; color: imageEditorDialog.textColor; Layout.topMargin: 10 }
                ListModel {
                    id: effectsModel
                    ListElement { name: "Vignette"; key: "vignette"; min: 0; max: 100 }
                }
                Repeater { model: effectsModel; delegate: editSlider }

                // --- Transform Group ---
                Label { text: "Transform"; font.bold: true; color: imageEditorDialog.textColor; Layout.topMargin: 10 }
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "Rotation"; color: imageEditorDialog.textColor }
                    Button { text: "↶"; onClicked: controller.rotate_image_ccw() }
                    Button { text: "↷"; onClicked: controller.rotate_image_cw() }
                }

                // --- Action Buttons ---
                Item { Layout.fillHeight: true; Layout.minimumHeight: 20 }
                Button { 
                    text: "Reset All Edits"
                    Layout.fillWidth: true
                    onClicked: {
                        controller.reset_edit_parameters()
                        imageEditorDialog.updatePulse++
                    }
                }
                Button { 
                    text: "Save and Close (S)"
                    Layout.fillWidth: true
                    onClicked: {
                        controller.save_edited_image()
                        uiState.isEditorOpen = false
                    }
                }
                Button { 
                    text: "Close Without Saving (E)"
                    Layout.fillWidth: true
                    onClicked: { 
                        uiState.isEditorOpen = false
                    }
                }
            }
        }
    }

    Component {
        id: editSlider
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            
            property bool isReversed: model.reverse !== undefined ? model.reverse : false
            
            // Label
            Text {
                text: model.name
                color: imageEditorDialog.textColor
                font.pixelSize: 14
                Layout.preferredWidth: 80
                Layout.alignment: Qt.AlignVCenter
            }
            
            // Slider
            Slider {
                id: slider
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
                from: model.min === undefined ? -100 : model.min
                to: model.max === undefined ? 100 : model.max
                stepSize: 1
                
                property real backendValue: {
                    var val = imageEditorDialog.getBackendValue(model.key) * (model.max === undefined ? 100 : model.max)
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
                    controller.set_edit_parameter(model.key, sendValue / (model.max === undefined ? 100.0 : model.max))
                }
                
                TapHandler {
                    acceptedButtons: Qt.LeftButton
                    onDoubleTapped: {
                         controller.set_edit_parameter(model.key, 0.0)
                         slider.value = 0.0
                         imageEditorDialog.updatePulse++
                    }
                }
                
                onPressedChanged: {
                    if (pressed) imageEditorDialog.slidersPressedCount++; else imageEditorDialog.slidersPressedCount--;
                }
                
                onBackendValueChanged: {
                    if (!pressed) {
                        value = backendValue
                    }
                }
                
                background: Rectangle {
                    x: slider.leftPadding
                    y: slider.topPadding + slider.availableHeight / 2 - height / 2
                    width: slider.availableWidth
                    height: 4
                    radius: 2
                    color: imageEditorDialog.backgroundColor === "#2b2b2b" ? Qt.lighter(imageEditorDialog.backgroundColor, 1.2) : Qt.darker(imageEditorDialog.backgroundColor, 1.2)
                }

                handle: Rectangle {
                     x: slider.leftPadding + slider.visualPosition * (slider.availableWidth - width)
                     y: slider.topPadding + slider.availableHeight / 2 - height / 2
                     width: 16
                     height: 16
                     radius: 8
                     color: slider.pressed ? "#4fb360" : "#6fcf7c"
                     border.color: (uiState && uiState.theme === 0) ? Qt.darker(Material.accent, 1.2) : Qt.lighter(Material.accent, 1.2)
                }
            }

            // SpinBox
            // SpinBox
            SpinBox {
                id: valueInput
                from: model.min === undefined ? -100 : model.min
                to: model.max === undefined ? 100 : model.max
                stepSize: 1
                editable: true
                Layout.preferredWidth: 80 // Compact width
                Layout.alignment: Qt.AlignVCenter
                
                value: isReversed ? -slider.value : slider.value
                
                onValueModified: {
                     var val = value
                     var sendValue = isReversed ? -val : val
                     controller.set_edit_parameter(model.key, sendValue / (model.max === undefined ? 100.0 : model.max))
                     imageEditorDialog.updatePulse++ 
                }

                // Customizations for compact look with small arrows
                contentItem: TextInput {
                    z: 2
                    text: valueInput.textFromValue(valueInput.value, valueInput.locale)
                    font: valueInput.font
                    color: imageEditorDialog.textColor
                    selectionColor: "#21be2b"
                    selectedTextColor: "#ffffff"
                    horizontalAlignment: Qt.AlignHCenter
                    verticalAlignment: Qt.AlignVCenter
                    readOnly: !valueInput.editable
                    validator: valueInput.validator
                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                }

                up.indicator: Rectangle {
                    x: valueInput.mirrored ? 0 : parent.width - width
                    height: parent.height
                    implicitWidth: 20 // Small width for buttons
                    implicitHeight: 20
                    color: "transparent"
                    
                    Text {
                        text: "+"
                        font.pixelSize: 14
                        anchors.centerIn: parent
                        color: valueInput.up.pressed ? "#4fb360" : imageEditorDialog.textColor
                    }
                    

                }

                down.indicator: Rectangle {
                    x: valueInput.mirrored ? parent.width - width : 0
                    height: parent.height
                    implicitWidth: 20 // Small width for buttons
                    implicitHeight: 20
                    color: "transparent"
                    
                    Text {
                        text: "-"
                        font.pixelSize: 14
                        anchors.centerIn: parent
                        color: valueInput.down.pressed ? "#4fb360" : imageEditorDialog.textColor
                    }

                }

                background: Rectangle {
                    implicitWidth: 80
                    color: "transparent"
                    border.color: "#555555"
                    border.width: 1
                    radius: 2
                }
            }
        }
    }
}
