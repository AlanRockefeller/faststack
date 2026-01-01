import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15

Window {
    id: editDialog
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
    onVisibleChanged: {
        if (!visible) {
            uiState.isEditorOpen = false
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

    // Keyboard Shortcuts
    Item {
        anchors.fill: parent
        focus: true
        Keys.onPressed: (event) => {
            if (event.key === Qt.Key_E || event.key === Qt.Key_Escape) {
                uiState.isEditorOpen = false
                event.accepted = true
            }
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
                    ListElement { name: "Shadows"; key: "shadows" }
                    ListElement { name: "Whites"; key: "whites"; reverse: true }
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
                        editDialog.updatePulse++
                    }
                }
                
                Button {
                    text: "Auto Levels"
                    Layout.fillWidth: true
                    Layout.topMargin: 5
                    onClicked: {
                        controller.auto_levels()
                        editDialog.updatePulse++
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
                        editDialog.updatePulse++
                    }
                }
                Button { 
                    text: "Save and Close Editor (Ctrl+S)"
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
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            
            property bool isReversed: model.reverse !== undefined ? model.reverse : false
            property real displayValue: isReversed ? -slider.value : slider.value
            
            RowLayout {
                Layout.fillWidth: true
                spacing: 5

                Text {
                    text: model.name + ":"
                    color: imageEditorDialog.textColor
                    font.pixelSize: 14
                }
                
                Item { Layout.fillWidth: true } // Spacer

                TextInput {
                    id: valueInput
                    text: displayValue.toFixed(0)
                    color: imageEditorDialog.textColor
                    font.pixelSize: 14
                    selectByMouse: true
                    validator: IntValidator { bottom: model.min === undefined ? -100 : model.min; top: model.max === undefined ? 100 : model.max }
                    
                    onEditingFinished: {
                        var val = parseInt(text)
                        if (isNaN(val)) return
                        
                        // Clamp value
                        var min = model.min === undefined ? -100 : model.min
                        var max = model.max === undefined ? 100 : model.max
                        if (val < min) val = min
                        if (val > max) val = max
                        
                        var sendValue = isReversed ? -val : val
                        controller.set_edit_parameter(model.key, sendValue / (model.max === undefined ? 100.0 : model.max))
                        editDialog.updatePulse++ // Force slider update
                    }
                }
            }
            Slider {
                id: slider
                Layout.fillWidth: true
                Layout.minimumHeight: 30
                from: model.min === undefined ? -100 : model.min
                to: model.max === undefined ? 100 : model.max
                stepSize: 1
                
                property real backendValue: {
                    var val = editDialog.getBackendValue(model.key) * (model.max === undefined ? 100 : model.max)
                    return isReversed ? -val : val
                }
                
                value: backendValue

		Connections {
			target: editDialog
			function onUpdatePulseChanged() {
			    if (!slider.pressed) {
				// This forces the visual handle to snap to the backendValue
				// even if backendValue hasn't numerically changed (e.g. 0 -> 0)
				slider.value = slider.backendValue
			    }
			}
		    }
                
                onMoved: {
                    var sendValue = isReversed ? -value : value
                    controller.set_edit_parameter(model.key, sendValue / (model.max === undefined ? 100.0 : model.max))
                }

                // Double click/tap to reset
                TapHandler {
                    acceptedButtons: Qt.LeftButton
                    onDoubleTapped: {
                         controller.set_edit_parameter(model.key, 0.0)
                         slider.value = 0.0
                         editDialog.updatePulse++
                    }
                }

                onPressedChanged: {
                    if (pressed) editDialog.slidersPressedCount++; else editDialog.slidersPressedCount--;
                }

		onBackendValueChanged: {
			// Check '!pressed' to avoid fighting the user if they are 
			// currently dragging the slider while an update comes in.
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
        }
    }
}
