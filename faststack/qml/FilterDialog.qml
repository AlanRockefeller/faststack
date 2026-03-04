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
    height: 400

    property string filterString: ""
    property var filterFlags: []
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
        spacing: 12
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

        // Flag filter section
        Label {
            text: "Show only images with these flags:"
            wrapMode: Text.WordWrap
            width: parent.width - parent.padding * 2
            color: filterDialog.textColor
            topPadding: 4
        }

        Grid {
            columns: 3
            columnSpacing: 16
            rowSpacing: 4
            width: parent.width - parent.padding * 2

            CheckBox {
                id: cbUploaded
                text: "Uploaded"
                checked: false
                Material.foreground: filterDialog.textColor
                Material.accent: "#4fc3f7"
                onCheckedChanged: _collectFlags()
            }
            CheckBox {
                id: cbStacked
                text: "Stacked"
                checked: false
                Material.foreground: filterDialog.textColor
                Material.accent: "#81c784"
                onCheckedChanged: _collectFlags()
            }
            CheckBox {
                id: cbEdited
                text: "Edited"
                checked: false
                Material.foreground: filterDialog.textColor
                Material.accent: "#ffb74d"
                onCheckedChanged: _collectFlags()
            }
            CheckBox {
                id: cbRestacked
                text: "Restacked"
                checked: false
                Material.foreground: filterDialog.textColor
                Material.accent: "#ce93d8"
                onCheckedChanged: _collectFlags()
            }
            CheckBox {
                id: cbTodo
                text: "Todo"
                checked: false
                Material.foreground: filterDialog.textColor
                Material.accent: "#64B5F6"
                onCheckedChanged: _collectFlags()
            }
            CheckBox {
                id: cbFavorite
                text: "Favorite"
                checked: false
                Material.foreground: filterDialog.textColor
                Material.accent: "#ffd54f"
                onCheckedChanged: _collectFlags()
            }
        }

        Label {
            text: "Leave empty and unchecked to show all images."
            font.italic: true
            opacity: 0.7
            wrapMode: Text.WordWrap
            width: parent.width - parent.padding * 2
            color: filterDialog.textColor
        }
    }

    function _collectFlags() {
        var flags = []
        if (cbUploaded.checked) flags.push("uploaded")
        if (cbStacked.checked) flags.push("stacked")
        if (cbEdited.checked) flags.push("edited")
        if (cbRestacked.checked) flags.push("restacked")
        if (cbTodo.checked) flags.push("todo")
        if (cbFavorite.checked) flags.push("favorite")
        filterDialog.filterFlags = flags
    }

    onAccepted: {
        // Flags are now collected live via onCheckedChanged
    }

    onOpened: {
        // Load current filter string from controller
        var current = controller && controller.get_filter_string ? controller.get_filter_string() : ""
        filterDialog.filterString = current || ""
        filterField.text = filterDialog.filterString

        // Load current filter flags from controller
        var currentFlags = controller && controller.get_filter_flags ? controller.get_filter_flags() : []
        cbUploaded.checked = currentFlags.indexOf("uploaded") >= 0
        cbStacked.checked = currentFlags.indexOf("stacked") >= 0
        cbEdited.checked = currentFlags.indexOf("edited") >= 0
        cbRestacked.checked = currentFlags.indexOf("restacked") >= 0
        cbTodo.checked = currentFlags.indexOf("todo") >= 0
        cbFavorite.checked = currentFlags.indexOf("favorite") >= 0

        filterField.forceActiveFocus()
        filterField.selectAll()
        // Notify Python that a dialog is open
        if (controller && controller.dialog_opened) {
            controller.dialog_opened()
        }
    }

    onClosed: {
        // Notify Python that dialog is closed
        if (controller && controller.dialog_closed) {
            controller.dialog_closed()
        }
    }
}
