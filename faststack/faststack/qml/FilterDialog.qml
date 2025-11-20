import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15

Dialog {
    id: filterDialog
    title: "Filter Images"
    modal: true
    standardButtons: Dialog.Ok | Dialog.Cancel
    width: 400
    height: 200

    property string filterString: ""

    // Match the app's theme dynamically
    Material.theme: uiState && uiState.theme === 0 ? Material.Dark : Material.Light

    background: Rectangle {
        color: Material.theme === Material.Dark ? "#1e1e1e" : "white"
        border.color: Material.theme === Material.Dark ? "#404040" : "#c0c0c0"
        border.width: 1
        radius: 4
    }

    contentItem: Column {
        spacing: 16
        anchors.fill: parent
        anchors.margins: 20

        Label {
            text: "Show only images whose filename contains:"
            wrapMode: Text.WordWrap
            width: parent.width
        }

        TextField {
            id: filterField
            text: filterDialog.filterString
            placeholderText: "Enter text to filter (e.g., 'stacked', 'IMG_001')..."
            width: parent.width
            selectByMouse: true
            focus: true
            
            onTextChanged: {
                filterDialog.filterString = text
            }
            
            Keys.onReturnPressed: {
                filterDialog.accept()
            }
        }

        Label {
            text: "Leave empty to show all images."
            font.italic: true
            opacity: 0.7
            wrapMode: Text.WordWrap
            width: parent.width
        }
    }

    onOpened: {
        // Load current filter string from controller
        var current = controller.get_filter_string ? controller.get_filter_string() : ""
        filterDialog.filterString = current || ""
        filterField.text = filterDialog.filterString
        filterField.forceActiveFocus()
        filterField.selectAll()
    }
}
