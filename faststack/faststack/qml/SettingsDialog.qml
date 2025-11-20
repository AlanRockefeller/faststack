import QtQuick
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: settingsDialog
    title: "Settings"
    standardButtons: Dialog.Ok | Dialog.Cancel
    modal: true
    width: 600
    height: 600

    property string heliconPath: ""
    property double cacheSize: 1.5
    property int prefetchRadius: 4
    property int theme: 0
    property string defaultDirectory: ""
    property string photoshopPath: ""

    onAccepted: {
        uiState.set_helicon_path(heliconPath)
        uiState.set_photoshop_path(photoshopPath)
        uiState.set_cache_size(cacheSize)
        uiState.set_prefetch_radius(prefetchRadius)
        uiState.set_theme(theme)
        uiState.set_default_directory(defaultDirectory)
    }

    contentItem: GridLayout {
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
            text: settingsDialog.cacheSize.toFixed(1) // Display with one decimal place
            onTextChanged: {
                var value = parseFloat(text)
                if (!isNaN(value) && value >= 0.5 && value <= 16) {
                    settingsDialog.cacheSize = value
                } else if (text === "") { // Handle empty text
                    settingsDialog.cacheSize = 1.5 // Default to 1.5 if empty
                }
            }
        }
        Label {} // Placeholder

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
    }
}
