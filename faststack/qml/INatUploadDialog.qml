pragma ComponentBehavior: Bound
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15

// Actions -> iNaturalist upload. Groups the batch into observation cards,
// identifies each with MycoMap Vision, lets the user fix the grouping
// (split / merge / drag photos between cards, several at once after a Ctrl or
// Shift click), order the photos inside a card by dragging, look at one full
// screen, pick a location on a map that shows the other observations, and
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

    // Photos picked with Ctrl+click (one at a time) or Shift+click (a range);
    // dragging one of them takes them all.
    property var selectedPhotos: ({})   // photo key -> true
    property int selectedPhotoCount: 0
    property string selectionAnchor: ""  // where a Shift+click range starts
    function setPhotoSelection(keys, anchor) {
        var picked = {}
        for (var i = 0; i < keys.length; i++) picked[keys[i]] = true
        selectedPhotos = picked
        selectedPhotoCount = Object.keys(picked).length
        selectionAnchor = anchor
    }
    function clearPhotoSelection() { if (selectedPhotoCount > 0 || selectionAnchor !== "") setPhotoSelection([], "") }
    // Every photo that can still be moved, in the order the cards show them.
    function movablePhotoKeys() {
        var keys = []
        for (var i = 0; i < cardRepeater.count; i++) {
            var item = cardRepeater.itemAt(i)
            if (!item || item.model.locked) continue
            var photos = item.model.photos
            for (var j = 0; j < photos.length; j++) keys.push(photos[j].key)
        }
        return keys
    }
    function togglePhoto(key) {
        var keys = Object.keys(selectedPhotos).filter(function(k) { return k !== key })
        if (selectedPhotos[key] !== true) keys.push(key)
        setPhotoSelection(keys, key)
    }
    function selectPhotoRange(key) {
        var all = movablePhotoKeys()
        var from = all.indexOf(selectionAnchor)
        var to = all.indexOf(key)
        if (from < 0 || to < 0) { setPhotoSelection([key], key); return }
        setPhotoSelection(all.slice(Math.min(from, to), Math.max(from, to) + 1), selectionAnchor)
    }

    onVisibleChanged: {
        if (visible) {
            clearPhotoSelection()
            if (controllerRef) controllerRef.dialog_opened("qml-inat-upload")
            if (uploader && !(uploader.hasInatToken && uploader.hasVisionKey)) settingsPopup.open()
            if (uploader) uploader.checkToken()
        } else {
            photoViewer.close()
            if (uploader) uploader.releasePhotoCache()
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

    // Upload, after asking about a token in its last minutes and about
    // observations that would go up without a location.
    function startUpload(tokenChecked, locationChecked) {
        if (!uploader) return
        if (uploader.allUploaded) { dialog.close(); return }
        var left = uploader.tokenSecondsLeft
        if (!tokenChecked && left > 0 && left <= uploader.tokenWarnSeconds) {
            tokenSoonDialog.minutes = Math.max(1, Math.floor(left / 60))
            tokenSoonDialog.open()
            return
        }
        if (!locationChecked && uploader.cardsWithoutLocation > 0) {
            noLocationDialog.missing = uploader.cardsWithoutLocationList()
            noLocationDialog.open()
            return
        }
        uploader.uploadAll()
    }

    function toast(text) {
        toastLabel.text = text
        toastTimer.restart()
    }

    Connections {
        target: dialog.uploader
        function onShowMessage(text) { dialog.toast(text) }
        function onTokenRejected(text) {
            if (!dialog.visible) return
            dialog.toast(text)
            settingsPopup.open()
        }
        function onUploadCardFinished(cardId) { dialog.showCard(cardId) }
        function onUploadFailed(uploaded, failures) {
            uploadFailedDialog.uploaded = uploaded
            uploadFailedDialog.failures = failures
            uploadFailedDialog.open()
        }
    }

    // Scroll the card list so this card is at the top (the next one to upload
    // shows below it). Follows an upload as each observation finishes.
    function showCard(cardId) {
        var flick = scroller.contentItem
        for (var i = 0; i < cardRepeater.count; i++) {
            var item = cardRepeater.itemAt(i)
            if (!item || item.cardId !== cardId) continue
            var maxY = Math.max(0, flick.contentHeight - flick.height)
            followScroll.stop()
            followScroll.to = Math.max(0, Math.min(maxY, item.y - 8))
            followScroll.start()
            return
        }
    }
    // Run something that adds or removes cards (a drop, a split) and keep
    // `item` where it is on screen while the list settles around it. A card
    // above that vanishes or changes height would otherwise slide everything
    // under the mouse to somewhere else. Scrolling by the difference does
    // it; where the list can not scroll that far (its top or bottom is in
    // view) empty room is added there instead, which then closes smoothly,
    // so the cards glide to their new place rather than jump.
    property Item holdItem: null
    property real holdY: 0
    property bool holdBusy: false
    property real topExtra: 0
    property real bottomExtra: 0
    function holdInPlace(item, action) {
        settle.stop()
        wheelScroll.stop()
        holdItem = item
        holdY = item.mapToItem(scroller, 0, 0).y
        action()
        cardColumn.forceLayout()
        applyHold()
        holdTimer.restart()
    }
    function applyHold() {
        if (!holdItem || holdBusy) return
        holdBusy = true
        var flick = scroller.contentItem
        var wanted = flick.contentY + holdItem.mapToItem(scroller, 0, 0).y - holdY
        if (wanted < 0) {
            topExtra -= wanted
            wanted = 0
            cardColumn.forceLayout()
        } else {
            var maxY = Math.max(0, flick.contentHeight - flick.height)
            if (wanted > maxY) {
                bottomExtra += wanted - maxY
                cardColumn.forceLayout()
            }
        }
        if (Math.abs(wanted - flick.contentY) > 0.5) flick.contentY = wanted
        holdBusy = false
    }
    // As the room at the bottom closes, the view follows the end of the list.
    onBottomExtraChanged: {
        if (holdItem) return
        var flick = scroller.contentItem
        cardColumn.forceLayout()
        var maxY = Math.max(0, flick.contentHeight - flick.height)
        if (flick.contentY > maxY) flick.contentY = maxY
    }
    // Cards finish resizing over the next frame or two; hold until then.
    Timer {
        id: holdTimer
        interval: 120
        onTriggered: {
            dialog.applyHold()
            dialog.holdItem = null
            if (dialog.topExtra > 0 || dialog.bottomExtra > 0) settle.start()
        }
    }
    ParallelAnimation {
        id: settle
        NumberAnimation { target: dialog; property: "topExtra"; to: 0; duration: 260; easing.type: Easing.InOutQuad }
        NumberAnimation { target: dialog; property: "bottomExtra"; to: 0; duration: 260; easing.type: Easing.InOutQuad }
    }

    // How far one notch of the mouse wheel moves the card list.
    readonly property int wheelStep: 260
    NumberAnimation {
        id: wheelScroll
        target: scroller.contentItem
        property: "contentY"
        duration: 110
        easing.type: Easing.OutCubic
    }
    NumberAnimation {
        id: followScroll
        target: scroller.contentItem
        property: "contentY"
        duration: 250
        easing.type: Easing.OutCubic
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
        property string photoKey: ""       // the photo under the mouse
        property var photoKeys: []          // everything that moves with it
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
        Rectangle {
            visible: dragGhost.photoKeys.length > 1
            anchors.right: parent.right; anchors.top: parent.top
            anchors.margins: -8
            width: Math.max(24, ghostCount.implicitWidth + 12); height: 24
            radius: 12
            color: dialog.accentColor
            Text {
                id: ghostCount
                anchors.centerIn: parent
                text: dragGhost.photoKeys.length
                color: "white"; font.pixelSize: 13; font.bold: true
            }
        }
    }

    // While a photo is dragged, holding it near the top or bottom edge of the
    // card list scrolls the list, so it can be taken to a card that is off
    // screen. The closer to the edge, the faster.
    Timer {
        interval: 16
        repeat: true
        running: dragGhost.dragging
        onTriggered: {
            var flick = scroller.contentItem
            var y = dragGhost.mapToItem(scroller, dragGhost.width / 2, dragGhost.height / 2).y
            var zone = 70
            var speed = 0
            if (y < zone) speed = -Math.min(1, (zone - y) / zone)
            else if (y > scroller.height - zone) speed = Math.min(1, (y - (scroller.height - zone)) / zone)
            if (speed === 0) return
            var limit = Math.max(0, flick.contentHeight - flick.height)
            flick.contentY = Math.max(0, Math.min(limit, flick.contentY + speed * 28))
        }
    }
    // The list moved under a photo that is being held still (edge scrolling
    // above, or the mouse wheel): no mouse move follows, so nudge the ghost
    // to make Qt look again at which card is under it.
    Connections {
        target: scroller.contentItem
        enabled: dragGhost.dragging
        property bool flip: false
        function onContentYChanged() {
            flip = !flip
            dragGhost.x += flip ? 0.5 : -0.5
        }
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
                readonly property bool tokenBad: dialog.uploader && dialog.uploader.hasInatToken && dialog.uploader.verifyError !== ""
                text: tokenBad ? "iNat: token not accepted"
                      : (dialog.uploader && dialog.uploader.inatLogin ? "iNat: " + dialog.uploader.inatLogin : "iNat: not signed in")
                color: !tokenBad && dialog.uploader && dialog.uploader.inatLogin ? dialog.okColor : dialog.warnColor
                font.pixelSize: 12
            }
            // Tokens last 24 hours: how long this one has left.
            Text {
                readonly property int secondsLeft: dialog.uploader ? dialog.uploader.tokenSecondsLeft : -1
                visible: text !== ""
                text: dialog.uploader ? dialog.uploader.tokenExpiryText : ""
                color: secondsLeft === 0 ? dialog.errColor
                       : (secondsLeft > 0 && secondsLeft <= (dialog.uploader ? dialog.uploader.tokenWarnSeconds : 0) ? dialog.warnColor : dialog.mutedText)
                font.pixelSize: 12
                font.bold: secondsLeft >= 0 && secondsLeft <= (dialog.uploader ? dialog.uploader.tokenWarnSeconds : 0)
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
            Text {
                visible: dialog.selectedPhotoCount > 0
                text: dialog.selectedPhotoCount + (dialog.selectedPhotoCount === 1 ? " photo" : " photos") + " selected · drag one to move them all"
                color: dialog.accentColor
                font.pixelSize: 12
            }
            Button {
                visible: dialog.selectedPhotoCount > 0
                flat: true
                text: "Clear selection"
                onClicked: dialog.clearPhotoSelection()
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
                onClicked: dialog.startUpload(false, false)
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

        // Observations already on iNaturalist (phone observations, earlier
        // uploads) and what the camera clock looks like next to them.
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 4
            visible: dialog.uploader !== null
                     && (dialog.uploader.existingStatus !== "" || dialog.uploader.clockNotes.length > 0)
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Text {
                    Layout.fillWidth: true
                    text: dialog.uploader ? dialog.uploader.existingStatus : ""
                    color: dialog.mutedText
                    font.pixelSize: 12
                    elide: Text.ElideRight
                }
                Button {
                    flat: true
                    visible: dialog.uploader && dialog.uploader.dismissedCount > 0
                    text: "Show " + (dialog.uploader ? dialog.uploader.dismissedCount : 0) + " dismissed"
                    onClicked: dialog.uploader.restoreDismissed()
                    ToolTip.visible: hovered
                    ToolTip.text: "Offer again the observations you closed or marked \"Not this one\"."
                }
                Button {
                    flat: true
                    text: "Check again"
                    visible: dialog.uploader && !dialog.uploader.existingBusy && dialog.uploader.inatLogin !== ""
                    onClicked: dialog.uploader.checkExisting()
                    ToolTip.visible: hovered
                    ToolTip.text: "Ask iNaturalist again for your observations on these dates (say, after making one on the phone)."
                }
            }
            Repeater {
                model: dialog.uploader ? dialog.uploader.clockNotes : []
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: clockRow.implicitHeight + 10
                    radius: 6
                    color: "#3a3020"
                    border.color: dialog.warnColor
                    border.width: 1
                    RowLayout {
                        id: clockRow
                        anchors.left: parent.left; anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.leftMargin: 10; anchors.rightMargin: 6
                        spacing: 8
                        Text {
                            Layout.fillWidth: true
                            text: "⏱ " + modelData.text
                            color: "white"
                            font.pixelSize: 12
                            wrapMode: Text.Wrap
                        }
                        Button {
                            text: "Use it"
                            visible: modelData.source === "detected"
                            onClicked: dialog.uploader.useClockOffset(modelData.camera)
                            ToolTip.visible: hovered
                            ToolTip.text: "Correct the time of new observations from this camera, and remember the offset for its photos of the next few weeks."
                        }
                        Button {
                            flat: true
                            text: "Ignore"
                            visible: modelData.source === "detected"
                            onClicked: dialog.uploader.ignoreClockOffset(modelData.camera)
                        }
                        Button {
                            flat: true
                            text: "Camera clock was right"
                            visible: modelData.source !== "detected"
                            onClicked: dialog.uploader.clockWasRight(modelData.camera)
                            ToolTip.visible: hovered
                            ToolTip.text: "Send this camera's times as they are, and remember that for these dates."
                        }
                    }
                }
            }
        }

        // Upload progress: one segment per observation in the run, filled in
        // the order they finish - green went up, red did not.
        RowLayout {
            Layout.fillWidth: true
            visible: dialog.uploader && dialog.uploader.uploadTotal > 0
            spacing: 10
            Rectangle {
                id: uploadBar
                objectName: "uploadBar"
                Layout.fillWidth: true
                implicitHeight: 14
                radius: 4
                color: "#1c1c1c"
                border.color: dialog.cardBorder
                border.width: 1
                clip: true
                readonly property var results: dialog.uploader ? dialog.uploader.uploadResults : []
                readonly property int total: dialog.uploader ? dialog.uploader.uploadTotal : 0
                readonly property int failed: results.filter(function(ok) { return !ok }).length
                Repeater {
                    model: uploadBar.results
                    Rectangle {
                        required property var modelData
                        required property int index
                        readonly property real step: (uploadBar.width - 2) / Math.max(1, uploadBar.total)
                        x: 1 + index * step
                        y: 1
                        // A hairline gap between segments while they are wide enough to tell apart.
                        width: Math.max(1, step - (step >= 6 ? 1 : 0))
                        height: uploadBar.height - 2
                        color: modelData ? dialog.okColor : dialog.errColor
                    }
                }
            }
            Text {
                text: uploadBar.results.length + " / " + uploadBar.total
                      + (uploadBar.failed > 0 ? "  ·  " + uploadBar.failed + " failed" : "")
                color: uploadBar.failed > 0 ? dialog.errColor : dialog.okColor
                font.pixelSize: 12
                font.bold: true
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
                topPadding: dialog.topExtra
                bottomPadding: dialog.bottomExtra
                onPositioningComplete: dialog.applyHold()

                // The mouse wheel moves the list a good part of a card per
                // notch; the Flickable's own step is a few text lines, which
                // crawls past cards this tall. A touchpad keeps its own pace.
                WheelHandler {
                    acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                    onWheel: function(event) {
                        var flick = scroller.contentItem
                        var maxY = Math.max(0, flick.contentHeight - flick.height)
                        var touchpad = event.device.type === PointerDevice.TouchPad && event.pixelDelta.y !== 0
                        if (touchpad) {
                            wheelScroll.stop()
                            flick.contentY = Math.max(0, Math.min(maxY, flick.contentY - event.pixelDelta.y))
                            return
                        }
                        if (event.angleDelta.y === 0) return
                        // Notches turned while the last one is still easing add up.
                        var from = wheelScroll.running ? wheelScroll.to : flick.contentY
                        wheelScroll.stop()
                        wheelScroll.to = Math.max(0, Math.min(maxY, from - event.angleDelta.y / 120 * dialog.wheelStep))
                        wheelScroll.start()
                    }
                }

                Repeater {
                    id: cardRepeater
                    model: dialog.uploader ? dialog.uploader.cards : null
                    delegate: ObservationCard { width: cardColumn.width }
                }

                // Drop here to start a new observation
                Rectangle {
                    id: newStrip
                    width: parent.width
                    height: 64
                    radius: 8
                    color: newDrop.containsDrag ? "#2f2f4a" : "transparent"
                    border.color: newDrop.containsDrag ? dialog.accentColor : dialog.cardBorder
                    border.width: 1
                    visible: dialog.uploader && dialog.uploader.photoCount > 1
                    Text {
                        anchors.centerIn: parent
                        text: "Drop photos here to make them a separate observation"
                        color: dialog.mutedText
                        font.pixelSize: 13
                    }
                    DropArea {
                        id: newDrop
                        anchors.fill: parent
                        keys: ["inat-photo"]
                        onDropped: function(drop) {
                            var keys = dragGhost.photoKeys
                            dialog.holdInPlace(newStrip, function() { dialog.uploader.movePhotos(keys, -1, "", false) })
                            dialog.clearPhotoSelection()
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
                text: "Click a photo for full screen · Ctrl+click or Shift+click to select several · drag to reorder or move to another card, landing where you drop"
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
                ToolTip.text: "MycoMap Vision identify calls. Each card identified (or re-identified) is one request. The second figure is what MycoMap reports for your API key over its whole billing period, from everything that has used the key, not just these photos."
                MouseArea { id: counterHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
            }
        }
    }

    // ---------------------------------------------------------------
    // Full-screen photo viewer: click a thumbnail, arrow keys flip through
    // the photos of that observation, Esc or a click goes back.
    FocusScope {
        id: photoViewer
        anchors.fill: parent
        z: 2000
        visible: false
        property var photos: []
        property int current: 0
        property int visibilityBefore: Window.Windowed
        readonly property var photo: visible && current >= 0 && current < photos.length ? photos[current] : null
        // Longest edge to decode: the screen's, rounded up so a small change
        // in window size does not mean a different (uncached) image.
        readonly property int decodeWidth: Math.min(4096, Math.ceil(Math.max(Screen.width, Screen.height)
                                                                   * Screen.devicePixelRatio / 512) * 512)
        function fullSource(i) {
            if (!visible || photos.length === 0) return ""
            var n = photos.length
            return photos[((i % n) + n) % n].full + "?w=" + decodeWidth
        }
        function openFor(cardPhotos, index) {
            photos = cardPhotos
            current = index
            visibilityBefore = dialog.visibility
            visible = true
            dialog.showFullScreen()
            forceActiveFocus()
        }
        function close() {
            if (!visible) return
            visible = false
            photos = []
            // Not when the window itself is going away: that would show it again.
            if (dialog.visible) dialog.visibility = visibilityBefore
        }
        function step(delta) {
            var n = photos.length
            if (n > 1) current = (current + delta + n) % n
        }

        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Left || event.key === Qt.Key_Up) step(-1)
            else if (event.key === Qt.Key_Right || event.key === Qt.Key_Down || event.key === Qt.Key_Space) step(1)
            else if (event.key === Qt.Key_Escape || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) close()
            event.accepted = true
        }

        Rectangle { anchors.fill: parent; color: "black" }
        // The card thumbnail (already loaded) stands in until the large one is decoded.
        Image {
            anchors.fill: parent
            source: photoViewer.photo ? photoViewer.photo.source : ""
            fillMode: Image.PreserveAspectFit
            visible: fullImage.status !== Image.Ready
        }
        Image {
            id: fullImage
            objectName: "viewerFullImage"
            anchors.fill: parent
            source: photoViewer.fullSource(photoViewer.current)
            fillMode: Image.PreserveAspectFit
            asynchronous: true
            visible: status === Image.Ready
        }
        // Decode the neighbours ahead of time so an arrow press shows at once.
        // The provider keeps what it has decoded, so going back is instant too.
        Repeater {
            model: [1, -1, 2, -2].slice(0, Math.max(0, Math.min(4, photoViewer.photos.length - 1)))
            Image {
                required property int modelData
                visible: false
                asynchronous: true
                // Only once the photo on screen is done, so it is never queued behind these.
                source: fullImage.status === Image.Ready ? photoViewer.fullSource(photoViewer.current + modelData) : ""
            }
        }
        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.AllButtons
            hoverEnabled: true   // nothing underneath reacts while the viewer is up
            onClicked: photoViewer.close()
            onWheel: function(wheel) { photoViewer.step(wheel.angleDelta.y > 0 ? -1 : 1) }
        }
        Rectangle {
            anchors.top: parent.top; anchors.right: parent.right
            anchors.margins: 14
            width: viewerNumber.implicitWidth + 18; height: viewerNumber.implicitHeight + 8
            radius: 5
            color: "#b0000000"
            Text {
                id: viewerNumber
                anchors.centerIn: parent
                text: (photoViewer.current + 1) + " / " + photoViewer.photos.length
                color: dialog.okColor
                font.pixelSize: 20
                font.bold: true
            }
        }
        Text {
            anchors.bottom: parent.bottom; anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottomMargin: 10
            text: (photoViewer.photo ? photoViewer.photo.name + "   ·   " : "") + "← → to flip · Esc or click to go back"
            color: "#999999"
            style: Text.Outline; styleColor: "black"
            font.pixelSize: 12
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

    // An upload run ended with observations that did not (fully) go up.
    Dialog {
        id: uploadFailedDialog
        objectName: "uploadFailedDialog"
        property int uploaded: 0
        property var failures: []
        title: failures.length + (failures.length === 1 ? " observation was" : " observations were") + " not uploaded"
        modal: true
        anchors.centerIn: parent
        width: Math.min(620, dialog.width - 40)
        closePolicy: Popup.CloseOnEscape
        background: Rectangle { color: "#2a2a2a"; radius: 8; border.color: dialog.errColor; border.width: 2 }
        contentItem: ColumnLayout {
            spacing: 10
            Text {
                Layout.fillWidth: true
                text: (uploadFailedDialog.uploaded > 0
                       ? uploadFailedDialog.uploaded + " uploaded successfully. "
                       : "Nothing was uploaded. ")
                      + "The ones below are outlined in red in the list and are still waiting: fix what is wrong, then press Upload again. Nothing that already went up is sent twice."
                color: dialog.textColor
                wrapMode: Text.Wrap
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(failureColumn.implicitHeight, 300)
                clip: true
                contentWidth: availableWidth
                Column {
                    id: failureColumn
                    width: parent.width
                    spacing: 8
                    Repeater {
                        model: uploadFailedDialog.failures
                        Text {
                            required property var modelData
                            width: failureColumn.width
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            color: dialog.errColor
                            text: "Observation " + modelData.number
                                  + (modelData.name ? " (" + modelData.name + ")" : "") + ": " + modelData.message
                        }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Button {
                    text: "Show the first one"
                    highlighted: true
                    onClicked: {
                        var first = uploadFailedDialog.failures[0]
                        uploadFailedDialog.close()
                        if (first) dialog.showCard(first.cardId)
                    }
                }
                Button { text: "OK"; onClicked: uploadFailedDialog.close() }
            }
        }
    }

    // Observations that would go up without a location: named, one click
    // away, so they can be placed on the map first.
    Dialog {
        id: noLocationDialog
        objectName: "noLocationDialog"
        property var missing: []
        title: missing.length + (missing.length === 1 ? " observation has" : " observations have") + " no location"
        modal: true
        anchors.centerIn: parent
        width: Math.min(620, dialog.width - 40)
        closePolicy: Popup.CloseOnEscape
        background: Rectangle { color: "#2a2a2a"; radius: 8; border.color: dialog.warnColor; border.width: 2 }
        contentItem: ColumnLayout {
            spacing: 10
            Text {
                Layout.fillWidth: true
                text: "Their photos have no GPS and no point was picked on the map, so iNaturalist would grade them Casual. "
                      + "They are outlined in orange in the list. Show one to place it with \"Pick on map\", or upload them as they are."
                color: dialog.textColor
                wrapMode: Text.Wrap
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(missingColumn.implicitHeight, 300)
                clip: true
                contentWidth: availableWidth
                Column {
                    id: missingColumn
                    width: parent.width
                    spacing: 4
                    Repeater {
                        model: noLocationDialog.missing
                        RowLayout {
                            required property var modelData
                            width: missingColumn.width
                            spacing: 8
                            Text {
                                Layout.fillWidth: true
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                color: dialog.warnColor
                                text: "Observation " + modelData.number
                                      + (modelData.name ? " (" + modelData.name + ")" : "")
                                      + " · " + modelData.photoCount + (modelData.photoCount === 1 ? " photo" : " photos")
                            }
                            Button {
                                text: "Show"
                                flat: true
                                onClicked: {
                                    noLocationDialog.close()
                                    dialog.showCard(modelData.cardId)
                                }
                            }
                        }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Button {
                    text: noLocationDialog.missing.length === 1 ? "Show it" : "Show the first"
                    highlighted: true
                    onClicked: {
                        var first = noLocationDialog.missing[0]
                        noLocationDialog.close()
                        if (first) dialog.showCard(first.cardId)
                    }
                }
                Button {
                    text: "Upload anyway"
                    onClicked: {
                        noLocationDialog.close()
                        dialog.startUpload(true, true)
                    }
                }
                Button { text: "Cancel"; flat: true; onClicked: noLocationDialog.close() }
            }
        }
    }

    // The token runs out in a few minutes: an upload would stop partway.
    Dialog {
        id: tokenSoonDialog
        objectName: "tokenSoonDialog"
        property int minutes: 0
        title: "iNaturalist token expires in " + minutes + " min"
        modal: true
        anchors.centerIn: parent
        width: Math.min(560, dialog.width - 40)
        closePolicy: Popup.CloseOnEscape
        background: Rectangle { color: "#2a2a2a"; radius: 8; border.color: dialog.warnColor; border.width: 2 }
        contentItem: ColumnLayout {
            spacing: 10
            Text {
                Layout.fillWidth: true
                text: "iNaturalist API tokens last 24 hours, and this one is nearly done. Observations still waiting when it runs out fail "
                      + "(saving a fresh token then uploads the rest, nothing twice). Get a fresh token first?"
                color: dialog.textColor
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Button {
                    text: "Get a fresh token"
                    highlighted: true
                    onClicked: {
                        tokenSoonDialog.close()
                        Qt.openUrlExternally(dialog.uploader.inatTokenUrl)
                        settingsPopup.open()
                    }
                }
                Button {
                    text: "Upload anyway"
                    onClicked: {
                        tokenSoonDialog.close()
                        dialog.startUpload(true, false)
                    }
                }
                Button { text: "Cancel"; flat: true; onClicked: tokenSoonDialog.close() }
            }
        }
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
        clip: true
        property int cardId: -1
        property real pendingLat: 0
        property real pendingLon: 0
        property bool hasPending: false
        property int accuracy: 25
        // The other observations of this upload that have a location.
        property var neighbours: []
        property string copiedNote: ""

        function setAccuracy(meters) {
            accuracy = Math.max(1, Math.min(1000000, Math.round(meters)))
            if (parseInt(accuracyField.text) !== accuracy) accuracyField.text = accuracy
        }

        function openFor(cardId, hasLocation, lat, lon, manualAccuracy) {
            mapPopup.cardId = cardId
            mapPopup.hasPending = false
            mapPopup.copiedNote = ""
            mapPopup.neighbours = dialog.uploader.mapNeighbours(cardId)
            // A point placed before keeps its accuracy; anything else starts at 25 m.
            setAccuracy(manualAccuracy > 0 ? manualAccuracy : 25)
            open()
            if (hasLocation) {
                mapPicker.setMarker(lat, lon)
                mapPicker.centerOn(lat, lon, 15)
            } else if (neighbours.length > 0) {
                // No fix of its own: start where the observation nearest to
                // it in the list is, which is usually close by.
                var near = neighbours[0]
                for (var i = 1; i < neighbours.length; i++)
                    if (neighbours[i].cardsAway < near.cardsAway) near = neighbours[i]
                mapPicker.hasMarker = false
                mapPicker.centerOn(near.lat, near.lon, 15)
            } else if (mapPicker.hasMarker) {
                mapPicker.centerOn(mapPicker.markerLat, mapPicker.markerLon, mapPicker.zoom)
            } else {
                mapPicker.centerOn(39.5, -98.35, 4)
            }
        }

        // A plain Item around the layout: it is given exactly the room the
        // popup has, so a row that wants more can not push the map and the
        // buttons out past the popup's edge.
        contentItem: Item {
        ColumnLayout {
            anchors.fill: parent
            spacing: 8
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Text {
                    text: "Click the map to place this observation"
                    color: dialog.textColor; font.pixelSize: 15; font.bold: true
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                    Layout.minimumWidth: 80
                }
                TextField {
                    id: coordField
                    placeholderText: "lat, lon"
                    Layout.preferredWidth: 220
                    Layout.minimumWidth: 140
                    selectByMouse: true
                    onAccepted: {
                        var parts = text.trim().split(/[ ,]+/)
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
            // Other observations on the map: the rest of this upload is
            // always shown; the user's own from iNaturalist on request.
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Button {
                    objectName: "myObservationsButton"
                    text: "Show my iNaturalist observations here"
                    enabled: dialog.uploader && !dialog.uploader.myObservationsBusy
                    onClicked: {
                        mapPopup.copiedNote = ""
                        var box = mapPicker.viewBounds()
                        dialog.uploader.fetchMyObservations(box.swlat, box.swlng, box.nelat, box.nelng)
                    }
                    ToolTip.visible: hovered
                    ToolTip.delay: 600
                    ToolTip.text: "Adds the observations you already have on iNaturalist inside the area the map shows now.\nZoom out or move the map and press again to bring in more."
                }
                Button {
                    flat: true
                    text: "Hide them"
                    visible: dialog.uploader && dialog.uploader.myObservations.length > 0
                    onClicked: dialog.uploader.clearMyObservations()
                }
                Text {
                    Layout.fillWidth: true
                    text: mapPopup.copiedNote !== "" ? mapPopup.copiedNote
                          : (dialog.uploader && dialog.uploader.myObservationsStatus !== "" ? dialog.uploader.myObservationsStatus
                             : (mapPopup.neighbours.length > 0
                                ? "The photos on the map are the other observations in this upload. Right-click one to copy its coordinates."
                                : ""))
                    color: dialog.mutedText
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                }
            }
            MapPicker {
                id: mapPicker
                objectName: "mapPicker"
                accuracyMeters: mapPopup.accuracy
                photoMarkers: mapPopup.neighbours.concat(dialog.uploader ? dialog.uploader.myObservations : [])
                Layout.fillWidth: true
                Layout.fillHeight: true
                onPicked: function(lat, lon) {
                    mapPopup.pendingLat = lat; mapPopup.pendingLon = lon; mapPopup.hasPending = true
                    coordField.text = lat.toFixed(5) + ", " + lon.toFixed(5)
                }
                onMarkerCopyRequested: function(marker) {
                    var coords = marker.lat.toFixed(6) + ", " + marker.lon.toFixed(6)
                    dialog.uploader.copyText(coords)
                    mapPopup.copiedNote = "Copied " + coords + " (" + marker.label + ")"
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 6
                Text { text: "Accuracy"; color: dialog.textColor }
                TextField {
                    id: accuracyField
                    objectName: "accuracyField"
                    Layout.preferredWidth: 80
                    horizontalAlignment: TextInput.AlignRight
                    validator: IntValidator { bottom: 1; top: 1000000 }
                    selectByMouse: true
                    onTextEdited: if (acceptableInput) mapPopup.accuracy = parseInt(text)
                    onEditingFinished: text = mapPopup.accuracy
                    ToolTip.visible: hovered
                    ToolTip.delay: 600
                    ToolTip.text: "How far the real spot could be from the marker, in metres.\nSent to iNaturalist as the observation's positional accuracy and drawn as the blue circle."
                }
                Text { text: "m"; color: dialog.mutedText }
                Repeater {
                    model: [10, 50, 100, 500, 1000]
                    Button {
                        required property int modelData
                        flat: true
                        implicitWidth: 68
                        text: modelData >= 1000 ? (modelData / 1000) + " km" : modelData + " m"
                        onClicked: mapPopup.setAccuracy(modelData)
                    }
                }
                Item { Layout.fillWidth: true }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Text {
                    Layout.fillWidth: true
                    text: mapPopup.hasPending ? mapPopup.pendingLat.toFixed(5) + ", " + mapPopup.pendingLon.toFixed(5) : "No point chosen"
                    color: dialog.mutedText
                    elide: Text.ElideRight
                }
                Button { text: "Cancel"; onClicked: mapPopup.close() }
                Button {
                    text: "Use this location"
                    highlighted: true
                    enabled: mapPopup.hasPending
                    onClicked: {
                        dialog.uploader.setLocation(mapPopup.cardId, mapPopup.pendingLat, mapPopup.pendingLon, mapPopup.accuracy)
                        mapPopup.close()
                    }
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
        // Its photos go to an observation already on iNaturalist: the name,
        // place and notes of that observation stay as they are.
        readonly property bool attached: model.attachedObs.obsId > 0

        implicitHeight: cardLayout.implicitHeight + 24
        radius: 8
        // A photo from another card is being held over this one.
        readonly property bool takingPhoto: cardDrop.containsDrag && dragGhost.sourceCardId !== cardId
        color: takingPhoto ? "#2a2a40" : dialog.cardColor
        // No GPS, no map point and going up as a new observation.
        readonly property bool needsLocation: !model.hasLocation && !attached && status !== "uploaded"
        border.width: model.selected || model.uploadError !== "" || needsLocation ? 2 : 1
        border.color: model.selected ? dialog.accentColor
                      : (takingPhoto ? dialog.accentColor
                         : (status === "error" ? dialog.errColor
                            : (needsLocation ? dialog.warnColor : dialog.cardBorder)))

        // This card's selected photos, unless that is all of them (nothing
        // would be left to split from): Split then splits exactly those off.
        readonly property var pickedKeys: {
            var picked = dialog.selectedPhotos
            var keys = []
            var photos = card.model.photos
            for (var i = 0; i < photos.length; i++)
                if (picked[photos[i].key] === true) keys.push(photos[i].key)
            return keys.length < photos.length ? keys : []
        }

        // Where the dragged photos would land in this card: next to the
        // photo they are held beside (dropKey, and on which side), or after
        // the last one when held anywhere else on a card they are not from.
        property string dropKey: ""
        property bool dropAfter: false
        function photoThumbs() {
            var thumbs = []
            for (var i = 0; i < photoFlow.children.length; i++)
                if (photoFlow.children[i].photoKey !== undefined) thumbs.push(photoFlow.children[i])
            return thumbs
        }
        function updateDropTarget(x, y) {
            var own = dragGhost.sourceCardId === cardId
            var thumbs = photoThumbs()
            var p = cardDrop.mapToItem(photoFlow, x, y)
            if (thumbs.length === 0) { dropKey = ""; return }
            if (p.y < -8 || p.y > photoFlow.height + 8) {
                // Not among the photos: last, or left alone in its own card.
                dropAfter = true
                dropKey = own ? "" : thumbs[thumbs.length - 1].photoKey
                return
            }
            var best = null
            var bestDistance = 0
            for (var i = 0; i < thumbs.length; i++) {
                var item = thumbs[i]
                var dx = p.x - (item.x + item.width / 2)
                var dy = p.y - (item.y + item.height / 2)
                var distance = dx * dx + 9 * dy * dy   // prefer the row under the pointer
                if (best === null || distance < bestDistance) { best = item; bestDistance = distance }
            }
            if (best.photoKey === dragGhost.photoKey) { dropKey = ""; return }
            dropAfter = p.x > best.x + best.width / 2
            dropKey = best.photoKey
        }

        DropArea {
            id: cardDrop
            anchors.fill: parent
            keys: ["inat-photo"]
            enabled: !card.model.locked
            onEntered: function(drag) { card.updateDropTarget(drag.x, drag.y) }
            onPositionChanged: function(drag) { card.updateDropTarget(drag.x, drag.y) }
            onExited: card.dropKey = ""
            onDropped: function(drop) {
                var target = card.dropKey
                card.dropKey = ""
                drop.accept()
                var keys = dragGhost.photoKeys
                if (target === "") {
                    // Released on its own photo or away from the photos: only
                    // photos selected in other cards have somewhere to go.
                    var mine = card.photoThumbs().map(function(t) { return t.photoKey })
                    if (keys.every(function(k) { return mine.indexOf(k) >= 0 })) return
                }
                var after = card.dropAfter
                dialog.holdInPlace(card, function() { dialog.uploader.movePhotos(keys, card.cardId, target, after) })
                dialog.clearPhotoSelection()
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
                Text { text: "Split into"; color: dialog.mutedText; font.pixelSize: 12; visible: card.model.canSplit && card.pickedKeys.length === 0 }
                ComboBox {
                    id: splitCount
                    visible: card.model.canSplit && card.pickedKeys.length === 0
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
                    text: card.pickedKeys.length > 0 ? "Split off " + card.pickedKeys.length + " selected" : "Split"
                    visible: card.model.canSplit
                    enabled: dialog.uploader && !dialog.uploader.busy
                    onClicked: {
                        if (card.pickedKeys.length > 0) {
                            var picked = card.pickedKeys
                            dialog.holdInPlace(card, function() { dialog.uploader.splitOffPhotos(card.cardId, picked) })
                            dialog.clearPhotoSelection()
                        } else {
                            dialog.uploader.splitCard(card.cardId, parseInt(splitCount.currentText) || 2)
                        }
                    }
                    ToolTip.visible: hovered
                    ToolTip.text: card.pickedKeys.length > 0
                                  ? "Give the selected photos of this card an observation of their own."
                                  : "Grouped too aggressively? Choose how many observations are in this card and split it by time and GPS.\nOr select photos first (Ctrl+click, Shift+click) to split exactly those off."
                }
                Button {
                    text: card.status === "identifying" ? "Identifying…" : (card.status === "identified" ? "Re-identify" : "Identify")
                    visible: card.status !== "uploading" && card.status !== "uploaded" && !card.attached
                    enabled: card.status !== "identifying"
                    onClicked: dialog.uploader.identifyCard(card.cardId)
                }
            }

            // Photos
            Flow {
                id: photoFlow
                Layout.fillWidth: true
                spacing: 8
                Repeater {
                    model: card.model.photos
                    delegate: Item {
                        id: thumb
                        required property var modelData
                        required property int index
                        readonly property string photoKey: modelData.key
                        readonly property bool picked: dialog.selectedPhotos[photoKey] === true
                        width: 220; height: 180
                        // Where the dragged photos would be inserted.
                        Rectangle {
                            visible: card.dropKey === thumb.photoKey
                            x: card.dropAfter ? thumb.width + 2 : -6
                            width: 4; height: thumb.height
                            radius: 2
                            color: dialog.accentColor
                        }
                        Rectangle {
                            anchors.fill: parent
                            color: "#151515"
                            radius: 4
                            border.color: thumb.picked || thumbArea.containsMouse ? dialog.accentColor : "#333333"
                            border.width: thumb.picked ? 3 : 1
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
                            // Selected (Ctrl+click / Shift+click): moves with whichever one is dragged.
                            Rectangle {
                                visible: thumb.picked
                                anchors.top: parent.top; anchors.left: parent.left
                                anchors.margins: 4
                                width: 22; height: 22; radius: 11
                                color: dialog.accentColor
                                border.color: "white"; border.width: 1
                                Text { anchors.centerIn: parent; text: "✓"; color: "white"; font.pixelSize: 13; font.bold: true }
                            }
                            // Place in the observation; the full-screen view shows the same number.
                            Rectangle {
                                anchors.top: parent.top; anchors.right: parent.right
                                anchors.margins: 4
                                width: Math.max(20, thumbNumber.implicitWidth + 10); height: 20
                                radius: 4
                                color: "#b0000000"
                                Text {
                                    id: thumbNumber
                                    anchors.centerIn: parent
                                    text: thumb.index + 1
                                    color: dialog.okColor
                                    font.pixelSize: 13
                                    font.bold: true
                                }
                            }
                        }
                        MouseArea {
                            id: thumbArea
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: card.model.locked ? Qt.PointingHandCursor : Qt.OpenHandCursor
                            // Photos of an uploaded card can still be looked at, not moved.
                            drag.target: card.model.locked ? null : dragGhost
                            drag.threshold: 6
                            onClicked: function(mouse) {
                                if (card.model.locked) photoViewer.openFor(card.model.photos, thumb.index)
                                else if (mouse.modifiers & Qt.ShiftModifier) dialog.selectPhotoRange(thumb.photoKey)
                                else if (mouse.modifiers & Qt.ControlModifier) dialog.togglePhoto(thumb.photoKey)
                                else photoViewer.openFor(card.model.photos, thumb.index)
                            }
                            onPressed: function(mouse) {
                                var p = thumbArea.mapToItem(dialog.contentItem, mouse.x, mouse.y)
                                dragGhost.x = p.x - dragGhost.width / 2
                                dragGhost.y = p.y - dragGhost.height / 2
                                dragGhost.photoKey = thumb.modelData.key
                                dragGhost.sourceCardId = card.cardId
                                ghostImage.source = thumb.modelData.source
                            }
                            drag.onActiveChanged: {
                                if (!thumbArea.drag.active) return
                                // A selected photo takes the rest of the selection with it;
                                // any other photo goes alone.
                                if (thumb.picked && dialog.selectedPhotoCount > 1) {
                                    var picked = dialog.selectedPhotos
                                    dragGhost.photoKeys = dialog.movablePhotoKeys().filter(function(k) { return picked[k] === true })
                                } else {
                                    dialog.clearPhotoSelection()
                                    dragGhost.photoKeys = [thumb.photoKey]
                                }
                                dragGhost.dragging = true
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

            // Already on iNaturalist? A phone observation of this find, or
            // an earlier upload of some of its photos.
            Rectangle {
                Layout.fillWidth: true
                visible: card.attached || (card.model.existingMatches.length > 0 && !card.model.locked)
                implicitHeight: existingColumn.implicitHeight + 16
                radius: 6
                color: card.attached ? "#233823" : "#222c45"
                border.color: card.attached ? dialog.okColor : "#5c7cfa"
                border.width: 1
                ColumnLayout {
                    id: existingColumn
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                    anchors.margins: 8
                    spacing: 6
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Text {
                            Layout.fillWidth: true
                            text: card.attached
                                  ? (card.model.locked
                                     ? "These photos went to an observation that was already on iNaturalist"
                                     : "✓ Upload adds these photos to this observation instead of making a new one")
                                  : (card.model.existingMatches.length === 1
                                     ? "This find may already be on iNaturalist:"
                                     : "This find may already be on iNaturalist. One of these?")
                            color: "white"
                            font.pixelSize: 13
                            font.bold: true
                            wrapMode: Text.Wrap
                        }
                        ToolButton {
                            visible: !card.attached
                            text: "✕"
                            font.pixelSize: 14
                            implicitWidth: 30; implicitHeight: 30
                            onClicked: dialog.uploader.dismissAllMatches(card.cardId)
                            ToolTip.visible: hovered
                            ToolTip.text: card.model.existingMatches.length === 1
                                          ? "Not this one: close the box (the photos become a new observation)"
                                          : "None of these: close the box (the photos become a new observation)"
                        }
                    }
                    Repeater {
                        model: card.attached ? [card.model.attachedObs] : card.model.existingMatches
                        delegate: RowLayout {
                            id: matchRow
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 10
                            Rectangle {
                                implicitWidth: 72; implicitHeight: 72
                                radius: 4
                                color: "#151515"
                                border.color: "#333333"
                                Image {
                                    anchors.fill: parent
                                    anchors.margins: 2
                                    source: matchRow.modelData.thumb
                                    fillMode: Image.PreserveAspectCrop
                                    asynchronous: true
                                    cache: true
                                }
                                Text {
                                    anchors.centerIn: parent
                                    visible: matchRow.modelData.thumb === ""
                                    text: "no photo"
                                    color: dialog.mutedText
                                    font.pixelSize: 10
                                }
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                Text {
                                    Layout.fillWidth: true
                                    text: matchRow.modelData.name + "  ·  " + matchRow.modelData.when + "  ·  "
                                          + matchRow.modelData.photoCount + (matchRow.modelData.photoCount === 1 ? " photo" : " photos")
                                    color: "white"
                                    font.pixelSize: 13
                                    elide: Text.ElideRight
                                }
                                Text {
                                    Layout.fillWidth: true
                                    visible: matchRow.modelData.detail !== ""
                                    text: matchRow.modelData.detail
                                    color: "#c5cae9"
                                    font.pixelSize: 12
                                    wrapMode: Text.Wrap
                                }
                                Text {
                                    text: "<a href=\"" + matchRow.modelData.url + "\">Observation " + matchRow.modelData.obsId + " on iNaturalist</a>"
                                    color: dialog.okColor
                                    linkColor: "#90caf9"
                                    font.pixelSize: 12
                                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                                }
                            }
                            Button {
                                text: "Not this one"
                                flat: true
                                visible: !card.attached && card.model.existingMatches.length > 1
                                onClicked: dialog.uploader.dismissMatch(card.cardId, matchRow.modelData.obsId)
                            }
                            Button {
                                text: "Add these photos to it"
                                visible: !card.attached
                                highlighted: true
                                enabled: dialog.uploader && !dialog.uploader.uploading
                                onClicked: dialog.uploader.attachToExisting(card.cardId, matchRow.modelData.obsId)
                                ToolTip.visible: hovered
                                ToolTip.text: "Upload attaches these photos to this observation; no new observation is made. Its time also tells FastStack how far off the camera clock is."
                            }
                            Button {
                                text: "Make a new observation instead"
                                flat: true
                                visible: card.attached && !card.model.locked
                                onClicked: dialog.uploader.detachFromExisting(card.cardId)
                            }
                        }
                    }
                    // The order on iNaturalist: its photos and these, one
                    // row. Drag a photo to where it should go.
                    Text {
                        visible: orderFlow.visible
                        text: card.model.locked
                              ? "Order on iNaturalist"
                              : "Order on iNaturalist: drag a photo to move it. Blue frames are the photos this upload adds."
                        color: "#c5cae9"
                        font.pixelSize: 12
                    }
                    Flow {
                        id: orderFlow
                        Layout.fillWidth: true
                        spacing: 6
                        visible: card.attached && card.model.attachedObs.order !== undefined
                                 && card.model.attachedObs.order.length > 0
                        readonly property var items: card.attached && card.model.attachedObs.order !== undefined
                                                     ? card.model.attachedObs.order : []
                        property int dragFrom: -1
                        property int dropAt: -1   // insert before this index (length = last)
                        // Where a tile held with its centre at (x, y) would go.
                        function dropIndex(x, y) {
                            var best = -1, bestDistance = 0, after = false
                            for (var i = 0; i < orderRepeater.count; i++) {
                                var item = orderRepeater.itemAt(i)
                                if (!item) continue
                                var dx = x - (item.x + item.width / 2)
                                var dy = y - (item.y + item.height / 2)
                                var distance = dx * dx + 9 * dy * dy
                                if (best < 0 || distance < bestDistance) { best = i; bestDistance = distance; after = dx > 0 }
                            }
                            return best < 0 ? -1 : best + (after ? 1 : 0)
                        }
                        Repeater {
                            id: orderRepeater
                            model: orderFlow.items
                            delegate: Item {
                                id: orderItem
                                required property var modelData
                                required property int index
                                width: 96; height: 96
                                z: orderMouse.drag.active ? 10 : 0
                                // Insertion bar: the dragged photo lands here.
                                Rectangle {
                                    visible: orderFlow.dragFrom >= 0 && orderFlow.dropAt === orderItem.index
                                             && orderFlow.dropAt !== orderFlow.dragFrom && orderFlow.dropAt !== orderFlow.dragFrom + 1
                                    x: -5; width: 4; height: parent.height; radius: 2
                                    color: dialog.accentColor
                                }
                                Rectangle {
                                    visible: orderFlow.dragFrom >= 0 && orderItem.index === orderFlow.items.length - 1
                                             && orderFlow.dropAt === orderFlow.items.length && orderFlow.dragFrom !== orderItem.index
                                    x: parent.width + 1; width: 4; height: parent.height; radius: 2
                                    color: dialog.accentColor
                                }
                                Rectangle {
                                    id: orderTile
                                    objectName: "orderTile"
                                    width: 96; height: 96
                                    radius: 4
                                    color: "#151515"
                                    border.color: orderItem.modelData.isNew ? dialog.accentColor : "#555555"
                                    border.width: orderItem.modelData.isNew ? 3 : 1
                                    opacity: orderMouse.drag.active ? 0.85 : 1.0
                                    Image {
                                        anchors.fill: parent
                                        anchors.margins: 3
                                        source: orderItem.modelData.thumb
                                        sourceSize.width: 192
                                        fillMode: Image.PreserveAspectCrop
                                        asynchronous: true
                                        cache: true
                                    }
                                    Rectangle {
                                        anchors.top: parent.top; anchors.left: parent.left
                                        anchors.margins: 4
                                        width: Math.max(18, orderNumber.implicitWidth + 8); height: 18
                                        radius: 4
                                        color: "#b0000000"
                                        Text {
                                            id: orderNumber
                                            anchors.centerIn: parent
                                            text: orderItem.index + 1
                                            color: orderItem.modelData.isNew ? "#c5cae9" : dialog.okColor
                                            font.pixelSize: 11
                                            font.bold: true
                                        }
                                    }
                                    Rectangle {
                                        anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right
                                        anchors.margins: 3
                                        height: 16
                                        color: "#b0000000"
                                        Text {
                                            anchors.centerIn: parent
                                            text: orderItem.modelData.isNew ? "new" : "on iNat"
                                            color: "white"
                                            font.pixelSize: 10
                                        }
                                    }
                                    MouseArea {
                                        id: orderMouse
                                        anchors.fill: parent
                                        enabled: !card.model.locked
                                        cursorShape: enabled ? Qt.OpenHandCursor : Qt.ArrowCursor
                                        drag.target: orderTile
                                        drag.threshold: 6
                                        drag.onActiveChanged: {
                                            if (orderMouse.drag.active) {
                                                orderFlow.dragFrom = orderItem.index
                                                orderFlow.dropAt = orderItem.index
                                            }
                                        }
                                        onPositionChanged: {
                                            if (!orderMouse.drag.active) return
                                            var c = orderTile.mapToItem(orderFlow, orderTile.width / 2, orderTile.height / 2)
                                            orderFlow.dropAt = orderFlow.dropIndex(c.x, c.y)
                                        }
                                        onReleased: {
                                            var from = orderFlow.dragFrom
                                            var to = orderFlow.dropAt
                                            orderFlow.dragFrom = -1
                                            orderFlow.dropAt = -1
                                            orderTile.x = 0
                                            orderTile.y = 0
                                            if (from >= 0 && to >= 0 && to !== from && to !== from + 1)
                                                dialog.uploader.moveInObservation(card.cardId, from, to)
                                        }
                                        onCanceled: {
                                            orderFlow.dragFrom = -1
                                            orderFlow.dropAt = -1
                                            orderTile.x = 0
                                            orderTile.y = 0
                                        }
                                    }
                                }
                            }
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
                             + (card.model.locationSource === "manual" ? "  (chosen on map, ±" + card.model.accuracyMeters + " m)"
                                : "  (EXIF, " + card.model.gpsCount + " fix" + (card.model.gpsCount === 1 ? "" : "es")
                                  + (card.model.gpsCount > 1 ? ", spread " + card.model.spreadMeters + " m" : "") + ")"))
                          : "No GPS in these photos"
                    color: card.model.hasLocation ? dialog.textColor : dialog.warnColor
                    font.pixelSize: 12
                }
                ToolButton {
                    visible: card.model.hasLocation
                    text: "Copy"
                    font.pixelSize: 12
                    implicitHeight: 26
                    onClicked: {
                        var coords = card.model.lat.toFixed(6) + ", " + card.model.lon.toFixed(6)
                        dialog.uploader.copyText(coords)
                        dialog.toast("Copied " + coords)
                    }
                    ToolTip.visible: hovered
                    ToolTip.delay: 600
                    ToolTip.text: "Copy these coordinates. Paste them into the lat, lon box of another observation's map to put it at the same spot."
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
                    visible: card.status !== "uploading" && card.status !== "uploaded" && !card.attached
                    onClicked: mapPopup.openFor(card.cardId, card.model.hasLocation, card.model.lat, card.model.lon,
                                                card.model.locationSource === "manual" ? card.model.accuracyMeters : 0)
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
                    enabled: card.status !== "uploading" && card.status !== "uploaded" && !card.attached
                    currentIndex: Math.max(0, model.indexOf(card.model.geoprivacy))
                    onActivated: dialog.uploader.setGeoprivacy(card.cardId, currentText)
                }
            }

            // A failed upload: loud, so it is not mistaken for a card that went up.
            Rectangle {
                Layout.fillWidth: true
                visible: card.model.uploadError !== ""
                implicitHeight: uploadErrorText.implicitHeight + 14
                radius: 5
                color: "#40ef5350"
                border.color: dialog.errColor
                border.width: 1
                Text {
                    id: uploadErrorText
                    anchors.fill: parent
                    anchors.margins: 7
                    text: "⚠ Not uploaded: " + card.model.uploadError
                    textFormat: Text.PlainText
                    color: "white"
                    font.pixelSize: 13
                    font.bold: true
                    wrapMode: Text.Wrap
                }
            }
            // Vision status
            Text {
                Layout.fillWidth: true
                text: card.model.statusText
                visible: text !== "" && text !== card.model.uploadError
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
                visible: card.model.choices.length > 0 && card.status !== "uploaded" && !card.attached
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
                    placeholderText: card.attached ? "The existing observation keeps its own name" : "Scientific name (or leave blank)"
                    enabled: card.status !== "uploading" && card.status !== "uploaded" && !card.attached
                    // Upright on purpose: italics are correct for a name but harder to read while typing.
                    onEditingFinished: {
                        suggestTimer.stop()
                        suggestPopup.close()
                        if (text !== card.model.chosenName) dialog.uploader.setChosenName(card.cardId, text, "")
                    }

                    // Autocomplete from iNaturalist. "p semilanc" offers Psilocybe semilanceata.
                    property var suggestions: []
                    property int highlighted: 0
                    function accept(row) {
                        suggestTimer.stop()
                        suggestPopup.close()
                        dialog.uploader.chooseSuggestion(card.cardId, row.taxonId)
                        // Typing replaced the bound text; show the chosen name again.
                        nameField.text = Qt.binding(function() { return card.model.chosenName })
                    }
                    onTextEdited: {
                        if (text.trim().length < 2) { suggestTimer.stop(); suggestPopup.close() }
                        else suggestTimer.restart()
                    }
                    onActiveFocusChanged: if (!activeFocus) { suggestTimer.stop(); suggestPopup.close() }
                    Timer {
                        id: suggestTimer
                        interval: 250
                        onTriggered: dialog.uploader.suggestTaxa(card.cardId, nameField.text)
                    }
                    Connections {
                        target: dialog.uploader
                        function onTaxonSuggestions(cardId, query, rows) {
                            if (cardId !== card.cardId || query !== nameField.text || !nameField.activeFocus) return
                            nameField.suggestions = rows
                            nameField.highlighted = 0
                            if (rows.length > 0) suggestPopup.open()
                            else suggestPopup.close()
                        }
                    }
                    Keys.priority: Keys.BeforeItem
                    Keys.onPressed: function(event) {
                        if (!suggestPopup.visible) return
                        var n = nameField.suggestions.length
                        if (event.key === Qt.Key_Down) {
                            nameField.highlighted = (nameField.highlighted + 1) % n
                            event.accepted = true
                        } else if (event.key === Qt.Key_Up) {
                            nameField.highlighted = (nameField.highlighted + n - 1) % n
                            event.accepted = true
                        } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Tab) {
                            nameField.accept(nameField.suggestions[nameField.highlighted])
                            event.accepted = true
                        } else if (event.key === Qt.Key_Escape) {
                            suggestPopup.close()
                            event.accepted = true
                        }
                    }
                    Popup {
                        id: suggestPopup
                        y: nameField.height + 2
                        width: Math.max(nameField.width, 440)
                        padding: 4
                        focus: false   // the field keeps the keyboard
                        closePolicy: Popup.CloseOnPressOutsideParent
                        background: Rectangle { color: "#2a2a2a"; radius: 6; border.color: "#4a4a4a" }
                        contentItem: Column {
                            Repeater {
                                model: nameField.suggestions
                                delegate: Rectangle {
                                    id: suggestRow
                                    required property var modelData
                                    required property int index
                                    width: suggestPopup.availableWidth
                                    height: 28
                                    radius: 4
                                    color: suggestRow.index === nameField.highlighted ? dialog.accentColor : "transparent"
                                    Text {
                                        id: suggestName
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.left: parent.left; anchors.leftMargin: 8
                                        text: suggestRow.modelData.name
                                        color: "white"; font.pixelSize: 13
                                    }
                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.left: suggestName.right; anchors.leftMargin: 10
                                        anchors.right: parent.right; anchors.rightMargin: 8
                                        text: suggestRow.modelData.rank
                                              + (suggestRow.modelData.commonName ? " · " + suggestRow.modelData.commonName : "")
                                        color: suggestRow.index === nameField.highlighted ? "#e0e0ff" : dialog.mutedText
                                        font.pixelSize: 11
                                        elide: Text.ElideRight
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        onEntered: nameField.highlighted = suggestRow.index
                                        onClicked: nameField.accept(suggestRow.modelData)
                                    }
                                }
                            }
                        }
                    }
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
                    enabled: card.status !== "uploading" && card.status !== "uploaded" && !card.attached
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
