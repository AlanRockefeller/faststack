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
    height: 600

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
    }

    property string heliconPath: ""
    property double cacheSize: 1.5
    property int prefetchRadius: 4
    property int theme: 0
    property string defaultDirectory: ""
    property string photoshopPath: ""
    property string optimizeFor: "speed"

    property string awbMode: "lab"
    property double awbStrength: 0.7
    property int awbWarmBias: 6

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
        
        uiState.awbMode = awbMode
        uiState.awbStrength = awbStrength
        uiState.awbWarmBias = awbWarmBias
        
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
                        visible: uiState.check_path_exists(heliconPathField.text)
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
                        visible: uiState.check_path_exists(photoshopPathField.text)
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
            }

            GridLayout {
                columns: 3

                // --- Auto White Balance ---
                Label { 
                    text: "Auto WB Mode:"
                    Layout.topMargin: 10
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

                Label { text: "Auto WB Strength:" }
                Slider {
                    id: awbStrengthSlider
                    from: 0.3
                    to: 1.0
                    value: settingsDialog.awbStrength
                    onValueChanged: settingsDialog.awbStrength = value
                }
                Label { text: (awbStrengthSlider.value * 100).toFixed(0) + "%" }

                Label { text: "Auto WB Warm Bias:" }
                SpinBox {
                    id: awbWarmBiasSpinBox
                    from: -10
                    to: 20
                    value: settingsDialog.awbWarmBias
                    onValueChanged: settingsDialog.awbWarmBias = value
                }
                Label {} // Placeholder

                // --- Advanced AWB Settings ---
                CheckBox {
                    id: advancedAwbCheckBox
                    text: "Advanced Settings"
                    checked: false
                    Layout.columnSpan: 3
                }

                GridLayout {
                    visible: advancedAwbCheckBox.checked
                    columns: 3
                    Layout.columnSpan: 3
                    Layout.fillWidth: true
                    
                    Label { text: "Luma Lower Bound:" }
                    SpinBox {
                        from: 0
                        to: 255
                        value: settingsDialog.awbLumaLowerBound
                        onValueChanged: settingsDialog.awbLumaLowerBound = value
                    }
                    Label {}

                    Label { text: "Luma Upper Bound:" }
                    SpinBox {
                        from: 0
                        to: 255
                        value: settingsDialog.awbLumaUpperBound
                        onValueChanged: settingsDialog.awbLumaUpperBound = value
                    }
                    Label {}

                    Label { text: "RGB Lower Bound:" }
                    SpinBox {
                        from: 0
                        to: 255
                        value: settingsDialog.awbRgbLowerBound
                        onValueChanged: settingsDialog.awbRgbLowerBound = value
                    }
                    Label {}

                    Label { text: "RGB Upper Bound:" }
                    SpinBox {
                        from: 0
                        to: 255
                        value: settingsDialog.awbRgbUpperBound
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
        onTriggered: settingsDialog.cacheUsage = uiState.get_cache_usage_gb()
    }
}
