pragma ComponentBehavior: Bound
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15

// Actions -> iNaturalist upload. Groups the batch into observation cards,
// identifies each with MycoMap Vision, lets the user fix the grouping
// (split / merge / drag photos between cards), pick a location on a map, and
// uploads to iNaturalist.
Window {
    id: dialog
    title: "Upload to iNaturalist"
    width: 1180
    height: 860
    minimumWidth: 820
    minimumHeight: 520
    visible: false
    flags: Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint | Qt.WindowMinMaxButtonsHint
    modality: Qt.ApplicationModal
    color: backgroundColor

    Material.theme: Material.Dark
    Material.accent: accentColor

    property color backgroundColor: "#1e1e1e"
    property color textColor: "white"
    readonly property color accentColor: "#6366f1"
    readonly property color cardColor: "#262626"
    readonly property color cardBorder: "#3a3a3a"
    readonly property color mutedText: "#9a9a9a"
    readonly property color okColor: "#4CAF50"
    readonly property color warnColor: "#ffb74d"
    readonly property color errColor: "#ef5350"

    property var controllerRef: typeof controller !== "undefined" ? controller : null
    property var uploader: typeof inatUpload !== "undefined" ? inatUpload : null

    onVisibleChanged: {
        if (visible) {
            if (controllerRef) controllerRef.dialog_opened("qml-inat-upload")
            if (uploader && !(uploader.hasInatToken && uploader.hasVisionKey)) settingsPopup.open()
        } else {
            if (controllerRef) controllerRef.dialog_closed("qml-inat-upload")
        }
    }
    Component.onDestruction: { if (controllerRef) controllerRef.dialog_closed("qml-inat-upload") }

    onClosing: function(close) {
        // Only an upload has to finish (or be cancelled) first: closing in
        // the middle of one would leave observations half attached. An
        // identification can take minutes when Vision is rate limiting, and
        // this window is application-modal, so waiting for one would lock
        // the whole app; those are dropped instead and the cards stay as
        // they are for the next time the dialog opens.
        if (uploader && uploader.uploading) {
            close.accepted = false
            dialog.toast("Wait for the upload to finish (or cancel it) before closing.")
            return
        }
        if (uploader) uploader.cancelIdentify()
    }

    function toast(text) {
        toastLabel.text = text
        toastTimer.restart()
    }

    Connections {
        target: dialog.uploader
        function onShowMessage(text) { dialog.toast(text) }
    }

    // ---------------------------------------------------------------
    // Drag ghost: one floating thumbnail that follows the mouse while a
    // photo is dragged between cards. Lives at the window root so it can
    // cross card boundaries and the scroll view.
    Item {
        id: dragGhost
        width: 120; height: 90
        z: 1000
        visible: dragging
        property bool dragging: false
        property string photoKey: ""
        property int sourceCardId: -1
        Drag.active: dragging
        Drag.hotSpot.x: width / 2
        Drag.hotSpot.y: height / 2
        Drag.keys: ["inat-photo"]
        Image {
            id: ghostImage
            anchors.fill: parent
            fillMode: Image.PreserveAspectFit
            opacity: 0.85
            asynchronous: true
        }
        Rectangle { anchors.fill: parent; color: "transparent"; border.color: dialog.accentColor; border.width: 2 }
    }

    // ---------------------------------------------------------------
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 10

        // Header
        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            Text {
                text: "iNaturalist upload"
                color: dialog.textColor
                font.pixelSize: 20
                font.bold: true
            }
            Text {
                text: dialog.uploader
                      ? (dialog.uploader.photoCount + " photos · " + dialog.uploader.cardCount + " observations")
                      : ""
                color: dialog.mutedText
                font.pixelSize: 13
            }
            Item { Layout.fillWidth: true }
            Text {
                text: dialog.uploader && dialog.uploader.inatLogin ? "iNat: " + dialog.uploader.inatLogin : "iNat: not signed in"
                color: dialog.uploader && dialog.uploader.inatLogin ? dialog.okColor : dialog.warnColor
                font.pixelSize: 12
            }
            ToolButton {
                text: "⚙ Settings"
                onClicked: settingsPopup.open()
            }
        }

        // Toolbar
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Button {
                text: "Identify all with MycoMap Vision"
                enabled: dialog.uploader && !dialog.uploader.busy && dialog.uploader.cardCount > 0
                onClicked: dialog.uploader.identifyAll()
            }
            Button {
                text: dialog.uploader && dialog.uploader.selectedCount > 1
                      ? "Merge " + dialog.uploader.selectedCount + " selected"
                      : "Merge selected"
                enabled: dialog.uploader && dialog.uploader.selectedCount > 1
                onClicked: dialog.uploader.mergeSelected()
            }
            Item { Layout.fillWidth: true }
            Text {
                visible: dialog.uploader && dialog.uploader.uploadTotal > 0 && dialog.uploader.busy
                text: dialog.uploader ? "Uploading " + dialog.uploader.uploadDone + " / " + dialog.uploader.uploadTotal : ""
                color: dialog.mutedText
                font.pixelSize: 12
            }
            Button {
                text: "Cancel upload"
                visible: dialog.uploader && dialog.uploader.uploadTotal > 0 && dialog.uploader.busy
                onClicked: dialog.uploader.cancelUpload()
            }
            Button {
                id: uploadButton
                highlighted: true
                text: dialog.uploader
                      ? (dialog.uploader.allUploaded ? "Done" : "Upload " + dialog.uploader.cardsToUpload + " to iNaturalist")
                      : "Upload"
                enabled: dialog.uploader && !dialog.uploader.busy && dialog.uploader.cardCount > 0
                onClicked: {
                    if (dialog.uploader.allUploaded) { dialog.close(); return }
                    if (dialog.uploader.cardsWithoutLocation > 0) confirmNoLocation.open()
                    else dialog.uploader.uploadAll()
                }
            }
        }

        // Grouping sliders: regroup live by time gap and GPS distance.
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: sliderRow.implicitHeight + 16
            radius: 8
            color: dialog.cardColor
            border.color: dialog.cardBorder

            // Log-scale mapping so the useful range (seconds to hours, metres
            // to kilometres) fits one slider.
            function toValue(pos, lo, hi) { return lo * Math.pow(hi / lo, pos) }
            function toPos(value, lo, hi) { return Math.log(Math.max(lo, Math.min(hi, value)) / lo) / Math.log(hi / lo) }
            function fmtSeconds(s) { return s < 90 ? Math.round(s) + " s" : (s < 5400 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h") }
            function fmtMeters(m) { return m < 1000 ? Math.round(m) + " m" : (m / 1000).toFixed(1) + " km" }
            id: sliderBox
            readonly property real timeLo: 15
            readonly property real timeHi: 6 * 3600
            readonly property real distLo: 2
            readonly property real distHi: 5000
            property bool syncing: false

            // Debounced push to the controller so dragging doesn't regroup per pixel.
            Timer {
                id: regroupTimer
                interval: 120
                onTriggered: {
                    if (!dialog.uploader) return
                    dialog.uploader.setThresholds(sliderBox.toValue(timeSlider.value, sliderBox.timeLo, sliderBox.timeHi),
                                                  sliderBox.toValue(distSlider.value, sliderBox.distLo, sliderBox.distHi))
                }
            }
            function syncFromController() {
                if (!dialog.uploader) return
                syncing = true
                timeSlider.value = toPos(dialog.uploader.timeGapSeconds, timeLo, timeHi)
                distSlider.value = toPos(dialog.uploader.distanceMetersLive, distLo, distHi)
                syncing = false
            }
            Connections {
                target: dialog.uploader
                function onThresholdsChanged() { sliderBox.syncFromController() }
            }
            Component.onCompleted: syncFromController()

            RowLayout {
                id: sliderRow
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.margins: 12
                spacing: 14

                Text { text: "New observation after"; color: dialog.mutedText; font.pixelSize: 12 }
                Slider {
                    id: timeSlider
                    Layout.preferredWidth: 220
                    from: 0; to: 1
                    enabled: dialog.uploader && dialog.uploader.canRegroup && !dialog.uploader.busy
                    onMoved: regroupTimer.restart()
                    ToolTip.visible: pressed
                    ToolTip.text: sliderBox.fmtSeconds(sliderBox.toValue(value, sliderBox.timeLo, sliderBox.timeHi))
                }
                Text {
                    text: sliderBox.fmtSeconds(sliderBox.toValue(timeSlider.value, sliderBox.timeLo, sliderBox.timeHi))
                    color: dialog.textColor; font.pixelSize: 13; font.bold: true
                    Layout.preferredWidth: 56
                }
                Text { text: "or a move of"; color: dialog.mutedText; font.pixelSize: 12 }
                Slider {
                    id: distSlider
                    Layout.preferredWidth: 220
                    from: 0; to: 1
                    enabled: timeSlider.enabled
                    onMoved: regroupTimer.restart()
                    ToolTip.visible: pressed
                    ToolTip.text: sliderBox.fmtMeters(sliderBox.toValue(value, sliderBox.distLo, sliderBox.distHi))
                }
                Text {
                    text: sliderBox.fmtMeters(sliderBox.toValue(distSlider.value, sliderBox.distLo, sliderBox.distHi))
                    color: dialog.textColor; font.pixelSize: 13; font.bold: true
                    Layout.preferredWidth: 56
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: dialog.uploader ? dialog.uploader.thresholdsNote
                          + (dialog.uploader.hasManualEdits ? " · moving a slider discards your manual splits/merges" : "")
                          : ""
                    color: "#777777"; font.pixelSize: 11
                    elide: Text.ElideRight
                    Layout.maximumWidth: 420
                }
            }
        }

        // Cards
        ScrollView {
            id: scroller
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

            Column {
                id: cardColumn
                width: scroller.availableWidth
                spacing: 12

                Repeater {
                    model: dialog.uploader ? dialog.uploader.cards : null
                    delegate: ObservationCard { width: cardColumn.width }
                }

                // Drop here to start a new observation
                Rectangle {
                    width: parent.width
                    height: 64
                    radius: 8
                    color: newDrop.containsDrag ? "#2f2f4a" : "transparent"
                    border.color: newDrop.containsDrag ? dialog.accentColor : dialog.cardBorder
                    border.width: 1
                    visible: dialog.uploader && dialog.uploader.photoCount > 1
                    Text {
                        anchors.centerIn: parent
                        text: "Drop a photo here to make it a separate observation"
                        color: dialog.mutedText
                        font.pixelSize: 13
                    }
                    DropArea {
                        id: newDrop
                        anchors.fill: parent
                        keys: ["inat-photo"]
                        onDropped: function(drop) {
                            dialog.uploader.movePhoto(dragGhost.photoKey, -1)
                            drop.accept()
                        }
                    }
                }
                Item { width: 1; height: 8 }
            }
        }

        // Footer
        RowLayout {
            Layout.fillWidth: true
            Text {
                Layout.fillWidth: true
                text: dialog.uploader ? dialog.uploader.statusText : ""
                color: dialog.mutedText
                font.pixelSize: 12
                elide: Text.ElideRight
            }
            BusyIndicator {
                running: dialog.uploader && dialog.uploader.busy
                visible: running
                implicitWidth: 22; implicitHeight: 22
            }
            Text {
                text: "Drag photos between cards to fix the grouping · tick cards and Merge to combine them"
                color: "#666666"
                font.pixelSize: 11
            }
            // Vision request counter: handy for people with a metered Vision plan.
            Text {
                text: dialog.uploader ? dialog.uploader.visionCounterText : ""
                color: dialog.okColor
                font.pixelSize: 12
                font.bold: true
                Layout.leftMargin: 12
                ToolTip.visible: counterHover.containsMouse
                ToolTip.text: "MycoMap Vision identify calls. Each card identified (or re-identified) is one request; the period figure comes from your MycoMap account."
                MouseArea { id: counterHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
            }
        }
    }

    // Toast
    Rectangle {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 48
        width: toastLabel.implicitWidth + 32
        height: toastLabel.implicitHeight + 18
        radius: 6
        color: "#e0333333"
        border.color: dialog.warnColor
        visible: toastTimer.running
        z: 900
        Text { id: toastLabel; anchors.centerIn: parent; color: "white"; font.pixelSize: 13 }
        Timer { id: toastTimer; interval: 4000 }
    }

    Dialog {
        id: confirmNoLocation
        title: "Observations without a location"
        modal: true
        anchors.centerIn: parent
        width: 460
        standardButtons: Dialog.Yes | Dialog.No
        background: Rectangle { color: "#2a2a2a"; radius: 8; border.color: "#4a4a4a"; border.width: 1 }
        contentItem: Text {
            text: (dialog.uploader ? dialog.uploader.cardsWithoutLocation : 0)
                  + " observation(s) have no location. iNaturalist will grade them Casual.\nUpload anyway?"
            color: dialog.textColor
            wrapMode: Text.Wrap
        }
        onAccepted: dialog.uploader.uploadAll()
    }

    // ---------------------------------------------------------------
    // Map picker
    Popup {
        id: mapPopup
        objectName: "mapPopup"
        modal: true
        anchors.centerIn: parent
        background: Rectangle { color: "#2a2a2a"; radius: 8; border.color: "#4a4a4a"; border.width: 1 }
        width: Math.min(900, dialog.width - 40)
        height: Math.min(700, dialog.height - 40)
        padding: 12
        property int cardId: -1
        property real pendingLat: 0
        property real pendingLon: 0
        property bool hasPending: false

        function openFor(cardId, hasLocation, lat, lon) {
            mapPopup.cardId = cardId
            mapPopup.hasPending = false
            open()
            if (hasLocation) {
                mapPicker.setMarker(lat, lon)
                mapPicker.centerOn(lat, lon, 15)
            } else if (mapPicker.hasMarker) {
                mapPicker.centerOn(mapPicker.markerLat, mapPicker.markerLon, mapPicker.zoom)
            } else {
                mapPicker.centerOn(39.5, -98.35, 4)
            }
        }

        contentItem: ColumnLayout {
            spacing: 8
            RowLayout {
                Layout.fillWidth: true
                Text { text: "Click the map to place this observation"; color: dialog.textColor; font.pixelSize: 15; font.bold: true }
                Item { Layout.fillWidth: true }
                TextField {
                    id: coordField
                    placeholderText: "lat, lon"
                    Layout.preferredWidth: 220
                    onAccepted: {
                        var parts = text.split(/[ ,]+/)
                        if (parts.length >= 2) {
                            var la = parseFloat(parts[0]); var lo = parseFloat(parts[1])
                            if (!isNaN(la) && !isNaN(lo)) {
                                mapPicker.setMarker(la, lo); mapPicker.centerOn(la, lo, 15)
                                mapPopup.pendingLat = la; mapPopup.pendingLon = lo; mapPopup.hasPending = true
                            }
                        }
                    }
                }
            }
            MapPicker {
                id: mapPicker
                Layout.fillWidth: true
                Layout.fillHeight: true
                onPicked: function(lat, lon) {
                    mapPopup.pendingLat = lat; mapPopup.pendingLon = lon; mapPopup.hasPending = true
                    coordField.text = lat.toFixed(5) + ", " + lon.toFixed(5)
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Text {
                    text: mapPopup.hasPending ? mapPopup.pendingLat.toFixed(5) + ", " + mapPopup.pendingLon.toFixed(5) : "No point chosen"
                    color: dialog.mutedText
                }
                Item { Layout.fillWidth: true }
                Button { text: "Cancel"; onClicked: mapPopup.close() }
                Button {
                    text: "Use this location"
                    highlighted: true
                    enabled: mapPopup.hasPending
                    onClicked: {
                        dialog.uploader.setLocation(mapPopup.cardId, mapPopup.pendingLat, mapPopup.pendingLon)
                        mapPopup.close()
                    }
                }
            }
        }
    }

    // ---------------------------------------------------------------
    // Settings
    Popup {
        id: settingsPopup
        objectName: "settingsPopup"
        modal: true
        anchors.centerIn: parent
        background: Rectangle { color: "#2a2a2a"; radius: 8; border.color: "#4a4a4a"; border.width: 1 }
        width: Math.min(720, dialog.width - 40)
        padding: 18
        // aboutToShow, not opened: opened() fires only after the enter
        // transition, which wiped a token pasted the moment the popup appeared.
        onAboutToShow: {
            tokenField.text = ""
            keyField.text = ""
            gapField.text = dialog.uploader ? dialog.uploader.timeGapMinutes : 10
            distField.text = dialog.uploader ? dialog.uploader.distanceMeters : 100
            spreadField.text = dialog.uploader ? dialog.uploader.spreadWarningMeters : 100
            geoCombo.currentIndex = geoCombo.indexOfValue(dialog.uploader ? dialog.uploader.defaultGeoprivacy : "open")
            tileDirField.text = dialog.uploader ? dialog.uploader.tileCacheDir : ""
            tileMbField.text = dialog.uploader ? dialog.uploader.tileCacheMb : 0
        }

        contentItem: ColumnLayout {
            spacing: 10
            Text { text: "iNaturalist upload settings"; color: dialog.textColor; font.pixelSize: 16; font.bold: true }

            Text {
                text: !dialog.uploader ? ""
                      : (dialog.uploader.keyringBackend === "file"
                         ? "Credentials (no OS keyring available: stored in an owner-only file in the FastStack data folder)"
                         : "Credentials (stored in the OS keyring: " + dialog.uploader.keyringBackend + ")")
                color: dialog.mutedText; font.pixelSize: 12
                wrapMode: Text.Wrap; Layout.fillWidth: true
            }
            GridLayout {
                columns: 3
                columnSpacing: 8
                Layout.fillWidth: true
                Text { text: "iNaturalist API token"; color: dialog.textColor }
                TextField {
                    id: tokenField
                    Layout.fillWidth: true
                    echoMode: TextInput.Password
                    placeholderText: dialog.uploader && dialog.uploader.hasInatToken ? "(saved) paste a new one to replace" : "paste the token from the page on the right"
                }
                Button {
                    text: "Get token"
                    onClicked: Qt.openUrlExternally(dialog.uploader.inatTokenUrl)
                }
                Text { text: ""; }
                Text {
                    Layout.columnSpan: 2
                    text: dialog.uploader
                          ? (dialog.uploader.verifyError ? dialog.uploader.verifyError
                             : (dialog.uploader.inatLogin ? "Signed in as " + dialog.uploader.inatLogin + " (tokens expire after 24 h)" : "Not verified yet"))
                          : ""
                    color: dialog.uploader && dialog.uploader.verifyError ? dialog.errColor : dialog.mutedText
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                Text { text: "MycoMap Vision API key"; color: dialog.textColor }
                TextField {
                    id: keyField
                    Layout.fillWidth: true
                    echoMode: TextInput.Password
                    placeholderText: dialog.uploader && dialog.uploader.hasVisionKey ? "(saved) paste a new one to replace" : "vision_…"
                }
                Button {
                    text: "Get key"
                    onClicked: Qt.openUrlExternally("https://mycomap.org/vision-keys")
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: dialog.cardBorder }
            Text { text: "Grouping defaults (the sliders start here until FastStack has learned from your uploads)"; color: dialog.textColor; font.bold: true; font.pixelSize: 12 }
            GridLayout {
                columns: 4
                columnSpacing: 8
                Text { text: "New observation after a gap of"; color: dialog.textColor }
                TextField { id: gapField; Layout.preferredWidth: 70; validator: IntValidator { bottom: 1; top: 1440 } }
                Text { text: "minutes, or a move of"; color: dialog.textColor }
                RowLayout {
                    TextField { id: distField; Layout.preferredWidth: 70; validator: IntValidator { bottom: 1; top: 100000 } }
                    Text { text: "m"; color: dialog.textColor }
                }
                Text { text: "Warn when a card's GPS fixes spread more than"; color: dialog.textColor }
                TextField { id: spreadField; Layout.preferredWidth: 70; validator: IntValidator { bottom: 1; top: 100000 } }
                Text { text: "m"; color: dialog.textColor }
                Item {}
                Text { text: "Default geoprivacy"; color: dialog.textColor }
                ComboBox {
                    id: geoCombo
                    Layout.columnSpan: 3
                    Layout.preferredWidth: 160
                    model: ["open", "obscured", "private"]
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: dialog.cardBorder }
            Text { text: "Map tile cache (OpenStreetMap)"; color: dialog.textColor; font.bold: true }
            GridLayout {
                columns: 3
                columnSpacing: 8
                Layout.fillWidth: true
                Text { text: "Folder"; color: dialog.textColor }
                TextField { id: tileDirField; Layout.fillWidth: true }
                Button {
                    text: "Browse…"
                    onClicked: {
                        var chosen = dialog.controllerRef ? dialog.controllerRef.open_directory_dialog(tileDirField.text) : ""
                        if (chosen) tileDirField.text = chosen
                    }
                }
                Text { text: "Size limit (MB, 0 = auto from free space)"; color: dialog.textColor }
                TextField { id: tileMbField; Layout.preferredWidth: 100; validator: IntValidator { bottom: 0; top: 1000000 } }
                Button { text: "Clear cache"; onClicked: dialog.uploader.clearTileCache() }
                Text { text: ""; }
                Text { Layout.columnSpan: 2; text: dialog.uploader ? dialog.uploader.tileCacheStats : ""; color: dialog.mutedText; font.pixelSize: 12 }
            }
            Text {
                text: "Tiles you look at often are kept longest; ones not seen in a while go first when the cache is full."
                color: "#777777"; font.pixelSize: 11; wrapMode: Text.Wrap; Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                Button {
                    text: "Forget iNat token"
                    visible: dialog.uploader && dialog.uploader.hasInatToken
                    onClicked: dialog.uploader.clearInatToken()
                }
                Button {
                    text: "Forget Vision key"
                    visible: dialog.uploader && dialog.uploader.hasVisionKey
                    onClicked: dialog.uploader.clearVisionKey()
                }
                Item { Layout.fillWidth: true }
                Button { text: "Cancel"; onClicked: settingsPopup.close() }
                Button {
                    text: "Save"
                    highlighted: true
                    onClicked: {
                        var u = dialog.uploader
                        if (!u) return
                        u.saveGroupingSettings(parseInt(gapField.text) || 10, parseInt(distField.text) || 100,
                                               parseInt(spreadField.text) || 100, geoCombo.currentText)
                        u.saveTileCacheSettings(tileDirField.text, parseInt(tileMbField.text) || 0)
                        u.saveCredentials(tokenField.text, keyField.text)
                        settingsPopup.close()
                    }
                }
            }
        }
    }

    // ---------------------------------------------------------------
    // One observation card
    component ObservationCard: Rectangle {
        id: card
        required property var model
        required property int index

        readonly property int cardId: model.cardId
        readonly property string status: model.status

        implicitHeight: cardLayout.implicitHeight + 24
        radius: 8
        color: cardDrop.containsDrag ? "#2a2a40" : dialog.cardColor
        border.width: model.selected ? 2 : 1
        border.color: model.selected ? dialog.accentColor
                      : (cardDrop.containsDrag ? dialog.accentColor
                         : (status === "error" ? dialog.errColor : dialog.cardBorder))

        DropArea {
            id: cardDrop
            anchors.fill: parent
            keys: ["inat-photo"]
            enabled: !card.model.locked
            onDropped: function(drop) {
                dialog.uploader.movePhoto(dragGhost.photoKey, card.cardId)
                drop.accept()
            }
        }

        ColumnLayout {
            id: cardLayout
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 12
            spacing: 8

            // Card header
            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                CheckBox {
                    checked: card.model.selected
                    enabled: !card.model.locked
                    onToggled: dialog.uploader.toggleSelected(card.cardId)
                    ToolTip.visible: hovered
                    ToolTip.text: "Tick two or more cards, then Merge"
                }
                Text {
                    text: "Observation " + (card.index + 1)
                    color: dialog.textColor
                    font.pixelSize: 16
                    font.bold: true
                }
                Text {
                    text: card.model.photoCount + (card.model.photoCount === 1 ? " photo" : " photos") + " · " + card.model.dateText
                    color: dialog.mutedText
                    font.pixelSize: 13
                }
                Item { Layout.fillWidth: true }

                // Split: tiny dropdown for how many observations are in here
                Text { text: "Split into"; color: dialog.mutedText; font.pixelSize: 12; visible: card.model.canSplit }
                ComboBox {
                    id: splitCount
                    visible: card.model.canSplit
                    implicitWidth: 64
                    model: {
                        var n = Math.min(10, card.model.photoCount)
                        var out = []
                        for (var i = 2; i <= n; i++) out.push(i)
                        return out
                    }
                    currentIndex: 0
                }
                Button {
                    text: "Split"
                    visible: card.model.canSplit
                    enabled: dialog.uploader && !dialog.uploader.busy
                    onClicked: dialog.uploader.splitCard(card.cardId, parseInt(splitCount.currentText) || 2)
                    ToolTip.visible: hovered
                    ToolTip.text: "Grouped too aggressively? Choose how many observations are in this card and split it by time and GPS."
                }
                Button {
                    text: card.status === "identifying" ? "Identifying…" : (card.status === "identified" ? "Re-identify" : "Identify")
                    visible: card.status !== "uploading" && card.status !== "uploaded"
                    enabled: card.status !== "identifying"
                    onClicked: dialog.uploader.identifyCard(card.cardId)
                }
            }

            // Photos
            Flow {
                Layout.fillWidth: true
                spacing: 8
                Repeater {
                    model: card.model.photos
                    delegate: Item {
                        id: thumb
                        required property var modelData
                        width: 220; height: 180
                        Rectangle {
                            anchors.fill: parent
                            color: "#151515"
                            radius: 4
                            border.color: thumbArea.containsMouse ? dialog.accentColor : "#333333"
                            Image {
                                anchors.fill: parent
                                anchors.margins: 2
                                source: thumb.modelData.source
                                fillMode: Image.PreserveAspectFit
                                asynchronous: true
                                sourceSize.width: 360
                                cache: true
                            }
                            Rectangle {
                                anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                                height: 20
                                color: "#90000000"
                                radius: 4
                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    anchors.left: parent.left; anchors.leftMargin: 6
                                    text: thumb.modelData.name
                                    color: "#dddddd"; font.pixelSize: 11
                                    elide: Text.ElideMiddle
                                    width: parent.width - 60
                                }
                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    anchors.right: parent.right; anchors.rightMargin: 6
                                    text: (thumb.modelData.hasGps ? "⌖ " : "") + thumb.modelData.taken
                                    color: thumb.modelData.hasGps ? "#9ccc65" : "#bbbbbb"
                                    font.pixelSize: 10
                                }
                            }
                        }
                        MouseArea {
                            id: thumbArea
                            anchors.fill: parent
                            hoverEnabled: true
                            enabled: !card.model.locked
                            cursorShape: enabled ? Qt.OpenHandCursor : Qt.ArrowCursor
                            drag.target: dragGhost
                            drag.threshold: 6
                            onPressed: function(mouse) {
                                var p = thumbArea.mapToItem(dialog.contentItem, mouse.x, mouse.y)
                                dragGhost.x = p.x - dragGhost.width / 2
                                dragGhost.y = p.y - dragGhost.height / 2
                                dragGhost.photoKey = thumb.modelData.key
                                dragGhost.sourceCardId = card.cardId
                                ghostImage.source = thumb.modelData.source
                            }
                            drag.onActiveChanged: {
                                if (thumbArea.drag.active) dragGhost.dragging = true
                            }
                            onReleased: {
                                if (dragGhost.dragging) {
                                    dragGhost.Drag.drop()
                                    dragGhost.dragging = false
                                }
                            }
                            onCanceled: dragGhost.dragging = false
                        }
                    }
                }
            }

            // Location row
            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                Text {
                    text: card.model.hasLocation
                          ? (card.model.lat.toFixed(5) + ", " + card.model.lon.toFixed(5)
                             + (card.model.locationSource === "manual" ? "  (chosen on map)"
                                : "  (EXIF, " + card.model.gpsCount + " fix" + (card.model.gpsCount === 1 ? "" : "es")
                                  + (card.model.gpsCount > 1 ? ", spread " + card.model.spreadMeters + " m" : "") + ")"))
                          : "No GPS in these photos"
                    color: card.model.hasLocation ? dialog.textColor : dialog.warnColor
                    font.pixelSize: 12
                }
                Text {
                    visible: card.model.spreadWarning
                    text: "⚠ GPS fixes are " + card.model.spreadMeters + " m apart — this card may hold more than one observation"
                    color: dialog.warnColor
                    font.pixelSize: 12
                    font.bold: true
                }
                Button {
                    text: card.model.hasLocation ? "Change on map" : "Pick on map"
                    flat: true
                    visible: card.status !== "uploading" && card.status !== "uploaded"
                    onClicked: mapPopup.openFor(card.cardId, card.model.hasLocation, card.model.lat, card.model.lon)
                }
                Button {
                    text: "Use EXIF again"
                    flat: true
                    visible: card.model.locationSource === "manual" && card.status !== "uploaded"
                    onClicked: dialog.uploader.clearManualLocation(card.cardId)
                }
                Item { Layout.fillWidth: true }
                Text { text: "Geoprivacy"; color: dialog.mutedText; font.pixelSize: 12 }
                ComboBox {
                    implicitWidth: 120
                    model: ["open", "obscured", "private"]
                    enabled: card.status !== "uploading" && card.status !== "uploaded"
                    currentIndex: Math.max(0, model.indexOf(card.model.geoprivacy))
                    onActivated: dialog.uploader.setGeoprivacy(card.cardId, currentText)
                }
            }

            // Vision status
            Text {
                Layout.fillWidth: true
                text: card.model.statusText
                visible: text !== ""
                wrapMode: Text.Wrap
                font.pixelSize: 12
                color: card.status === "error" ? dialog.errColor
                       : (card.status === "uploaded" ? dialog.okColor
                          : (card.status === "identified" ? "#9ccc65" : dialog.mutedText))
            }
            Text {
                Layout.fillWidth: true
                visible: card.model.gateMessage !== ""
                text: card.model.gateMessage
                color: dialog.warnColor
                font.pixelSize: 12
            }
            RowLayout {
                Layout.fillWidth: true
                visible: card.model.visionWarning !== ""
                spacing: 10
                Text {
                    Layout.fillWidth: true
                    text: "⚠ " + card.model.visionWarning
                    color: dialog.warnColor
                    font.pixelSize: 12
                    font.bold: true
                    wrapMode: Text.Wrap
                }
                Button {
                    text: "Split by Vision"
                    visible: card.model.canSplitByVision
                    enabled: dialog.uploader && !dialog.uploader.busy
                    onClicked: dialog.uploader.splitByVision(card.cardId)
                    ToolTip.visible: hovered
                    ToolTip.text: "Put the photos Vision scores as a different genus into their own observation"
                }
            }

            // Vision choices
            Flow {
                Layout.fillWidth: true
                spacing: 6
                visible: card.model.choices.length > 0 && card.status !== "uploaded"
                Repeater {
                    model: card.model.choices
                    delegate: Rectangle {
                        id: chip
                        required property var modelData
                        readonly property bool chosen: chip.modelData.name === card.model.chosenName
                        height: 26
                        width: chipText.implicitWidth + 18
                        radius: 13
                        color: chosen ? dialog.accentColor : (chipMouse.containsMouse ? "#3a3a3a" : "#2e2e2e")
                        border.color: chip.modelData.confident ? "#9ccc65" : "#444444"
                        border.width: chip.modelData.confident ? 2 : 1
                        Text {
                            id: chipText
                            anchors.centerIn: parent
                            text: chip.modelData.name + "  " + chip.modelData.confidencePct + "%"
                                  + (chip.modelData.rank !== "species" ? "  (" + chip.modelData.rank + ")" : "")
                            color: "white"
                            font.pixelSize: 12
                            font.italic: chip.modelData.rank === "species" || chip.modelData.rank === "genus"
                        }
                        MouseArea {
                            id: chipMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            onClicked: dialog.uploader.setChosenName(card.cardId, chip.modelData.name, chip.modelData.rank)
                        }
                        ToolTip.visible: chipMouse.containsMouse && chip.modelData.records > 0
                        ToolTip.text: chip.modelData.records + " DNA-verified reference records"
                    }
                }
            }

            // Name, taxon match, description
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Text { text: "Name"; color: dialog.mutedText; font.pixelSize: 12 }
                TextField {
                    id: nameField
                    Layout.preferredWidth: 320
                    text: card.model.chosenName
                    placeholderText: "Scientific name (or leave blank)"
                    enabled: card.status !== "uploading" && card.status !== "uploaded"
                    font.italic: true
                    onEditingFinished: if (text !== card.model.chosenName) dialog.uploader.setChosenName(card.cardId, text, "")
                }
                Text {
                    text: card.model.taxonLabel ? "✓ iNat: " + card.model.taxonLabel
                          : (card.model.chosenName ? "no exact iNat match yet — sent as a guess" : "")
                    color: card.model.taxonLabel ? "#9ccc65" : dialog.mutedText
                    font.pixelSize: 12
                    elide: Text.ElideRight
                    Layout.maximumWidth: 320
                }
                Item { Layout.fillWidth: true }
                Text {
                    visible: card.model.obsUrl !== ""
                    text: "<a href=\"" + card.model.obsUrl + "\">" + card.model.obsUrl + "</a>"
                    color: dialog.okColor
                    linkColor: dialog.okColor
                    font.pixelSize: 12
                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Text { text: "Notes"; color: dialog.mutedText; font.pixelSize: 12 }
                TextField {
                    Layout.fillWidth: true
                    text: card.model.description
                    placeholderText: "Description for iNaturalist (habitat, substrate, smell…)"
                    enabled: card.status !== "uploading" && card.status !== "uploaded"
                    onEditingFinished: dialog.uploader.setDescription(card.cardId, text)
                }
            }
            Repeater {
                model: card.model.hints
                delegate: Text {
                    required property string modelData
                    text: "Vision hint: " + modelData
                    color: "#888888"
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
            }
        }
    }
}
