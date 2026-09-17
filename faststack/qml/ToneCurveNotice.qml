pragma ComponentBehavior: Bound

import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

ColumnLayout {
    id: notice
    required property var uiStateRef
    required property var controllerRef
    readonly property var version: uiStateRef ? uiStateRef.tone_curve_version : 2
    readonly property bool supported: version === 1 || version === 2

    Layout.fillWidth: true
    visible: version !== 2
    spacing: 4

    Label {
        Layout.fillWidth: true
        text: notice.supported
            ? "This photo uses the previous Brightness and Contrast adjustments."
            : "These saved edits need a compatible FastStack version. They cannot be rendered or saved here; the saved JPG is unchanged."
        wrapMode: Text.WordWrap
        font.pixelSize: 11
        color: notice.supported ? "#aaaaaa" : "#f0b963"
    }

    Button {
        objectName: "useImprovedAdjustments"
        text: "Use improved adjustments"
        Layout.fillWidth: true
        visible: notice.version === 1
        enabled: notice.controllerRef && notice.uiStateRef
                 && !notice.uiStateRef.isCropping && !notice.uiStateRef.isSaving
        font.pixelSize: 11
        ToolTip.visible: hovered
        ToolTip.delay: 500
        ToolTip.text: "Change the tone response without resetting the crop or slider values. Saves with your other edits."
        onClicked: notice.controllerRef.use_improved_adjustments()
    }
}
