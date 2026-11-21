.pragma library

// How far a two-finger trackpad scroll moves a list, as a multiple of the
// pixel delta Qt reports. On Wayland that delta is the raw finger travel,
// which crawls through a tall list. macOS already speeds its deltas up and
// adds momentum, so it keeps them as they are.
var factor = Qt.platform.os === "osx" ? 1 : 5

// The distance to scroll for one trackpad event's pixelDelta.
function pixels(pixelDelta) {
    return pixelDelta * factor
}
