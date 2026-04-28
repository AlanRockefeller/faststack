import QtQuick
import QtQuick.Layouts 1.15

RowLayout {
    id: root

    property bool overlaidMode: true
    signal modeRequested(bool overlaid)

    Item { Layout.fillWidth: true }

    Row {
        spacing: 0

        Rectangle {
            width: 70; height: 20
            radius: 3
            color: root.overlaidMode ? "#2c2c2c" : "transparent"
            border.color: "#3a3a3a"; border.width: 1

            Text {
                anchors.centerIn: parent
                text: "Overlaid"
                font.pixelSize: 10
                color: root.overlaidMode ? "#e8e6e3" : "#6b6764"
            }

            MouseArea {
                anchors.fill: parent
                onClicked: root.modeRequested(true)
            }
        }

        Rectangle {
            width: 70; height: 20
            radius: 3
            color: !root.overlaidMode ? "#2c2c2c" : "transparent"
            border.color: "#3a3a3a"; border.width: 1

            Text {
                anchors.centerIn: parent
                text: "Channels"
                font.pixelSize: 10
                color: !root.overlaidMode ? "#e8e6e3" : "#6b6764"
            }

            MouseArea {
                anchors.fill: parent
                onClicked: root.modeRequested(false)
            }
        }
    }
}
