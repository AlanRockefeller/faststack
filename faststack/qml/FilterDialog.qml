import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15

Dialog {
    id: filterDialog
    title: "Filter Images"
    modal: true
    standardButtons: Dialog.Ok | Dialog.Cancel
    closePolicy: Popup.CloseOnEscape
    width: 500
    height: 250

    property string filterString: ""
    property color backgroundColor: "#1e1e1e"
    property color textColor: "white"


    // Match the app's theme dynamically
    // Material.theme: uiState && uiState.theme === 0 ? Material.Dark : Material.Light

    background: Rectangle {
        color: filterDialog.backgroundColor
        border.color: "#404040"
        border.width: 1
        radius: 4
    }

    contentItem: Column {
        spacing: 16
        padding: 20

        Label {
            text: "Show only images whose filename contains:"
            wrapMode: Text.WordWrap
            width: parent.width - parent.padding * 2
            color: filterDialog.textColor
        }

        TextField {
            id: filterField
            placeholderText: "Enter text to filter (e.g., 'stacked', 'IMG_001')..."
            width: parent.width - parent.padding * 2
            height: 50
            selectByMouse: true
            focus: true
            font.pixelSize: 16
            verticalAlignment: TextInput.AlignVCenter
            color: filterDialog.textColor
            background: Rectangle {
                color: Qt.lighter(filterDialog.backgroundColor, 1.2)
                border.color: "#505050"
                border.width: 1
                radius: 2
            }
            
            onTextChanged: {
                filterDialog.filterString = text
            }
            
            Keys.onReturnPressed: filterDialog.accept()
            Keys.onEnterPressed: filterDialog.accept()
        }
        Label {
            text: "Leave empty to show all images."
            font.italic: true
            opacity: 0.7
            wrapMode: Text.WordWrap
            width: parent.width - parent.padding * 2
            color: filterDialog.textColor
        }
    }

    onOpened: {
        // Load current filter string from controller
        var current = controller && controller.get_filter_string ? controller.get_filter_string() : ""
        filterDialog.filterString = current || ""
        filterField.text = filterDialog.filterString
        filterField.forceActiveFocus()
        filterField.selectAll()
        // Notify Python that a dialog is open
        if (controller && controller.dialog_opened) {
            controller.dialog_opened()
        }
    onClosed: {
        // Notify Python that dialog is closed
        if (controller && controller.dialog_closed) {
            controller.dialog_closed()
        }
    }    }
}
