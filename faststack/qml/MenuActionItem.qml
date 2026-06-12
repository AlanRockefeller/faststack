import QtQuick
import QtQuick.Controls 2.15

ItemDelegate {
    id: menuActionItem

    property color hoverFillColor: "transparent"
    property color selectedFillColor: "transparent"
    property color defaultTextColor: "white"
    property color disabledTextColor: "#888888"
    property bool selected: false
    property bool boldWhenSelected: true
    property bool useEnabledHover: true
    property real disabledTextOpacity: 0.6
    property int textLeftPadding: 10
    // Optional leading checkbox glyph for toggle-style menu entries. Named to
    // avoid AbstractButton.checked, whose setter force-enables checkable and
    // lets clicks overwrite the caller's binding.
    property bool showCheckbox: false
    property bool checkboxChecked: false

    height: 36
    hoverEnabled: true

    background: Rectangle {
        color: ((menuActionItem.enabled || !menuActionItem.useEnabledHover) && menuActionItem.hovered)
            ? menuActionItem.hoverFillColor
            : (menuActionItem.selected ? menuActionItem.selectedFillColor : "transparent")
    }

    contentItem: Text {
        text: (menuActionItem.showCheckbox
            ? (menuActionItem.checkboxChecked ? "☑ " : "☐ ")
            : "") + menuActionItem.text
        font.bold: menuActionItem.boldWhenSelected && menuActionItem.selected
        color: menuActionItem.enabled ? menuActionItem.defaultTextColor : menuActionItem.disabledTextColor
        opacity: menuActionItem.enabled ? 1.0 : menuActionItem.disabledTextOpacity
        verticalAlignment: Text.AlignVCenter
        leftPadding: menuActionItem.textLeftPadding
        elide: Text.ElideRight
    }
}
