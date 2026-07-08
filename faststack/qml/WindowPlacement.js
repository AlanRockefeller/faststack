.pragma library

// Shared window-geometry helpers used by Main.qml and CompactEditorWindow.qml.
// All functions are pure: they operate on the window/screen objects passed in
// so the same persistence policy lives in one place.

// Return the ScreenInfo from `screens` (Application.screens) whose geometry
// contains the given virtual-desktop point, or null if the point is off every
// screen (e.g. a monitor that was unplugged since the geometry was saved).
function screenContaining(screens, px, py) {
    if (!screens) return null
    for (var i = 0; i < screens.length; i++) {
        var s = screens[i]
        if (px >= s.virtualX && px < s.virtualX + s.width
                && py >= s.virtualY && py < s.virtualY + s.height) {
            return s
        }
    }
    return null
}

// Clamp `win`'s geometry so it fits within `screen`'s available area. `opts`
// may define minWidth, maxWidth, minHeight, maxHeight; any omitted bound is
// ignored. Clamping against an explicit screen (rather than win.screen) lets
// callers keep a window on the monitor its saved coordinates fall on, even
// before the window has been mapped.
function clampToScreen(win, screen, opts) {
    if (!screen) return

    var screenX = screen.virtualX
    var screenY = screen.virtualY
    var screenWidth = screen.desktopAvailableWidth
    var screenHeight = screen.desktopAvailableHeight
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
function centerOnScreen(win, screen) {
    if (!screen) return false
    var availW = screen.desktopAvailableWidth
    var availH = screen.desktopAvailableHeight
    win.x = screen.virtualX + Math.max(0, Math.round((availW - win.width) / 2))
    win.y = screen.virtualY + Math.max(0, Math.round((availH - win.height) / 2))
    return true
}
