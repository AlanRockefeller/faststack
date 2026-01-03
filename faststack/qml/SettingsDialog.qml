import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15

Window {
    id: settingsDialog
    title: "Settings"
    width: 700
    height: 800
    visible: false
    flags: Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
    modality: Qt.ApplicationModal
    
    // Make window close button (X) behave like Cancel
    onClosing: function(close) {
        close.accepted = false
        visible = false
    }
    
    // Properties matching the original dialog
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

    // Live cache usage value (updated by timer)
    property real cacheUsage: 0.0

    // Modern Color Palette (copied from ImageEditorDialog)
    property color backgroundColor: "#1e1e1e"
    property color textColor: "white"
    readonly property color accentColor: "#6366f1" // Modern Indigo
    readonly property color accentColorHover: "#818cf8"
    readonly property color accentColorSubtle: "#306366f1"
    readonly property color controlBg: "#10ffffff"
    readonly property color controlBorder: "#30ffffff"
    readonly property color separatorColor: "#20ffffff"

    Material.theme: Material.Dark
    Material.accent: accentColor
    color: backgroundColor

    // Helper to open the dialog
    function open() {
        // Reload all properties from uiState to ensure Cancel discards edits
        if (uiState) {
            heliconPath = uiState.get_helicon_path()
            photoshopPath = uiState.get_photoshop_path()
            cacheSize = uiState.get_cache_size()
            prefetchRadius = uiState.get_prefetch_radius()
            theme = uiState.theme
            defaultDirectory = uiState.get_default_directory()
            optimizeFor = uiState.get_optimize_for()
            autoLevelClippingThreshold = uiState.autoLevelClippingThreshold
            autoLevelStrength = uiState.autoLevelStrength
            autoLevelStrengthAuto = uiState.autoLevelStrengthAuto
            awbMode = uiState.awbMode
            awbStrength = uiState.awbStrength
            awbWarmBias = uiState.awbWarmBias
            awbTintBias = uiState.awbTintBias
            awbLumaLowerBound = uiState.awbLumaLowerBound
            awbLumaUpperBound = uiState.awbLumaUpperBound
            awbRgbLowerBound = uiState.awbRgbLowerBound
            awbRgbUpperBound = uiState.awbRgbUpperBound
        }
        visible = true
        raise()
        requestActivate()
    }

    Shortcut {
        sequence: "Escape"
        context: Qt.WindowShortcut
        onActivated: visible = false
    }

    onVisibleChanged: {
        cacheUsageTimer.running = visible
        if (visible) {
            controller.dialog_opened()
            // Reset all text fields from properties
            if (heliconField.item) heliconField.item.text = settingsDialog.heliconPath
            if (photoshopField.item) photoshopField.item.text = settingsDialog.photoshopPath
            if (defaultDirField.item) defaultDirField.item.text = settingsDialog.defaultDirectory
            if (cacheSizeField.item) cacheSizeField.item.text = settingsDialog.cacheSize.toFixed(1)
            // Note: ComboBoxes and SpinBoxes update automatically via bindings/connections
        } else {
            controller.dialog_closed()
        }
    }

    function saveSettings() {
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

        visible = false
    }

    // Component for Section Separator
    Component {
        id: sectionSeparator
        Rectangle {
            Layout.fillWidth: true
            Layout.topMargin: 20
            Layout.bottomMargin: 5
            height: 1
            color: settingsDialog.separatorColor
        }
    }

    // Component for Section Header
    Component {
        id: sectionHeader
        Label {
            font.bold: true
            font.pixelSize: 15
            font.letterSpacing: 1.0
            color: settingsDialog.accentColorHover
            Layout.topMargin: 5
            Layout.bottomMargin: 10
        }
    }

    // Custom Styled TextField
    Component {
        id: styledTextField
        TextField {
            id: control
            color: settingsDialog.textColor
            placeholderTextColor: "#80ffffff"
            selectionColor: settingsDialog.accentColor
            selectedTextColor: "#ffffff"
            font.pixelSize: 13
            background: Rectangle {
                color: control.enabled ? "transparent" : "#05ffffff"
                border.color: control.activeFocus ? settingsDialog.accentColor : settingsDialog.controlBorder
                border.width: 1
                radius: 4
            }
        }
    }

    // Styled Slider Component
    Component {
        id: styledSlider
        Slider {
            id: control
            
            background: Item {
                x: control.leftPadding
                y: control.topPadding + control.availableHeight / 2 - height / 2
                width: control.availableWidth
                height: 6
                
                Rectangle {
                    anchors.fill: parent
                    radius: 3
                    color: settingsDialog.controlBg
                    border.color: settingsDialog.controlBorder
                    border.width: 1
                }

                Rectangle {
                    width: control.visualPosition * parent.width
                    height: parent.height
                    radius: 3
                    color: settingsDialog.accentColor
                    opacity: 0.8
                }
            }

            handle: Rectangle {
                 x: control.leftPadding + control.visualPosition * (control.availableWidth - width)
                 y: control.topPadding + control.availableHeight / 2 - height / 2
                 width: 16
                 height: 16
                 radius: 8
                 color: control.pressed ? settingsDialog.accentColor : "white"
                 border.color: control.pressed ? "white" : settingsDialog.accentColor
                 border.width: 2
            }
        }
    }

    // Styled SpinBox Component
    Component {
        id: styledSpinBox
        SpinBox {
            id: control
            editable: true
            
            contentItem: TextInput {
                z: 2
                text: control.textFromValue(control.value, control.locale)
                font.pixelSize: 13
                color: settingsDialog.textColor
                selectionColor: settingsDialog.accentColor
                selectedTextColor: "#ffffff"
                horizontalAlignment: Qt.AlignHCenter
                verticalAlignment: Qt.AlignVCenter
                readOnly: !control.editable
                validator: control.validator
                inputMethodHints: Qt.ImhFormattedNumbersOnly
                
                // Update control.value when user finishes typing
                onEditingFinished: {
                    control.value = control.valueFromText(text, control.locale)
                }
            }

            up.indicator: Item {
                x: parent.width - width
                height: parent.height
                width: 20
                Rectangle {
                    anchors.centerIn: parent
                    width: 16; height: 16
                    radius: 2
                    color: control.up.pressed ? settingsDialog.accentColor : "transparent"
                    Text {
                        text: "+"
                        anchors.centerIn: parent
                        color: settingsDialog.textColor
                    }
                }
            }

            down.indicator: Item {
                x: 0
                height: parent.height
                width: 20
                Rectangle {
                    anchors.centerIn: parent
                    width: 16; height: 16
                    radius: 2
                    color: control.down.pressed ? settingsDialog.accentColor : "transparent"
                    Text {
                        text: "-"
                        anchors.centerIn: parent
                        color: settingsDialog.textColor
                    }
                }
            }

            background: Rectangle {
                implicitWidth: 100
                color: "transparent"
                border.color: settingsDialog.controlBorder
                border.width: 1
                radius: 4
            }
        }
    }

    // State
    property int currentTab: 0

    // Component for Tab Button
    Component {
        id: tabButton
        Rectangle {
            property string text
            property int index
            
            anchors.fill: parent
            color: "transparent"
            
            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: 2
                color: settingsDialog.currentTab === index ? settingsDialog.accentColor : "transparent"
                Behavior on color { ColorAnimation { duration: 200 } }
            }
            
            Text {
                anchors.centerIn: parent
                text: parent.text
                color: settingsDialog.currentTab === index ? settingsDialog.accentColor : "#80ffffff"
                font.bold: settingsDialog.currentTab === index
                font.pixelSize: 14
            }
            
            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: settingsDialog.currentTab = index
            }
        }
    }

    // Main Layout container
    ColumnLayout {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: bottomBar.top
        spacing: 0

        // --- Custom Tab Bar ---
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 50
            color: "#1e1e1e"
            z: 10
            
            RowLayout {
                anchors.fill: parent
                anchors.margins: 20
                anchors.bottomMargin: 0
                spacing: 20
                
                Loader { 
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    sourceComponent: tabButton
                    onLoaded: { item.text = "General"; item.index = 0 }
                }
                Loader { 
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    sourceComponent: tabButton
                    onLoaded: { item.text = "Auto Adjustments"; item.index = 1 }
                }
            }
            
            // Bottom border for tab bar
            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: 1
                color: "#20ffffff"
            }
        }

        // --- Content Stack ---
        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: settingsDialog.currentTab
            
            // --- TAB 1: GENERAL ---
            Item {
                ScrollView {
                    anchors.fill: parent
                    anchors.margins: 20
                    clip: true
                    contentWidth: availableWidth

                    ColumnLayout {
                        width: parent.width
                        spacing: 15

                        Loader { 
                            sourceComponent: sectionHeader
                            onLoaded: item.text = "General Settings"
                        }

                        // Helicon Path
                        Label { text: "Helicon Focus Path"; color: "#aaaaaa"; font.pixelSize: 12 }
                        RowLayout {
                            Layout.fillWidth: true
                            Loader {
                                id: heliconField
                                sourceComponent: styledTextField
                                Layout.fillWidth: true
                                onLoaded: {
                                    // Text is set once in onVisibleChanged
                                    item.text = settingsDialog.heliconPath
                                    item.textEdited.connect(function() { settingsDialog.heliconPath = item.text })
                                }
                            }
                            Button {
                                text: "Browse"
                                flat: true
                                onClicked: {
                                    var path = uiState.open_file_dialog()
                                    if (path) {
                                        settingsDialog.heliconPath = path
                                        if (heliconField.item) heliconField.item.text = path
                                    }
                                }
                                background: Rectangle { color: parent.pressed ? "#20ffffff" : "#10ffffff"; radius: 4 }
                                contentItem: Text { text: parent.text; color: settingsDialog.textColor; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            }
                            Label {
                                text: "✔"
                                color: "#4ade80"
                                visible: uiState && uiState.check_path_exists(settingsDialog.heliconPath)
                            }
                        }

                        // Photoshop Path
                        Label { text: "Photoshop Path"; color: "#aaaaaa"; font.pixelSize: 12; Layout.topMargin: 5 }
                        RowLayout {
                            Layout.fillWidth: true
                            Loader {
                                id: photoshopField
                                sourceComponent: styledTextField
                                Layout.fillWidth: true
                                onLoaded: {
                                    // Text is set once in onVisibleChanged
                                    item.text = settingsDialog.photoshopPath
                                    item.textEdited.connect(function() { settingsDialog.photoshopPath = item.text })
                                }
                            }
                            Button {
                                text: "Browse"
                                flat: true
                                onClicked: {
                                    var path = uiState.open_file_dialog()
                                    if (path) {
                                        settingsDialog.photoshopPath = path
                                        if (photoshopField.item) photoshopField.item.text = path
                                    }
                                }
                                background: Rectangle { color: parent.pressed ? "#20ffffff" : "#10ffffff"; radius: 4 }
                                contentItem: Text { text: parent.text; color: settingsDialog.textColor; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            }
                            Label {
                                text: "✔"
                                color: "#4ade80"
                                visible: uiState && uiState.check_path_exists(settingsDialog.photoshopPath)
                            }
                        }

                        // Default Directory
                        Label { text: "Default Image Directory"; color: "#aaaaaa"; font.pixelSize: 12; Layout.topMargin: 5 }
                        RowLayout {
                            Layout.fillWidth: true
                            Loader {
                                id: defaultDirField
                                sourceComponent: styledTextField
                                Layout.fillWidth: true
                                onLoaded: {
                                    // Text is set once in onVisibleChanged
                                    item.text = settingsDialog.defaultDirectory
                                    item.textEdited.connect(function() { settingsDialog.defaultDirectory = item.text })
                                }
                            }
                            Button {
                                text: "Browse"
                                flat: true
                                onClicked: {
                                    var path = uiState.open_directory_dialog()
                                    if (path) {
                                        settingsDialog.defaultDirectory = path
                                        if (defaultDirField.item) defaultDirField.item.text = path
                                    }
                                }
                                background: Rectangle { color: parent.pressed ? "#20ffffff" : "#10ffffff"; radius: 4 }
                                contentItem: Text { text: parent.text; color: settingsDialog.textColor; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            }
                        }

                        Loader { sourceComponent: sectionSeparator }

                        // Grid for Cache/Theme/Etc
                        GridLayout {
                            columns: 2
                            columnSpacing: 20
                            rowSpacing: 15
                            Layout.fillWidth: true
                            Layout.topMargin: 5

                            // Cache
                            Label { 
                                text: "Cache Size (GB)"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: cacheSizeHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: cacheSizeHover.containsMouse
                                ToolTip.text: "Decoded images are cached in RAM for faster browsing. Higher values allow more images to be kept in memory, reducing re-decode times. Lower values use less RAM. Recommended: 2-8 GB depending on available memory."
                            }
                            RowLayout {
                                Loader {
                                    id: cacheSizeField
                                    sourceComponent: styledTextField
                                    Layout.preferredWidth: 80
                                    onLoaded: {
                                        // Text is set once in onVisibleChanged
                                        item.text = settingsDialog.cacheSize.toFixed(1)
                                        item.editingFinished.connect(function() {
                                            var value = parseFloat(item.text)
                                            if (!isNaN(value) && value >= 0.5 && value <= 16) {
                                                settingsDialog.cacheSize = value
                                                // Reformat to show consistent precision
                                                item.text = settingsDialog.cacheSize.toFixed(1)
                                            } else {
                                                // Reset to valid value if invalid input
                                                item.text = settingsDialog.cacheSize.toFixed(1)
                                            }
                                        })
                                    }
                                }
                                Label { 
                                    text: "In use: " + settingsDialog.cacheUsage.toFixed(2) + " GB"
                                    color: settingsDialog.accentColorHover
                                    font.pixelSize: 11
                                }
                            }

                            // Prefetch
                            Label { 
                                text: "Prefetch Radius"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: prefetchHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: prefetchHover.containsMouse
                                ToolTip.text: "Number of images around the current image to pre-load in the background. Higher values make browsing smoother but use more CPU/RAM. Lower values reduce resource usage. Recommended: 4-8 for smooth navigation."
                            }
                            Loader {
                                sourceComponent: styledSpinBox
                                onLoaded: {
                                    item.from = 1; item.to = 20
                                    item.value = settingsDialog.prefetchRadius
                                    item.valueChanged.connect(function() { settingsDialog.prefetchRadius = item.value })
                                }
                            }

                            // Optimize For
                            Label { 
                                text: "Optimize For"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: optimizeHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: optimizeHover.containsMouse
                                ToolTip.text: "Speed: Faster JPEG decoding using hardware acceleration (may have slight quality loss). Quality: Slower but pixel-perfect decoding. Choose Speed for general browsing, Quality for critical image inspection."
                            }
                            ComboBox {
                                model: ["speed", "quality"]
                                currentIndex: Math.max(0, model.indexOf(settingsDialog.optimizeFor))
                                onActivated: settingsDialog.optimizeFor = model[currentIndex]
                                Layout.preferredWidth: 150
                                delegate: ItemDelegate {
                                    width: parent.width
                                    contentItem: Text { text: modelData; color: settingsDialog.textColor; font: parent.font; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
                                    background: Rectangle { color: parent.highlighted ? "#20ffffff" : "transparent" }
                                }
                                contentItem: Text { text: parent.displayText; color: settingsDialog.textColor; verticalAlignment: Text.AlignVCenter; leftPadding: 10 }
                                background: Rectangle { color: "#10ffffff"; border.color: settingsDialog.controlBorder; radius: 4 }
                            }

                            // Theme
                            Label { text: "Theme"; color: settingsDialog.textColor }
                            ComboBox {
                                model: ["Dark", "Light"]
                                currentIndex: settingsDialog.theme
                                onActivated: settingsDialog.theme = currentIndex
                                Layout.preferredWidth: 150
                                delegate: ItemDelegate {
                                    width: parent.width
                                    contentItem: Text { text: modelData; color: settingsDialog.textColor; verticalAlignment: Text.AlignVCenter }
                                    background: Rectangle { color: parent.highlighted ? "#20ffffff" : "transparent" }
                                }
                                contentItem: Text { text: parent.displayText; color: settingsDialog.textColor; verticalAlignment: Text.AlignVCenter; leftPadding: 10 }
                                background: Rectangle { color: "#10ffffff"; border.color: settingsDialog.controlBorder; radius: 4 }
                            }
                        }
                        
                        Item { Layout.fillHeight: true } // Spacer
                    }
                }
            }
            
            // --- TAB 2: AUTO ADJUSTMENTS ---
            Item {
                ScrollView {
                    anchors.fill: parent
                    anchors.margins: 20
                    clip: true
                    contentWidth: availableWidth

                    ColumnLayout {
                        width: parent.width
                        spacing: 15

                        // --- Auto Levels ---
                        Loader { 
                            sourceComponent: sectionHeader
                            onLoaded: item.text = "Auto Levels"
                        }

                        GridLayout {
                            columns: 2
                            columnSpacing: 20
                            rowSpacing: 10
                            Layout.fillWidth: true

                            Label { 
                                text: "Clip Threshold %"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: clipThresholdHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: clipThresholdHover.containsMouse
                                ToolTip.text: "Percentage of pixels to clip at the dark and light ends of the histogram when auto-levels is applied. Higher values (e.g., 5%) increase contrast but risk hard clipping. Lower values (e.g., 0.1%) preserve more dynamic range. Default: 0.1%"
                            }
                            Loader {
                                sourceComponent: styledTextField
                                Layout.preferredWidth: 80
                                onLoaded: {
                                     item.text = settingsDialog.autoLevelClippingThreshold.toFixed(4)
                                     item.editingFinished.connect(function() {
                                         var value = parseFloat(item.text)
                                         if (!isNaN(value) && value >= 0.0 && value <= 10.0) settingsDialog.autoLevelClippingThreshold = value
                                         item.text = settingsDialog.autoLevelClippingThreshold.toFixed(4)
                                     })
                                }
                            }

                            Label { 
                                text: "Strength"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: autoLevelStrengthHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: autoLevelStrengthHover.containsMouse
                                ToolTip.text: "How much of the auto-levels correction to apply. 1.0 applies the full mathematical correction, lower values blend with the original for a subtler effect. The 'Auto' checkbox enables automatic strength reduction to avoid excessive clipping."
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Loader {
                                    sourceComponent: styledSlider
                                    Layout.fillWidth: true
                                    onLoaded: {
                                        item.from = 0.0; item.to = 1.0; item.stepSize = 0.05
                                        item.value = settingsDialog.autoLevelStrength
                                        item.valueChanged.connect(function() { settingsDialog.autoLevelStrength = item.value })
                                        item.enabled = Qt.binding(function() { return !autoLvlAuto.checked })
                                        item.opacity = Qt.binding(function() { return (!autoLvlAuto.checked) ? 1.0 : 0.5 })
                                    }
                                }
                                CheckBox {
                                    id: autoLvlAuto
                                    text: "Auto"
                                    checked: settingsDialog.autoLevelStrengthAuto
                                    onCheckedChanged: settingsDialog.autoLevelStrengthAuto = checked
                                    contentItem: Text { text: parent.text; color: settingsDialog.textColor; leftPadding: parent.indicator.width + parent.spacing; verticalAlignment: Text.AlignVCenter }
                                    indicator: Rectangle {
                                        implicitWidth: 18; implicitHeight: 18
                                        x: parent.leftPadding; y: parent.height / 2 - height / 2
                                        radius: 3
                                        border.color: settingsDialog.accentColor
                                        color: parent.checked ? settingsDialog.accentColor : "transparent"
                                        Text { text: "✓"; color: "white"; anchors.centerIn: parent; visible: parent.parent.checked; font.bold: true }
                                    }
                                }
                            }
                        }

                        Loader { sourceComponent: sectionSeparator }

                        // --- Auto White Balance ---
                        Loader { 
                            sourceComponent: sectionHeader
                            onLoaded: item.text = "Auto White Balance"
                        }

                        GridLayout {
                            columns: 2
                            columnSpacing: 20
                            rowSpacing: 15
                            Layout.fillWidth: true
                            
                            // AWB Mode
                            Label { 
                                text: "Algorithm"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: awbAlgorithmHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: awbAlgorithmHover.containsMouse
                                ToolTip.text: "Algorithm for auto white balance. 'lab' analyzes in LAB color space for perceptually uniform results. 'rgb' works directly in RGB space. Most users should use 'lab'."
                            }
                            ComboBox {
                                model: ["lab", "rgb"]
                                currentIndex: Math.max(0, model.indexOf(settingsDialog.awbMode))
                                onActivated: settingsDialog.awbMode = model[currentIndex]
                                Layout.preferredWidth: 150
                                delegate: ItemDelegate {
                                    width: parent.width
                                    contentItem: Text { text: modelData; color: settingsDialog.textColor; verticalAlignment: Text.AlignVCenter }
                                    background: Rectangle { color: parent.highlighted ? "#20ffffff" : "transparent" }
                                }
                                contentItem: Text { text: parent.displayText; color: settingsDialog.textColor; verticalAlignment: Text.AlignVCenter; leftPadding: 10 }
                                background: Rectangle { color: "#10ffffff"; border.color: settingsDialog.controlBorder; radius: 4 }
                            }

                            // Strength
                            Label { 
                                text: "Strength (" + (awbStrSlider.item ? Math.round(awbStrSlider.item.value * 100) : 0) + "%)"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: awbStrengthHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: awbStrengthHover.containsMouse
                                ToolTip.text: "How aggressively to apply the auto white balance correction. 100% applies full correction, lower values blend with original. Range: 30-100%. Recommended: 70%"
                            }
                            Loader {
                                id: awbStrSlider
                                sourceComponent: styledSlider
                                Layout.fillWidth: true
                                onLoaded: {
                                    item.from = 0.3; item.to = 1.0
                                    item.value = settingsDialog.awbStrength
                                    item.valueChanged.connect(function() { settingsDialog.awbStrength = item.value })
                                }
                            }

                            // Warm Bias
                            Label { 
                                text: "Warm Bias (Yel/Blu)"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: warmBiasHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: warmBiasHover.containsMouse
                                ToolTip.text: "Shifts the white balance warmer (yellow, positive values) or cooler (blue, negative values) after auto correction. Useful to compensate for systematic color casts. Range: -50 to +50. Default: +6"
                            }
                            Loader {
                                sourceComponent: styledSpinBox
                                onLoaded: {
                                    item.from = -50; item.to = 50
                                    item.value = settingsDialog.awbWarmBias
                                    item.valueChanged.connect(function() { settingsDialog.awbWarmBias = item.value })
                                }
                            }

                            // Tint Bias
                            Label { 
                                text: "Tint Bias (Mag/Grn)"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: tintBiasHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: tintBiasHover.containsMouse
                                ToolTip.text: "Shifts the color tint toward magenta (positive values) or green (negative values) after auto correction. Compensates for tint issues in the white balance. Range: -50 to +50. Default: 0"
                            }
                            Loader {
                                sourceComponent: styledSpinBox
                                onLoaded: {
                                    item.from = -50; item.to = 50
                                    item.value = settingsDialog.awbTintBias
                                    item.valueChanged.connect(function() { settingsDialog.awbTintBias = item.value })
                                }
                            }
                        }
                        
                        Loader { sourceComponent: sectionSeparator }
                        
                        Loader { 
                            sourceComponent: sectionHeader
                            onLoaded: item.text = "Advanced Thresholds"
                        }

                        GridLayout {
                            columns: 2
                            columnSpacing: 20
                            rowSpacing: 10
                            Layout.fillWidth: true

                            Label { 
                                text: "Luma Lower"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: lumaLowerHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: lumaLowerHover.containsMouse
                                ToolTip.text: "Minimum luminance (brightness) threshold for pixels to be included in AWB gray-point calculation. Pixels darker than this are excluded. Range: 0-255. Default: 30. Increase to ignore very dark areas."
                            }
                            Loader {
                                sourceComponent: styledSpinBox
                                onLoaded: { item.from=0; item.to=255; item.value=settingsDialog.awbLumaLowerBound; item.valueChanged.connect(function(){ settingsDialog.awbLumaLowerBound=item.value})}
                            }

                            Label { 
                                text: "Luma Upper"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: lumaUpperHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: lumaUpperHover.containsMouse
                                ToolTip.text: "Maximum luminance (brightness) threshold for pixels to be included in AWB gray-point calculation. Pixels brighter than this are excluded. Range: 0-255. Default: 220. Decrease to ignore very bright areas."
                            }
                            Loader {
                                sourceComponent: styledSpinBox
                                onLoaded: { item.from=0; item.to=255; item.value=settingsDialog.awbLumaUpperBound; item.valueChanged.connect(function(){ settingsDialog.awbLumaUpperBound=item.value})}
                            }

                            Label { 
                                text: "RGB Lower"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: rgbLowerHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: rgbLowerHover.containsMouse
                                ToolTip.text: "Minimum RGB channel value for pixels to be included in AWB calculation. Pixels with any channel below this are excluded. Range: 0-255. Default: 5. Increase to ignore very saturated colors."
                            }
                            Loader {
                                sourceComponent: styledSpinBox
                                onLoaded: { item.from=0; item.to=255; item.value=settingsDialog.awbRgbLowerBound; item.valueChanged.connect(function(){ settingsDialog.awbRgbLowerBound=item.value})}
                            }

                            Label { 
                                text: "RGB Upper"
                                color: settingsDialog.textColor
                                
                                MouseArea {
                                    id: rgbUpperHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                }
                                
                                ToolTip.visible: rgbUpperHover.containsMouse
                                ToolTip.text: "Maximum RGB channel value for pixels to be included in AWB calculation. Pixels with any channel above this are excluded. Range: 0-255. Default: 250. Decrease to ignore near-white areas."
                            }
                            Loader {
                                sourceComponent: styledSpinBox
                                onLoaded: { item.from=0; item.to=255; item.value=settingsDialog.awbRgbUpperBound; item.valueChanged.connect(function(){ settingsDialog.awbRgbUpperBound=item.value})}
                            }
                        }
                        
                        Item { Layout.fillHeight: true } // Spacer
                    }
                }
            }
        }
    }

    // Bottom Action Bar
    Rectangle {
        id: bottomBar
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: 70
        color: "#1e1e1e" // matches background
        // Gradient separator
        Rectangle { width: parent.width; height: 1; color: "#20ffffff"; anchors.top: parent.top }

        RowLayout {
            anchors.fill: parent
            anchors.margins: 20
            spacing: 15

            Item { Layout.fillWidth: true } // Spacer left

            Button {
                text: "Cancel"
                Layout.preferredWidth: 100
                onClicked: settingsDialog.visible = false
                
                contentItem: Text {
                    text: parent.text
                    font: parent.font
                    color: settingsDialog.textColor
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    color: parent.pressed ? "#40ffffff" : "#20ffffff"
                    radius: 4
                    border.color: parent.hovered ? "#60ffffff" : "transparent"
                }
            }

            Button {
                text: "Save"
                Layout.preferredWidth: 100
                highlighted: true
                onClicked: settingsDialog.saveSettings()
                
                contentItem: Text {
                    text: parent.text
                    font: parent.font
                    color: "white"
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    color: parent.pressed ? Qt.darker(settingsDialog.accentColor, 1.1) : settingsDialog.accentColor
                    radius: 4
                }
            }
        }
    }

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
