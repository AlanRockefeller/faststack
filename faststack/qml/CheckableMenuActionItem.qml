import QtQuick
import QtQuick.Layouts 1.15

Rectangle {
    id: checkableMenuItem

    property color hoverFillColor: "transparent"
    property color defaultTextColor: "white"
    property color disabledTextColor: "#888888"
    property bool checked: false
    property bool useEnabledHover: true
    property real disabledTextOpacity: 0.6
    property int textLeftPadding: 10
    property string itemText: ""
    signal clicked()

    width: 220
    height: 36
    color: mouseArea.hovered ? hoverFillColor : "transparent"

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: true
        onClicked: checkableMenuItem.clicked()
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: checkableMenuItem.textLeftPadding
        anchors.rightMargin: 10
        spacing: 8

        Text {
            text: checkableMenuItem.checked ? "☑" : "☐"
            color: checkableMenuItem.enabled ? checkableMenuItem.defaultTextColor : checkableMenuItem.disabledTextColor
            opacity: checkableMenuItem.enabled ? 1.0 : checkableMenuItem.disabledTextOpacity
            verticalAlignment: Text.AlignVCenter
            Layout.preferredWidth: implicitWidth
        }

        Text {
            text: checkableMenuItem.itemText
            color: checkableMenuItem.enabled ? checkableMenuItem.defaultTextColor : checkableMenuItem.disabledTextColor
            opacity: checkableMenuItem.enabled ? 1.0 : checkableMenuItem.disabledTextOpacity
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }
}
