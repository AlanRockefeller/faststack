import QtQuick
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: colorInfoDialog
    title: "Color Information"
    standardButtons: Dialog.Ok
    modal: true
    closePolicy: Popup.CloseOnEscape
    width: 560
    height: 500

    property string infoText: ""

    property color backgroundColor: "#333333"
    property color textColor: "#ffffff"
    property var controllerRef: typeof controller !== "undefined" ? controller : null

    background: Rectangle {
        color: colorInfoDialog.backgroundColor
        border.color: "#555555"
        border.width: 1
    }

    onOpened: {
        if (colorInfoDialog.controllerRef) {
            colorInfoDialog.controllerRef.dialog_opened()
        }
    }

    onClosed: {
        if (colorInfoDialog.controllerRef) {
            colorInfoDialog.controllerRef.dialog_closed()
        }
    }

    contentItem: ColumnLayout {
        spacing: 10

        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true

            TextArea {
                text: colorInfoDialog.infoText
                readOnly: true
                wrapMode: Text.Wrap
                selectByMouse: true
                color: colorInfoDialog.textColor
                background: null
                font.family: "Monospace"
                font.pixelSize: 13
            }
        }
    }
}
