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
    property var controllerRef: typeof controller !== "undefined" ? controller : null

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
            text: `You have ${deleteBatchDialog.batchCount} image${deleteBatchDialog.batchCount === 1 ? '' : 's'} selected in a batch.`
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
                id: deleteCurrentButton
                text: "Delete Current Image"
                onClicked: {
                    deleteBatchDialog.close()
                    if (deleteBatchDialog.controllerRef) {
                        deleteBatchDialog.controllerRef.delete_current_image_only()
                    }
                }
                background: Rectangle {
                    color: deleteCurrentButton.down ? "#555555" : (deleteCurrentButton.hovered ? "#666666" : "#444444")
                    radius: 4
                }
                contentItem: Text {
                    text: deleteCurrentButton.text
                    color: deleteBatchDialog.textColor
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }

            Button {
                id: deleteAllButton
                text: `Delete All (${deleteBatchDialog.batchCount})`
                onClicked: {
                    deleteBatchDialog.close()
                    if (deleteBatchDialog.controllerRef) {
                        deleteBatchDialog.controllerRef.delete_batch_images()
                    }
                }
                background: Rectangle {
                    color: deleteAllButton.down ? "#cc0000" : (deleteAllButton.hovered ? "#ff0000" : "#aa0000")
                    radius: 4
                }
                contentItem: Text {
                    text: deleteAllButton.text
                    color: deleteBatchDialog.textColor
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    font.bold: true
                }
            }

            Button {
                id: cancelDeleteButton
                text: "Cancel"
                onClicked: {
                    deleteBatchDialog.close()
                }
                background: Rectangle {
                    color: cancelDeleteButton.down ? "#555555" : (cancelDeleteButton.hovered ? "#666666" : "#444444")
                    radius: 4
                }
                contentItem: Text {
                    text: cancelDeleteButton.text
                    color: deleteBatchDialog.textColor
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }
    }

    onOpened: {
        // Notify Python that a dialog is open
        if (deleteBatchDialog.controllerRef) {
            deleteBatchDialog.controllerRef.dialog_opened("qml-delete-batch")
        }
    }
    
    onClosed: {
        // Notify Python that dialog is closed
        if (deleteBatchDialog.controllerRef) {
            deleteBatchDialog.controllerRef.dialog_closed("qml-delete-batch")
        }
    }

    Component.onDestruction: {
        if (deleteBatchDialog.controllerRef)
            deleteBatchDialog.controllerRef.dialog_closed("qml-delete-batch")
    }
}
