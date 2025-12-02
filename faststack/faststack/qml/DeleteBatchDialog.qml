import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15

Dialog {
    id: deleteBatchDialog
    title: "Delete Images"
    modal: true
    standardButtons: Dialog.NoButton
    closePolicy: Popup.CloseOnEscape
    width: 450
    height: 250

    property int batchCount: 0
    property color backgroundColor: "#1e1e1e"
    property color textColor: "white"

    background: Rectangle {
        color: deleteBatchDialog.backgroundColor
        border.color: "#404040"
        border.width: 1
        radius: 4
    }

    contentItem: Column {
        spacing: 20
        padding: 20

        Label {
            text: `You have ${batchCount} image${batchCount === 1 ? '' : 's'} selected in a batch.`
            wrapMode: Text.WordWrap
            width: parent.width - parent.padding * 2
            color: deleteBatchDialog.textColor
            font.pixelSize: 14
        }

        Label {
            text: "What would you like to delete?"
            wrapMode: Text.WordWrap
            width: parent.width - parent.padding * 2
            color: deleteBatchDialog.textColor
            font.pixelSize: 14
        }

        Row {
            spacing: 10
            anchors.horizontalCenter: parent.horizontalCenter

            Button {
                text: "Delete Current Image"
                onClicked: {
                    deleteBatchDialog.close()
                    if (controller) {
                        controller.delete_current_image_only()
                    }
                }
                background: Rectangle {
                    color: parent.pressed ? "#555555" : (parent.hovered ? "#666666" : "#444444")
                    radius: 4
                }
                contentItem: Text {
                    text: parent.text
                    color: deleteBatchDialog.textColor
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }

            Button {
                text: `Delete All (${batchCount})`
                onClicked: {
                    deleteBatchDialog.close()
                    if (controller) {
                        controller.delete_batch_images()
                    }
                }
                background: Rectangle {
                    color: parent.pressed ? "#cc0000" : (parent.hovered ? "#ff0000" : "#aa0000")
                    radius: 4
                }
                contentItem: Text {
                    text: parent.text
                    color: "white"
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    font.bold: true
                }
            }

            Button {
                text: "Cancel"
                onClicked: {
                    deleteBatchDialog.close()
                }
                background: Rectangle {
                    color: parent.pressed ? "#555555" : (parent.hovered ? "#666666" : "#444444")
                    radius: 4
                }
                contentItem: Text {
                    text: parent.text
                    color: deleteBatchDialog.textColor
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }
    }

    onOpened: {
        // Notify Python that a dialog is open
        if (controller) {
            controller.dialog_opened()
        }
    }
    
    onClosed: {
        // Notify Python that dialog is closed
        if (controller) {
            controller.dialog_closed()
        }
    }
}
