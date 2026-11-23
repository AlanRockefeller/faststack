import QtQuick
import QtQuick.Controls 2.15

// Status bar meter for W (RAW white balance): a strip of falling-code cells
// that fills as RawTherapee works. RawTherapee reports no progress, so the
// fill is an estimate: each stage eases towards the end of its span over the
// time earlier runs took, and the bar only completes when the run is done.
Item {
    id: meter

    property var info: ({ active: false })
    readonly property bool shown: !!info && info.active === true
    readonly property string runState: shown ? (info.state || "") : ""
    readonly property color phosphor: runState === "failed" ? "#ff4040" : "#00ff41"
    readonly property color phosphorDim: runState === "failed" ? "#3d0d0d" : "#0b3a14"
    readonly property int cellCount: 24
    readonly property string glyphSet:
        "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789:=*+<>"

    property real progress: 0
    property string lastOpId: ""
    property var glyphs: []
    property int frame: 0

    implicitWidth: 360
    implicitHeight: 20
    visible: opacity > 0.01
    opacity: shown ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 450 } }

    function stageTag() {
        if (runState === "failed") return "ERR"
        if (runState === "done") return "OK!"
        switch (info.stage) {
        case "save": return "SAV"
        case "develop": return "DEV"
        case "highlights": return "HLR"
        case "finish": return "FIN"
        }
        return "AWB"
    }

    function glyph() {
        return glyphSet.charAt(Math.floor(Math.random() * glyphSet.length))
    }

    function step() {
        if (!shown) return
        var opId = info.opId || ""
        if (opId !== lastOpId) {
            lastOpId = opId
            progress = 0
        }
        if (runState === "done") {
            progress = 1
        } else if (runState === "running") {
            var span = info.to - info.from
            var t = Math.max(0, Date.now() - info.startMs) / Math.max(1, info.expectedMs)
            // Eases towards the end of the span and never quite gets there.
            var p = info.from + span * (1 - Math.exp(-1.6 * t))
            if (p > progress) progress = p
        }
        var g = []
        for (var i = 0; i < cellCount; i++) g.push(glyph())
        glyphs = g
        frame++
    }

    onInfoChanged: step()

    Timer {
        interval: 70
        repeat: true
        running: meter.visible
        onTriggered: meter.step()
    }

    // Outer glow.
    Rectangle {
        anchors.fill: parent
        anchors.margins: -3
        radius: 5
        color: "transparent"
        border.color: meter.phosphor
        border.width: 2
        opacity: meter.runState === "running" ? 0.18 + 0.12 * Math.sin(meter.frame / 4) : 0.25
    }

    Rectangle {
        id: screen
        anchors.fill: parent
        radius: 2
        color: "#020a04"
        border.color: meter.phosphor
        border.width: 1
        clip: true

        Row {
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: parent.left
            anchors.leftMargin: 5
            spacing: 5

            Text {
                text: "[" + meter.stageTag() + "]"
                color: meter.phosphor
                font.family: "Monospace"
                font.pixelSize: 11
                font.bold: true
                anchors.verticalCenter: parent.verticalCenter
            }

            Row {
                id: cells
                spacing: 0
                anchors.verticalCenter: parent.verticalCenter
                readonly property int filled: Math.round(meter.progress * meter.cellCount)

                Repeater {
                    model: meter.cellCount
                    Text {
                        required property int index
                        width: 9
                        horizontalAlignment: Text.AlignHCenter
                        font.family: "Monospace"
                        font.pixelSize: 12
                        text: index < cells.filled
                              ? (meter.glyphs[index] || "0")
                              : (index === cells.filled && meter.runState === "running"
                                 && meter.frame % 6 < 3 ? "_" : "·")
                        // The newest cell burns white-hot; older ones fade
                        // back, like a falling column of code.
                        color: {
                            if (index >= cells.filled) return meter.phosphorDim
                            if (meter.runState === "failed") return meter.phosphor
                            if (index === cells.filled - 1) return "#e6ffe9"
                            var age = (cells.filled - 1 - index) / Math.max(1, meter.cellCount)
                            return Qt.rgba(0, 1 - 0.55 * age, 0.25 - 0.15 * age, 1)
                        }
                    }
                }
            }

            Text {
                text: meter.runState === "failed"
                      ? "FAIL"
                      : ("00" + Math.floor(meter.progress * 100)).slice(-3) + "%"
                color: meter.phosphor
                font.family: "Monospace"
                font.pixelSize: 11
                anchors.verticalCenter: parent.verticalCenter
            }

            Text {
                text: (meter.shown && meter.info.edits ? "+ED " : "")
                      + (meter.shown && meter.info.count > 1 ? "x" + meter.info.count : "")
                visible: text !== ""
                color: "#b6ff00"
                font.family: "Monospace"
                font.pixelSize: 10
                anchors.verticalCenter: parent.verticalCenter
            }
        }

        // Scanline sweeping across the screen.
        Rectangle {
            width: 18
            height: parent.height
            visible: meter.runState === "running"
            x: (meter.frame * 7) % (screen.width + width) - width
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: "transparent" }
                GradientStop { position: 0.8; color: Qt.rgba(0, 1, 0.25, 0.10) }
                GradientStop { position: 1.0; color: Qt.rgba(0.8, 1, 0.8, 0.22) }
            }
        }
    }

    MouseArea {
        id: hover
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.NoButton
    }
    ToolTip.visible: hover.containsMouse && meter.shown
    ToolTip.delay: 300
    ToolTip.text: {
        if (!meter.shown) return ""
        var what = {
            "save": "saving your edits first",
            "develop": "RawTherapee: developing with auto white balance",
            "highlights": "RawTherapee: second pass, pulling blown highlights back"
                          + (meter.info.detail ? " (" + meter.info.detail + ")" : ""),
            "finish": "matching orientation and frame, writing the JPG"
        }[meter.info.stage] || ""
        if (meter.runState === "done") what = "done"
        if (meter.runState === "failed") what = "failed (see the status message)"
        var s = "W " + (meter.info.name || "") + ": " + what
        if (meter.info.edits && meter.runState === "running")
            s += "\nYour edits will be applied to the new JPG."
        if (meter.info.count > 1) s += "\n" + meter.info.count + " photos in progress"
        return s
    }
}
