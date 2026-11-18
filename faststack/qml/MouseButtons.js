.pragma library

// Left/right mouse-button swap (Settings > General > Swap Mouse Buttons).
// The image view, the thumbnail grid and the map picker read the button a
// press means through these helpers instead of comparing mouse.button with
// Qt.LeftButton / Qt.RightButton directly. Standard widgets (buttons, menus,
// sliders) are left alone. `swap` is uiState.swapMouseButtons.

// The logical button for one physical button (mouse.button).
function button(physical, swap) {
    if (!swap) return physical
    if (physical === Qt.LeftButton) return Qt.RightButton
    if (physical === Qt.RightButton) return Qt.LeftButton
    return physical
}

// The logical button mask for a physical mask (mouse.buttons, pressedButtons).
function buttons(physical, swap) {
    if (!swap) return physical
    var out = physical & ~(Qt.LeftButton | Qt.RightButton)
    if (physical & Qt.LeftButton) out |= Qt.RightButton
    if (physical & Qt.RightButton) out |= Qt.LeftButton
    return out
}
