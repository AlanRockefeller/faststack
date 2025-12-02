import QtQuick
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: exifDialog
    title: "EXIF Data"
    standardButtons: Dialog.Ok
    modal: true
    closePolicy: Popup.CloseOnEscape
    width: 500
    height: 600

    property var summaryData: ({})
    property var fullData: ({})
    property bool showFull: false
    
    // Theme properties (can be bound from Main.qml)
    property color backgroundColor: "#333333"
    property color textColor: "#ffffff"

    background: Rectangle {
        color: exifDialog.backgroundColor
        border.color: "#555555"
        border.width: 1
    }

    onOpened: {
        // Reset to summary view when opened
        showFull = false
        // Notify Python that a dialog is open
        controller.dialog_opened()
    }
    
    onClosed: {
        controller.dialog_closed()
    }

    contentItem: ColumnLayout {
        spacing: 10

        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true

            TextArea {
                id: dataText
                text: exifDialog.getDisplayText()
                readOnly: true
                wrapMode: Text.Wrap
                color: exifDialog.textColor
                background: null
                font.family: "Consolas, monospace"
                font.pixelSize: 14
            }
        }

        Button {
            text: exifDialog.showFull ? "Show Summary" : "Show All"
            Layout.alignment: Qt.AlignRight
            onClicked: {
                exifDialog.showFull = !exifDialog.showFull
            }
        }
    }

    function getDisplayText() {
        var data = showFull ? fullData : summaryData
        var text = ""
        
        if (showFull) {
            // Sort keys for full view
            var keys = Object.keys(data).sort()
            for (var i = 0; i < keys.length; i++) {
                text += keys[i] + ": " + data[keys[i]] + "\n"
            }
        } else {
            // Specific order for summary
            var order = ["Date Taken", "Camera", "Lens", "ISO", "Aperture", "Shutter Speed", "Focal Length", "Flash", "GPS"]
            for (var i = 0; i < order.length; i++) {
                var key = order[i]
                if (data[key]) {
                    text += key + ": " + data[key] + "\n"
                }
            }
            
            // Add any other keys not in the ordered list (if any)
            for (var key in data) {
                if (order.indexOf(key) === -1) {
                     text += key + ": " + data[key] + "\n"
                }
            }
        }
        
        if (text === "") {
            return "No EXIF data found."
        }
        return text
    }
}
