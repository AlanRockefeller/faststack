import QtQuick
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: settingsDialog
    title: "Settings"
    standardButtons: Dialog.Ok | Dialog.Cancel
    modal: true
    closePolicy: Popup.CloseOnEscape
    focus: true
    width: 600
    height: 770

    // Live cache usage value (updated by timer)
    property real cacheUsage: 0.0

    onVisibleChanged: {
        cacheUsageTimer.running = visible
        if (visible) {
            controller.dialog_opened()
        } else {
            controller.dialog_closed()
        }
    }
    
    onOpened: {
        // Refresh text fields when dialog opens with current values
        cacheSizeField.text = settingsDialog.cacheSize.toFixed(1)
        heliconPathField.text = settingsDialog.heliconPath
        photoshopPathField.text = settingsDialog.photoshopPath
        optimizeForComboBox.currentIndex = optimizeForComboBox.model.indexOf(settingsDialog.optimizeFor)
        autoLevelThresholdField.text = settingsDialog.autoLevelClippingThreshold.toFixed(4)
        settingsDialog.autoLevelStrength = uiState.autoLevelStrength
        settingsDialog.autoLevelStrengthAuto = uiState.autoLevelStrengthAuto
    }

    property string heliconPath: ""
    property double cacheSize: 1.5
    property double autoLevelClippingThreshold: 0.1
    property double autoLevelStrength: 1.0
    property bool autoLevelStrengthAuto: false
    property int prefetchRadius: 4
    property int theme: 0
    property string defaultDirectory: ""
    property string photoshopPath: ""
    property string optimizeFor: "speed"

    property string awbMode: "lab"
    property double awbStrength: 0.7
    property int awbWarmBias: 6
    property int awbTintBias: 0

    property int awbLumaLowerBound: 30
    property int awbLumaUpperBound: 220
    property int awbRgbLowerBound: 5
    property int awbRgbUpperBound: 250

    onAccepted: {
        uiState.set_helicon_path(heliconPath)
        uiState.set_photoshop_path(photoshopPath)
        uiState.set_cache_size(cacheSize)
        uiState.set_prefetch_radius(prefetchRadius)
        uiState.set_theme(theme)
        uiState.set_default_directory(defaultDirectory)
        uiState.set_optimize_for(optimizeFor)
        uiState.autoLevelClippingThreshold = autoLevelClippingThreshold
        uiState.autoLevelStrength = autoLevelStrength
        uiState.autoLevelStrengthAuto = autoLevelStrengthAuto
        
        uiState.awbMode = awbMode
        uiState.awbStrength = awbStrength
        uiState.awbWarmBias = awbWarmBias
        uiState.awbTintBias = awbTintBias
        
        uiState.awbLumaLowerBound = awbLumaLowerBound
        uiState.awbLumaUpperBound = awbLumaUpperBound
        uiState.awbRgbLowerBound = awbRgbLowerBound
        uiState.awbRgbUpperBound = awbRgbUpperBound
    }

    contentItem: ColumnLayout {
        Row {
            id: tabButtons
            spacing: 5
            
            Button {
                text: "General"
                highlighted: settingsStackLayout.currentIndex === 0
                onClicked: settingsStackLayout.currentIndex = 0
            }
            Button {
                text: "Auto White Balance"
                highlighted: settingsStackLayout.currentIndex === 1
                onClicked: settingsStackLayout.currentIndex = 1
            }
        }

        StackLayout {
            id: settingsStackLayout
            currentIndex: 0

            GridLayout {
                columns: 3
                
                // Helicon Path
                Label { text: "Helicon Focus Path:" }
                TextField {
                    id: heliconPathField
                    Layout.fillWidth: true
                    text: settingsDialog.heliconPath
                    onTextChanged: settingsDialog.heliconPath = text
                }
                RowLayout {
                    Button {
                        text: "Browse..."
                        onClicked: {
                            var path = uiState.open_file_dialog()
                            if (path) heliconPathField.text = path
                        }
                    }
                    Label {
                        id: checkMarkLabel
                        text: "✔"
                        color: "lightgreen"
                        visible: uiState && uiState.check_path_exists(heliconPathField.text)
                    }
                }

                // Photoshop Path
                Label { text: "Photoshop Path:" }
                TextField {
                    id: photoshopPathField
                    Layout.fillWidth: true
                    text: settingsDialog.photoshopPath
                    onTextChanged: settingsDialog.photoshopPath = text
                }
                RowLayout {
                    Button {
                        text: "Browse..."
                        onClicked: {
                            var path = uiState.open_file_dialog()
                            if (path) photoshopPathField.text = path
                        }
                    }
                    Label {
                        id: photoshopCheckMarkLabel
                        text: "✔"
                        color: "lightgreen"
                        visible: uiState && uiState.check_path_exists(photoshopPathField.text)
                    }
                }

                // Cache Size
                Label { text: "Cache Size (GB):" }
                TextField {
                    id: cacheSizeField
                    Layout.fillWidth: true
                    
                    Component.onCompleted: {
                        text = settingsDialog.cacheSize.toFixed(1)
                    }
                    
                    onEditingFinished: {
                        var value = parseFloat(text)
                        if (!isNaN(value) && value >= 0.5 && value <= 16) {
                            settingsDialog.cacheSize = value
                            text = value.toFixed(1)  // Format it
                        } else {
                            // Invalid input, reset to current value
                            text = settingsDialog.cacheSize.toFixed(1)
                        }
                    }
                }
                Label {
                    id: cacheUsageLabel
                    text: "In use: " + settingsDialog.cacheUsage.toFixed(2) + " GB"
                    color: "#1013e6"
                }

                // Prefetch Radius
                Label { text: "Prefetch Radius:" }
                SpinBox {
                    id: prefetchRadiusSpinBox
                    from: 1
                    to: 20
                    value: settingsDialog.prefetchRadius
                    onValueChanged: settingsDialog.prefetchRadius = value
                }
                Label {} // Placeholder

                // Theme
                Label { text: "Theme:" }
                ComboBox {
                    id: themeComboBox
                    model: ["Dark", "Light"]
                    currentIndex: settingsDialog.theme
                    onCurrentIndexChanged: settingsDialog.theme = currentIndex
                }
                Label {} // Placeholder

                // Default Directory
                Label { text: "Default Image Directory:" }
                TextField {
                    id: defaultDirectoryField
                    Layout.fillWidth: true
                    text: settingsDialog.defaultDirectory
                    onTextChanged: settingsDialog.defaultDirectory = text
                }
                Button {
                    text: "Browse..."
                    onClicked: {
                        var path = uiState.open_directory_dialog()
                        if (path) defaultDirectoryField.text = path
                    }
                }

                // Optimize For
                Label { text: "Optimize For:" }
                ComboBox {
                    id: optimizeForComboBox
                    model: ["speed", "quality"]
                    currentIndex: model.indexOf(settingsDialog.optimizeFor)
                    onCurrentIndexChanged: settingsDialog.optimizeFor = model[currentIndex]
                    Layout.fillWidth: true
                }
                Label {} // Placeholder

                // Auto Levels Clip Threshold
                Label { text: "Auto Levels Clip %:" }
                TextField {
                    id: autoLevelThresholdField
                    Layout.fillWidth: true
                    
                    onEditingFinished: {
                        var value = parseFloat(text)
                        if (!isNaN(value) && value >= 0.0 && value <= 10.0) {
                            settingsDialog.autoLevelClippingThreshold = value
                            text = value.toFixed(4)
                        } else {
                            text = settingsDialog.autoLevelClippingThreshold.toFixed(4)
                        }
                    }
                }
                Label {} // Placeholder

                // Auto Levels Strength
                Label { text: "Auto Levels Strength:" }
                RowLayout {
                    Layout.fillWidth: true
                    Slider {
                        id: autoLevelStrengthSlider
                        from: 0.0
                        to: 1.0
                        stepSize: 0.05
                        value: settingsDialog.autoLevelStrength
                        onValueChanged: settingsDialog.autoLevelStrength = value
                        enabled: !autoLevelStrengthAutoCheckBox.checked
                        Layout.fillWidth: true
                        opacity: enabled ? 1.0 : 0.5
                    }
                    CheckBox {
                        id: autoLevelStrengthAutoCheckBox
                        text: "Auto"
                        checked: settingsDialog.autoLevelStrengthAuto
                        onCheckedChanged: settingsDialog.autoLevelStrengthAuto = checked
                    }
                }
                Label { text: Math.round(settingsDialog.autoLevelStrength * 100) + "%" }
            }

            GridLayout {
                columns: 3

                // --- Auto White Balance ---
                MouseArea {
                    width: awbModeLabel.implicitWidth
                    height: awbModeLabel.implicitHeight
                    hoverEnabled: true
                    Layout.topMargin: 10
                    Label { 
                        id: awbModeLabel
                        text: "Auto WB Mode:"
                    }
                    ToolTip.visible: containsMouse
                    ToolTip.text: "Choose the algorithm for Auto White Balance.\n'lab': Uses Lab color space (recommended).\n'rgb': Uses simple Grey World assumption."
                }
                ComboBox {
                    id: awbModeComboBox
                    model: ["lab", "rgb"]
                    currentIndex: Math.max(0, model.indexOf(settingsDialog.awbMode))
                    onCurrentIndexChanged: settingsDialog.awbMode = model[currentIndex]                    
                    Layout.topMargin: 10
                }
                Label {
                     Layout.topMargin: 10
                }

                MouseArea {
                    width: awbStrengthLabel.implicitWidth
                    height: awbStrengthLabel.implicitHeight
                    hoverEnabled: true
                    Label { 
                        id: awbStrengthLabel
                        text: "Auto WB Strength:"
                    }
                    ToolTip.visible: containsMouse
                    ToolTip.text: "How strongly to apply the calculated white balance correction (0.0 - 1.0)."
                }
                Slider {
                    id: awbStrengthSlider
                    from: 0.3
                    to: 1.0
                    value: settingsDialog.awbStrength
                    onValueChanged: settingsDialog.awbStrength = value
                }
                Label { text: (awbStrengthSlider.value * 100).toFixed(0) + "%" }

                MouseArea {
                    width: awbWarmBiasLabel.implicitWidth
                    height: awbWarmBiasLabel.implicitHeight
                    hoverEnabled: true
                    Label { 
                        id: awbWarmBiasLabel
                        text: "Auto WB Warm Bias:"
                    }
                    ToolTip.visible: containsMouse
                    ToolTip.text: "Adjusts the target Yellow/Blue balance.\nPositive values make the result warmer (more yellow).\nNegative values make it cooler (more blue)."
                }
                SpinBox {
                    id: awbWarmBiasSpinBox
                    from: -50
                    to: 50
                    value: settingsDialog.awbWarmBias
                    editable: true
                    onValueChanged: settingsDialog.awbWarmBias = value
                }
                Label {} // Placeholder

                MouseArea {
                    width: awbTintBiasLabel.implicitWidth
                    height: awbTintBiasLabel.implicitHeight
                    hoverEnabled: true
                    Label { 
                        id: awbTintBiasLabel
                        text: "Auto WB Tint Bias:"
                    }
                    ToolTip.visible: containsMouse
                    ToolTip.text: "Adjusts the target Magenta/Green balance.\nPositive values add magenta tint.\nNegative values add green tint."
                }
                SpinBox {
                    id: awbTintBiasSpinBox
                    from: -50
                    to: 50
                    value: settingsDialog.awbTintBias
                    editable: true
                    onValueChanged: settingsDialog.awbTintBias = value
                }
                Label {} // Placeholder

                // --- Advanced AWB Settings ---
                CheckBox {
                    id: advancedAwbCheckBox
                    text: "Advanced Settings"
                    checked: false
                    Layout.columnSpan: 3
                    hoverEnabled: true
                    ToolTip.visible: hovered
                    ToolTip.text: "Configure thresholds for pixel selection in AWB calculation."
                }

                GridLayout {
                    visible: advancedAwbCheckBox.checked
                    columns: 3
                    Layout.columnSpan: 3
                    Layout.fillWidth: true
                    
                    MouseArea {
                        width: lumaLowerLabel.implicitWidth
                        height: lumaLowerLabel.implicitHeight
                        hoverEnabled: true
                        Label { 
                            id: lumaLowerLabel
                            text: "Luma Lower Bound:" 
                        }
                        ToolTip.visible: containsMouse
                        ToolTip.text: "Ignore pixels darker than this brightness (0-255) when calculating AWB."
                    }
                    SpinBox {
                        from: 0
                        to: 255
                        value: settingsDialog.awbLumaLowerBound
                        editable: true
                        onValueChanged: settingsDialog.awbLumaLowerBound = value
                    }
                    Label {}

                    MouseArea {
                        width: lumaUpperLabel.implicitWidth
                        height: lumaUpperLabel.implicitHeight
                        hoverEnabled: true
                        Label { 
                            id: lumaUpperLabel
                            text: "Luma Upper Bound:" 
                        }
                        ToolTip.visible: containsMouse
                        ToolTip.text: "Ignore pixels brighter than this brightness (0-255) when calculating AWB."
                    }
                    SpinBox {
                        from: 0
                        to: 255
                        value: settingsDialog.awbLumaUpperBound
                        editable: true
                        onValueChanged: settingsDialog.awbLumaUpperBound = value
                    }
                    Label {}

                    MouseArea {
                        width: rgbLowerLabel.implicitWidth
                        height: rgbLowerLabel.implicitHeight
                        hoverEnabled: true
                        Label { 
                            id: rgbLowerLabel
                            text: "RGB Lower Bound:" 
                        }
                        ToolTip.visible: containsMouse
                        ToolTip.text: "Ignore pixels where any channel is below this value (0-255)."
                    }
                    SpinBox {
                        from: 0
                        to: 255
                        value: settingsDialog.awbRgbLowerBound
                        editable: true
                        onValueChanged: settingsDialog.awbRgbLowerBound = value
                    }
                    Label {}

                    MouseArea {
                        width: rgbUpperLabel.implicitWidth
                        height: rgbUpperLabel.implicitHeight
                        hoverEnabled: true
                        Label { 
                            id: rgbUpperLabel
                            text: "RGB Upper Bound:" 
                        }
                        ToolTip.visible: containsMouse
                        ToolTip.text: "Ignore pixels where any channel is above this value (0-255)."
                    }
                    SpinBox {
                        from: 0
                        to: 255
                        value: settingsDialog.awbRgbUpperBound
                        editable: true
                        onValueChanged: settingsDialog.awbRgbUpperBound = value
                    }
                    Label {}
                }
            }
        }
    }

    // Poll cache usage periodically while the dialog is open
    Timer {
        id: cacheUsageTimer
        interval: 1000
        repeat: true
        running: false
        onTriggered: {
            if (uiState) settingsDialog.cacheUsage = uiState.get_cache_usage_gb()
        }
    }
}
