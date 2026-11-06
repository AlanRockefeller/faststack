.pragma library

// Shared window-geometry helpers used by Main.qml and CompactEditorWindow.qml.
// All functions are pure: they operate on the window/screen objects passed in
// so the same persistence policy lives in one place.

// Clamp `win`'s geometry so it fits within `geometry`'s available area. `opts`
// may define minWidth, maxWidth, minHeight, maxHeight; any omitted bound is
// ignored. The geometry comes from QScreen.availableGeometry(), because QML's
// desktopAvailableWidth/Height describe the whole virtual desktop rather than
// the selected physical screen.
function clampToScreen(win, geometry, opts) {
    if (!geometry) return

    var screenX = geometry.x
    var screenY = geometry.y
    var screenWidth = geometry.width
    var screenHeight = geometry.height
    if (screenWidth <= 0 || screenHeight <= 0) return

    opts = opts || {}
    var minWidth = opts.minWidth > 0 ? opts.minWidth : 1
    var minHeight = opts.minHeight > 0 ? opts.minHeight : 1

    var newWidth = Math.max(minWidth, win.width)
    var newHeight = Math.max(minHeight, win.height)
    if (opts.maxWidth > 0) newWidth = Math.min(newWidth, opts.maxWidth)
    if (opts.maxHeight > 0) newHeight = Math.min(newHeight, opts.maxHeight)

    if (screenWidth >= minWidth) newWidth = Math.min(newWidth, screenWidth)
    if (screenHeight >= minHeight) newHeight = Math.min(newHeight, screenHeight)

    var maxX = Math.max(screenX, screenX + screenWidth - newWidth)
    var maxY = Math.max(screenY, screenY + screenHeight - newHeight)

    win.x = Math.max(screenX, Math.min(win.x, maxX))
    win.y = Math.max(screenY, Math.min(win.y, maxY))
    win.width = newWidth
    win.height = newHeight
}

// Center `win` on `screen`'s available area. Returns false if no screen is
// available yet (caller can retry after the window is shown).
function centerOnScreen(win, geometry) {
    if (!geometry) return false
    var availW = geometry.width
    var availH = geometry.height
    win.x = geometry.x + Math.max(0, Math.round((availW - win.width) / 2))
    win.y = geometry.y + Math.max(0, Math.round((availH - win.height) / 2))
    return true
}
