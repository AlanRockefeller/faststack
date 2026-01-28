import QtQuick
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: jumpDialog
    title: "Jump to Image"
    standardButtons: Dialog.Ok | Dialog.Cancel
    modal: true
    closePolicy: Popup.CloseOnEscape
    width: 400

    property int maxImageCount: 0
    property color backgroundColor: "red" // Placeholder, will be set from Main.qml
    property color textColor: "white" // Placeholder, will be set from Main.qml


    // Inherit Material theme from parent
    // Material.theme: uiState && uiState.theme === 0 ? Material.Dark : Material.Light
    // Material.accent: "#4fb360"
    background: Rectangle {
        color: jumpDialog.backgroundColor
    }

    onOpened: {
        imageNumberField.text = ""
        imageNumberField.forceActiveFocus()
        // Notify Python that a dialog is open
        controller.dialog_opened()
    }
    
    onClosed: {
        // Notify Python that dialog is closed
        controller.dialog_closed()
    }

    onAccepted: {
        var num = parseInt(imageNumberField.text)
        if (!isNaN(num) && num >= 1 && num <= maxImageCount) {
            controller.jump_to_image(num - 1) // Convert 1-based to 0-based index
        }
    }

    contentItem: Item {
        implicitWidth: 400
        implicitHeight: 100

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 0
            spacing: 20

            Label {
                text: "Enter image number (1-" + jumpDialog.maxImageCount + "):"
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                color: jumpDialog.textColor
            }

            TextField {
                id: imageNumberField
                Layout.preferredWidth: 100
                Layout.preferredHeight: 40
                Layout.alignment: Qt.AlignLeft
                placeholderText: "Number"
                font.pixelSize: 16
                horizontalAlignment: TextInput.AlignHCenter
                maximumLength: Math.max(1, Math.ceil(Math.log10(jumpDialog.maxImageCount + 1)))
                selectByMouse: true
                focus: true
                validator: IntValidator {
                    bottom: 1
                    top: jumpDialog.maxImageCount
                }
                color: jumpDialog.textColor
                background: Rectangle {
                    color: jumpDialog.backgroundColor
                }
                
                Keys.onReturnPressed: jumpDialog.accept()
                Keys.onEnterPressed: jumpDialog.accept()
            }

            Item {
                Layout.fillHeight: true
            }
        }
    }
}
