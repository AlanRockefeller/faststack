pragma ComponentBehavior: Bound
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
    property bool autoVibranceEnabled: true
    property int prefetchRadius: 4
    property int theme: 0
    property string defaultDirectory: ""
    property string photoshopPath: ""
    property string rawtherapeePath: ""
    property string optimizeFor: "speed"

    property string awbMode: "lab"
    property double awbStrength: 0.7
    property int awbWarmBias: 6
    property int awbTintBias: 0

    property int awbLumaLowerBound: 30
    property int awbLumaUpperBound: 220
    property int awbRgbLowerBound: 5
    property int awbRgbUpperBound: 250
    property var uiStateRef: null
    property var controllerRef: null

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
    readonly property string autoVibranceTooltip: "When enabled, FastStack may add a small vibrance boost only when you press capital L (Shift+L / Quick Auto Adjust). Lowercase l still runs auto levels only and leaves vibrance unchanged."

    Material.theme: Material.Dark
    Material.accent: accentColor
    color: backgroundColor

    Component.onCompleted: {
        settingsDialog.uiStateRef = uiState
        settingsDialog.controllerRef = controller
    }

    function loaderItem(loader) {
        return loader ? loader.item : null
    }

    function loaderProperty(loader, propertyName, fallbackValue) {
        var control = settingsDialog.loaderItem(loader)
        var value = control ? control[propertyName] : undefined
        return value === undefined ? fallbackValue : value
    }

    function setLoaderProperty(loader, propertyName, value) {
        var control = settingsDialog.loaderItem(loader)
        if (control) {
            control[propertyName] = value
        }
    }

    function setLoaderBinding(loader, propertyName, bindingFn) {
        var control = settingsDialog.loaderItem(loader)
        if (control) {
            control[propertyName] = Qt.binding(bindingFn)
        }
    }

    function connectLoaderSignal(loader, signalName, callback) {
        var control = settingsDialog.loaderItem(loader)
        var signal = control ? control[signalName] : null
        if (signal && typeof signal.connect === "function") {
            signal.connect(callback)
        }
    }

    function openFileDialog() {
        return settingsDialog.uiStateRef ? settingsDialog.uiStateRef.open_file_dialog() : ""
    }

    function openDirectoryDialog() {
        return settingsDialog.uiStateRef ? settingsDialog.uiStateRef.open_directory_dialog() : ""
    }

    function pathExists(path) {
        return settingsDialog.uiStateRef ? settingsDialog.uiStateRef.check_path_exists(path) : false
    }

    function refreshTextFields() {
        settingsDialog.setLoaderProperty(heliconField, "text", settingsDialog.heliconPath)
        settingsDialog.setLoaderProperty(photoshopField, "text", settingsDialog.photoshopPath)
        settingsDialog.setLoaderProperty(rawtherapeeField, "text", settingsDialog.rawtherapeePath)
        settingsDialog.setLoaderProperty(defaultDirField, "text", settingsDialog.defaultDirectory)
        settingsDialog.setLoaderProperty(cacheSizeField, "text", settingsDialog.cacheSize.toFixed(1))
        settingsDialog.setLoaderProperty(prefetchRadiusLoader, "value", settingsDialog.prefetchRadius)
    }

    onPrefetchRadiusChanged: settingsDialog.setLoaderProperty(prefetchRadiusLoader, "value", settingsDialog.prefetchRadius)

    // Helper to open the dialog
    function open() {
        // Reload all properties from uiState to ensure Cancel discards edits
        if (settingsDialog.uiStateRef) {
            settingsDialog.heliconPath = settingsDialog.uiStateRef.get_helicon_path()
            settingsDialog.photoshopPath = settingsDialog.uiStateRef.get_photoshop_path()
            settingsDialog.rawtherapeePath = settingsDialog.uiStateRef.get_rawtherapee_path()
            settingsDialog.cacheSize = settingsDialog.uiStateRef.get_cache_size()
            settingsDialog.prefetchRadius = settingsDialog.uiStateRef.get_prefetch_radius()
            settingsDialog.theme = settingsDialog.uiStateRef.theme
            settingsDialog.defaultDirectory = settingsDialog.uiStateRef.get_default_directory()
            settingsDialog.optimizeFor = settingsDialog.uiStateRef.get_optimize_for()
            settingsDialog.autoLevelClippingThreshold = settingsDialog.uiStateRef.autoLevelClippingThreshold
            settingsDialog.autoLevelStrength = settingsDialog.uiStateRef.autoLevelStrength
            settingsDialog.autoLevelStrengthAuto = settingsDialog.uiStateRef.autoLevelStrengthAuto
            settingsDialog.autoVibranceEnabled = settingsDialog.uiStateRef.autoVibranceEnabled
            settingsDialog.awbMode = settingsDialog.uiStateRef.awbMode
            settingsDialog.awbStrength = settingsDialog.uiStateRef.awbStrength
            settingsDialog.awbWarmBias = settingsDialog.uiStateRef.awbWarmBias
            settingsDialog.awbTintBias = settingsDialog.uiStateRef.awbTintBias
            settingsDialog.awbLumaLowerBound = settingsDialog.uiStateRef.awbLumaLowerBound
            settingsDialog.awbLumaUpperBound = settingsDialog.uiStateRef.awbLumaUpperBound
            settingsDialog.awbRgbLowerBound = settingsDialog.uiStateRef.awbRgbLowerBound
            settingsDialog.awbRgbUpperBound = settingsDialog.uiStateRef.awbRgbUpperBound
        }
        settingsDialog.visible = true
        settingsDialog.raise()
        settingsDialog.requestActivate()
    }

    Shortcut {
        sequence: "Escape"
        context: Qt.WindowShortcut
        onActivated: settingsDialog.visible = false
    }

    onVisibleChanged: {
        cacheUsageTimer.running = settingsDialog.visible
        if (settingsDialog.visible) {
            if (settingsDialog.controllerRef) {
                settingsDialog.controllerRef.dialog_opened()
            }
            settingsDialog.refreshTextFields()
        } else {
            if (settingsDialog.controllerRef) {
                settingsDialog.controllerRef.dialog_closed()
            }
        }
    }

    function saveSettings() {
        var state = settingsDialog.uiStateRef
        if (!state) {
            settingsDialog.visible = false
            return
        }

        state.set_helicon_path(settingsDialog.heliconPath)
        state.set_photoshop_path(settingsDialog.photoshopPath)
        state.set_rawtherapee_path(settingsDialog.rawtherapeePath)
        state.set_cache_size(settingsDialog.cacheSize)
        state.set_prefetch_radius(settingsDialog.prefetchRadius)
        state.set_theme(settingsDialog.theme)
        state.set_default_directory(settingsDialog.defaultDirectory)
        state.set_optimize_for(settingsDialog.optimizeFor)
        state.autoLevelClippingThreshold = settingsDialog.autoLevelClippingThreshold
        state.autoLevelStrength = settingsDialog.autoLevelStrength
        state.autoLevelStrengthAuto = settingsDialog.autoLevelStrengthAuto
        state.autoVibranceEnabled = settingsDialog.autoVibranceEnabled

        state.awbMode = settingsDialog.awbMode
        state.awbStrength = settingsDialog.awbStrength
        state.awbWarmBias = settingsDialog.awbWarmBias
        state.awbTintBias = settingsDialog.awbTintBias

        state.awbLumaLowerBound = settingsDialog.awbLumaLowerBound
        state.awbLumaUpperBound = settingsDialog.awbLumaUpperBound
        state.awbRgbLowerBound = settingsDialog.awbRgbLowerBound
        state.awbRgbUpperBound = settingsDialog.awbRgbUpperBound

        settingsDialog.visible = false
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
                text: control.displayText
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
                    var parsedValue = parseInt(text, 10)
                    if (!isNaN(parsedValue)) {
                        control.value = parsedValue
                    }
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
            id: tabButtonRoot
            property string text
            property int index
            
            anchors.fill: parent
            color: "transparent"
            
            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: 2
                color: settingsDialog.currentTab === tabButtonRoot.index ? settingsDialog.accentColor : "transparent"
                Behavior on color { ColorAnimation { duration: 200 } }
            }
            
            Text {
                anchors.centerIn: parent
                text: tabButtonRoot.text
                color: settingsDialog.currentTab === tabButtonRoot.index ? settingsDialog.accentColor : "#80ffffff"
                font.bold: settingsDialog.currentTab === tabButtonRoot.index
                font.pixelSize: 14
            }
            
            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: settingsDialog.currentTab = tabButtonRoot.index
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
                                    settingsDialog.setLoaderProperty(heliconField, "text", settingsDialog.heliconPath)
                                    settingsDialog.connectLoaderSignal(heliconField, "textEdited", function() {
                                        settingsDialog.heliconPath = settingsDialog.loaderProperty(heliconField, "text", settingsDialog.heliconPath)
                                    })
                                }
                            }
                            Button {
                                id: heliconBrowseButton
                                text: "Browse"
                                flat: true
                                onClicked: {
                                    var path = settingsDialog.openFileDialog()
                                    if (path) {
                                        settingsDialog.heliconPath = path
                                        settingsDialog.setLoaderProperty(heliconField, "text", path)
                                    }
                                }
                                background: Rectangle { color: heliconBrowseButton.pressed ? "#20ffffff" : "#10ffffff"; radius: 4 }
                                contentItem: Text { text: heliconBrowseButton.text; color: settingsDialog.textColor; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            }
                            Label {
                                text: "✔"
                                color: "#4ade80"
                                visible: settingsDialog.pathExists(settingsDialog.heliconPath)
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
                                    settingsDialog.setLoaderProperty(photoshopField, "text", settingsDialog.photoshopPath)
                                    settingsDialog.connectLoaderSignal(photoshopField, "textEdited", function() {
                                        settingsDialog.photoshopPath = settingsDialog.loaderProperty(photoshopField, "text", settingsDialog.photoshopPath)
                                    })
                                }
                            }
                            Button {
                                id: photoshopBrowseButton
                                text: "Browse"
                                flat: true
                                onClicked: {
                                    var path = settingsDialog.openFileDialog()
                                    if (path) {
                                        settingsDialog.photoshopPath = path
                                        settingsDialog.setLoaderProperty(photoshopField, "text", path)
                                    }
                                }
                                background: Rectangle { color: photoshopBrowseButton.pressed ? "#20ffffff" : "#10ffffff"; radius: 4 }
                                contentItem: Text { text: photoshopBrowseButton.text; color: settingsDialog.textColor; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            }
                            Label {
                                text: "✔"
                                color: "#4ade80"
                                visible: settingsDialog.pathExists(settingsDialog.photoshopPath)
                            }
                        }

                        // RawTherapee Path
                        Label { text: "RawTherapee Path"; color: "#aaaaaa"; font.pixelSize: 12; Layout.topMargin: 5 }
                        RowLayout {
                            Layout.fillWidth: true
                            Loader {
                                id: rawtherapeeField
                                sourceComponent: styledTextField
                                Layout.fillWidth: true
                                onLoaded: {
                                    settingsDialog.setLoaderProperty(rawtherapeeField, "text", settingsDialog.rawtherapeePath)
                                    settingsDialog.connectLoaderSignal(rawtherapeeField, "textEdited", function() {
                                        settingsDialog.rawtherapeePath = settingsDialog.loaderProperty(rawtherapeeField, "text", settingsDialog.rawtherapeePath)
                                    })
                                }
                            }
                            Button {
                                id: rawtherapeeBrowseButton
                                text: "Browse"
                                flat: true
                                onClicked: {
                                    var path = settingsDialog.openFileDialog()
                                    if (path) {
                                        settingsDialog.rawtherapeePath = path
                                        settingsDialog.setLoaderProperty(rawtherapeeField, "text", path)
                                    }
                                }
                                background: Rectangle { color: rawtherapeeBrowseButton.pressed ? "#20ffffff" : "#10ffffff"; radius: 4 }
                                contentItem: Text { text: rawtherapeeBrowseButton.text; color: settingsDialog.textColor; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            }
                            Label {
                                text: "✔"
                                color: "#4ade80"
                                visible: settingsDialog.pathExists(settingsDialog.rawtherapeePath)
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
                                    settingsDialog.setLoaderProperty(defaultDirField, "text", settingsDialog.defaultDirectory)
                                    settingsDialog.connectLoaderSignal(defaultDirField, "textEdited", function() {
                                        settingsDialog.defaultDirectory = settingsDialog.loaderProperty(defaultDirField, "text", settingsDialog.defaultDirectory)
                                    })
                                }
                            }
                            Button {
                                id: defaultDirBrowseButton
                                text: "Browse"
                                flat: true
                                onClicked: {
                                    var path = settingsDialog.openDirectoryDialog()
                                    if (path) {
                                        settingsDialog.defaultDirectory = path
                                        settingsDialog.setLoaderProperty(defaultDirField, "text", path)
                                    }
                                }
                                background: Rectangle { color: defaultDirBrowseButton.pressed ? "#20ffffff" : "#10ffffff"; radius: 4 }
                                contentItem: Text { text: defaultDirBrowseButton.text; color: settingsDialog.textColor; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
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
                                        settingsDialog.setLoaderProperty(cacheSizeField, "text", settingsDialog.cacheSize.toFixed(1))
                                        settingsDialog.connectLoaderSignal(cacheSizeField, "editingFinished", function() {
                                            var value = parseFloat(settingsDialog.loaderProperty(cacheSizeField, "text", settingsDialog.cacheSize.toFixed(1)))
                                            if (!isNaN(value) && value >= 0.5 && value <= 16) {
                                                settingsDialog.cacheSize = value
                                                settingsDialog.setLoaderProperty(cacheSizeField, "text", settingsDialog.cacheSize.toFixed(1))
                                            } else {
                                                settingsDialog.setLoaderProperty(cacheSizeField, "text", settingsDialog.cacheSize.toFixed(1))
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
                                id: prefetchRadiusLoader
                                sourceComponent: styledSpinBox
                                onLoaded: {
                                    settingsDialog.setLoaderProperty(prefetchRadiusLoader, "from", 1)
                                    settingsDialog.setLoaderProperty(prefetchRadiusLoader, "to", 20)
                                    settingsDialog.setLoaderProperty(prefetchRadiusLoader, "value", settingsDialog.prefetchRadius)
                                    settingsDialog.connectLoaderSignal(prefetchRadiusLoader, "valueChanged", function() {
                                        settingsDialog.prefetchRadius = settingsDialog.loaderProperty(prefetchRadiusLoader, "value", settingsDialog.prefetchRadius)
                                    })
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
                                id: optimizeCombo
                                model: ["speed", "quality"]
                                currentIndex: Math.max(0, model.indexOf(settingsDialog.optimizeFor))
                                onActivated: settingsDialog.optimizeFor = model[currentIndex]
                                Layout.preferredWidth: 150
                                delegate: ItemDelegate {
                                    id: optimizeOption
                                    required property string modelData
                                    width: optimizeCombo.width
                                    contentItem: Text { text: optimizeOption.modelData; color: settingsDialog.textColor; font: optimizeOption.font; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
                                    background: Rectangle { color: optimizeOption.highlighted ? "#20ffffff" : "transparent" }
                                }
                                contentItem: Text { text: optimizeCombo.displayText; color: settingsDialog.textColor; verticalAlignment: Text.AlignVCenter; leftPadding: 10 }
                                background: Rectangle { color: "#10ffffff"; border.color: settingsDialog.controlBorder; radius: 4 }
                            }

                            // Theme
                            Label { text: "Theme"; color: settingsDialog.textColor }
                            ComboBox {
                                id: themeCombo
                                model: ["Dark", "Light"]
                                currentIndex: settingsDialog.theme
                                onActivated: settingsDialog.theme = currentIndex
                                Layout.preferredWidth: 150
                                delegate: ItemDelegate {
                                    id: themeOption
                                    required property string modelData
                                    width: themeCombo.width
                                    contentItem: Text { text: themeOption.modelData; color: settingsDialog.textColor; verticalAlignment: Text.AlignVCenter }
                                    background: Rectangle { color: themeOption.highlighted ? "#20ffffff" : "transparent" }
                                }
                                contentItem: Text { text: themeCombo.displayText; color: settingsDialog.textColor; verticalAlignment: Text.AlignVCenter; leftPadding: 10 }
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
                                ToolTip.text: "Percentage of pixels to clip at the dark and light ends of the histogram when auto-levels is applied. Higher values (e.g., 5%) increase contrast but risk making highlights appear clipped. Lower values (e.g., 0.1%) preserve more dynamic range. Default: 0.1%"
                            }
                            Loader {
                                id: clipThresholdLoader
                                sourceComponent: styledTextField
                                Layout.preferredWidth: 80
                                onLoaded: {
                                     settingsDialog.setLoaderProperty(clipThresholdLoader, "text", settingsDialog.autoLevelClippingThreshold.toFixed(4))
                                     settingsDialog.connectLoaderSignal(clipThresholdLoader, "editingFinished", function() {
                                         var value = parseFloat(settingsDialog.loaderProperty(clipThresholdLoader, "text", settingsDialog.autoLevelClippingThreshold.toFixed(4)))
                                         if (!isNaN(value) && value >= 0.0 && value <= 10.0) {
                                             settingsDialog.autoLevelClippingThreshold = value
                                         }
                                         settingsDialog.setLoaderProperty(clipThresholdLoader, "text", settingsDialog.autoLevelClippingThreshold.toFixed(4))
                                     })
                                }
                                Binding {
                                    target: clipThresholdLoader.item
                                    property: "text"
                                    value: settingsDialog.autoLevelClippingThreshold.toFixed(4)
                                    when: clipThresholdLoader.item && !settingsDialog.loaderProperty(clipThresholdLoader, "activeFocus", false)
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
                                    id: autoLevelStrengthLoader
                                    sourceComponent: styledSlider
                                    Layout.fillWidth: true
                                    onLoaded: {
                                        settingsDialog.setLoaderProperty(autoLevelStrengthLoader, "from", 0.0)
                                        settingsDialog.setLoaderProperty(autoLevelStrengthLoader, "to", 1.0)
                                        settingsDialog.setLoaderProperty(autoLevelStrengthLoader, "stepSize", 0.05)
                                        settingsDialog.setLoaderProperty(autoLevelStrengthLoader, "value", settingsDialog.autoLevelStrength)
                                        settingsDialog.connectLoaderSignal(autoLevelStrengthLoader, "valueChanged", function() {
                                            settingsDialog.autoLevelStrength = settingsDialog.loaderProperty(autoLevelStrengthLoader, "value", settingsDialog.autoLevelStrength)
                                        })
                                        settingsDialog.setLoaderBinding(autoLevelStrengthLoader, "enabled", function() { return !autoLvlAuto.checked })
                                        settingsDialog.setLoaderBinding(autoLevelStrengthLoader, "opacity", function() { return autoLvlAuto.checked ? 0.5 : 1.0 })
                                    }
                                    Binding {
                                        target: autoLevelStrengthLoader.item
                                        property: "value"
                                        value: settingsDialog.autoLevelStrength
                                        when: autoLevelStrengthLoader.item && !settingsDialog.loaderProperty(autoLevelStrengthLoader, "pressed", false)
                                    }
                                }
                                CheckBox {
                                    id: autoLvlAuto
                                    text: "Auto"
                                    checked: settingsDialog.autoLevelStrengthAuto
                                    onCheckedChanged: settingsDialog.autoLevelStrengthAuto = checked
                                    contentItem: Text { text: autoLvlAuto.text; color: settingsDialog.textColor; leftPadding: autoLvlAuto.indicator.width + autoLvlAuto.spacing; verticalAlignment: Text.AlignVCenter }
                                    indicator: Rectangle {
                                        implicitWidth: 18; implicitHeight: 18
                                        x: autoLvlAuto.leftPadding; y: parent.height / 2 - height / 2
                                        radius: 3
                                        border.color: settingsDialog.accentColor
                                        color: autoLvlAuto.checked ? settingsDialog.accentColor : "transparent"
                                        Text { text: "✓"; color: "white"; anchors.centerIn: parent; visible: autoLvlAuto.checked; font.bold: true }
                                    }
                                }
                            }

                            Label {
                                text: "Auto Vibrance"
                                color: settingsDialog.textColor
                            }
                            CheckBox {
                                id: autoVibranceCheck
                                text: "Enabled"
                                checked: settingsDialog.autoVibranceEnabled
                                hoverEnabled: true
                                onCheckedChanged: settingsDialog.autoVibranceEnabled = checked
                                ToolTip.visible: hovered
                                ToolTip.delay: 500
                                ToolTip.text: settingsDialog.autoVibranceTooltip
                                contentItem: Text { text: autoVibranceCheck.text; color: settingsDialog.textColor; leftPadding: autoVibranceCheck.indicator.width + autoVibranceCheck.spacing; verticalAlignment: Text.AlignVCenter }
                                indicator: Rectangle {
                                    implicitWidth: 18; implicitHeight: 18
                                    x: autoVibranceCheck.leftPadding; y: parent.height / 2 - height / 2
                                    radius: 3
                                    border.color: settingsDialog.accentColor
                                    color: autoVibranceCheck.checked ? settingsDialog.accentColor : "transparent"
                                    Text { text: "✓"; color: "white"; anchors.centerIn: parent; visible: autoVibranceCheck.checked; font.bold: true }
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
                                id: awbModeCombo
                                model: ["lab", "rgb"]
                                currentIndex: Math.max(0, model.indexOf(settingsDialog.awbMode))
                                onActivated: settingsDialog.awbMode = model[currentIndex]
                                Layout.preferredWidth: 150
                                delegate: ItemDelegate {
                                    id: awbModeOption
                                    required property string modelData
                                    width: awbModeCombo.width
                                    contentItem: Text { text: awbModeOption.modelData; color: settingsDialog.textColor; verticalAlignment: Text.AlignVCenter }
                                    background: Rectangle { color: awbModeOption.highlighted ? "#20ffffff" : "transparent" }
                                }
                                contentItem: Text { text: awbModeCombo.displayText; color: settingsDialog.textColor; verticalAlignment: Text.AlignVCenter; leftPadding: 10 }
                                background: Rectangle { color: "#10ffffff"; border.color: settingsDialog.controlBorder; radius: 4 }
                            }

                            // Strength
                            Label { 
                                text: "Strength (" + Math.round(settingsDialog.loaderProperty(awbStrSlider, "value", 0) * 100) + "%)"
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
                                    settingsDialog.setLoaderProperty(awbStrSlider, "from", 0.3)
                                    settingsDialog.setLoaderProperty(awbStrSlider, "to", 1.0)
                                    settingsDialog.setLoaderProperty(awbStrSlider, "value", settingsDialog.awbStrength)
                                    settingsDialog.connectLoaderSignal(awbStrSlider, "valueChanged", function() {
                                        settingsDialog.awbStrength = settingsDialog.loaderProperty(awbStrSlider, "value", settingsDialog.awbStrength)
                                    })
                                }
                                Binding {
                                    target: awbStrSlider.item
                                    property: "value"
                                    value: settingsDialog.awbStrength
                                    when: awbStrSlider.item && !settingsDialog.loaderProperty(awbStrSlider, "pressed", false)
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
                                id: awbWarmBiasLoader
                                sourceComponent: styledSpinBox
                                onLoaded: {
                                    settingsDialog.setLoaderProperty(awbWarmBiasLoader, "from", -50)
                                    settingsDialog.setLoaderProperty(awbWarmBiasLoader, "to", 50)
                                    settingsDialog.setLoaderProperty(awbWarmBiasLoader, "value", settingsDialog.awbWarmBias)
                                    settingsDialog.connectLoaderSignal(awbWarmBiasLoader, "valueChanged", function() {
                                        settingsDialog.awbWarmBias = settingsDialog.loaderProperty(awbWarmBiasLoader, "value", settingsDialog.awbWarmBias)
                                    })
                                }
                                Binding {
                                    target: awbWarmBiasLoader.item
                                    property: "value"
                                    value: settingsDialog.awbWarmBias
                                    when: awbWarmBiasLoader.item && !settingsDialog.loaderProperty(awbWarmBiasLoader, "activeFocus", false)
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
                                id: awbTintBiasLoader
                                sourceComponent: styledSpinBox
                                onLoaded: {
                                    settingsDialog.setLoaderProperty(awbTintBiasLoader, "from", -50)
                                    settingsDialog.setLoaderProperty(awbTintBiasLoader, "to", 50)
                                    settingsDialog.setLoaderProperty(awbTintBiasLoader, "value", settingsDialog.awbTintBias)
                                    settingsDialog.connectLoaderSignal(awbTintBiasLoader, "valueChanged", function() {
                                        settingsDialog.awbTintBias = settingsDialog.loaderProperty(awbTintBiasLoader, "value", settingsDialog.awbTintBias)
                                    })
                                }
                                Binding {
                                    target: awbTintBiasLoader.item
                                    property: "value"
                                    value: settingsDialog.awbTintBias
                                    when: awbTintBiasLoader.item
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
                                id: awbLumaLowerLoader
                                sourceComponent: styledSpinBox
                                onLoaded: {
                                    settingsDialog.setLoaderProperty(awbLumaLowerLoader, "from", 0)
                                    settingsDialog.setLoaderProperty(awbLumaLowerLoader, "to", 255)
                                    settingsDialog.setLoaderProperty(awbLumaLowerLoader, "value", settingsDialog.awbLumaLowerBound)
                                    settingsDialog.connectLoaderSignal(awbLumaLowerLoader, "valueChanged", function() {
                                        settingsDialog.awbLumaLowerBound = settingsDialog.loaderProperty(awbLumaLowerLoader, "value", settingsDialog.awbLumaLowerBound)
                                    })
                                }
                                Binding {
                                    target: awbLumaLowerLoader.item
                                    property: "value"
                                    value: settingsDialog.awbLumaLowerBound
                                    when: awbLumaLowerLoader.item
                                }
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
                                id: awbLumaUpperLoader
                                sourceComponent: styledSpinBox
                                onLoaded: {
                                    settingsDialog.setLoaderProperty(awbLumaUpperLoader, "from", 0)
                                    settingsDialog.setLoaderProperty(awbLumaUpperLoader, "to", 255)
                                    settingsDialog.setLoaderProperty(awbLumaUpperLoader, "value", settingsDialog.awbLumaUpperBound)
                                    settingsDialog.connectLoaderSignal(awbLumaUpperLoader, "valueChanged", function() {
                                        settingsDialog.awbLumaUpperBound = settingsDialog.loaderProperty(awbLumaUpperLoader, "value", settingsDialog.awbLumaUpperBound)
                                    })
                                }
                                Binding {
                                    target: awbLumaUpperLoader.item
                                    property: "value"
                                    value: settingsDialog.awbLumaUpperBound
                                    when: awbLumaUpperLoader.item
                                }
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
                                id: awbRgbLowerLoader
                                sourceComponent: styledSpinBox
                                onLoaded: {
                                    settingsDialog.setLoaderProperty(awbRgbLowerLoader, "from", 0)
                                    settingsDialog.setLoaderProperty(awbRgbLowerLoader, "to", 255)
                                    settingsDialog.setLoaderProperty(awbRgbLowerLoader, "value", settingsDialog.awbRgbLowerBound)
                                    settingsDialog.connectLoaderSignal(awbRgbLowerLoader, "valueChanged", function() {
                                        settingsDialog.awbRgbLowerBound = settingsDialog.loaderProperty(awbRgbLowerLoader, "value", settingsDialog.awbRgbLowerBound)
                                    })
                                }
                                Binding {
                                    target: awbRgbLowerLoader.item
                                    property: "value"
                                    value: settingsDialog.awbRgbLowerBound
                                    when: awbRgbLowerLoader.item
                                }
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
                                id: awbRgbUpperLoader
                                sourceComponent: styledSpinBox
                                onLoaded: {
                                    settingsDialog.setLoaderProperty(awbRgbUpperLoader, "from", 0)
                                    settingsDialog.setLoaderProperty(awbRgbUpperLoader, "to", 255)
                                    settingsDialog.setLoaderProperty(awbRgbUpperLoader, "value", settingsDialog.awbRgbUpperBound)
                                    settingsDialog.connectLoaderSignal(awbRgbUpperLoader, "valueChanged", function() {
                                        settingsDialog.awbRgbUpperBound = settingsDialog.loaderProperty(awbRgbUpperLoader, "value", settingsDialog.awbRgbUpperBound)
                                    })
                                }
                                Binding {
                                    target: awbRgbUpperLoader.item
                                    property: "value"
                                    value: settingsDialog.awbRgbUpperBound
                                    when: awbRgbUpperLoader.item
                                }
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
                id: cancelButton
                text: "Cancel"
                Layout.preferredWidth: 100
                onClicked: settingsDialog.visible = false
                
                contentItem: Text {
                    text: cancelButton.text
                    font: cancelButton.font
                    color: settingsDialog.textColor
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    color: cancelButton.pressed ? "#40ffffff" : "#20ffffff"
                    radius: 4
                    border.color: cancelButton.hovered ? "#60ffffff" : "transparent"
                }
            }

            Button {
                id: saveButton
                text: "Save"
                Layout.preferredWidth: 100
                highlighted: true
                onClicked: settingsDialog.saveSettings()
                
                contentItem: Text {
                    text: saveButton.text
                    font: saveButton.font
                    color: "white"
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    color: saveButton.pressed ? Qt.darker(settingsDialog.accentColor, 1.1) : settingsDialog.accentColor
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
            if (settingsDialog.uiStateRef) settingsDialog.cacheUsage = settingsDialog.uiStateRef.get_cache_usage_gb()
        }
    }
}
