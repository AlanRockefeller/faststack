.pragma library

// What mouse presses and arrow keys do to the editors' slider edits. Kept
// out of the QML so the tests can run it (see test_levels_slider_range.py).
//
// A slider's value property is its handle position (-100..+100, see
// SliderCurve.js); the edit itself can lie past the slider's end (an extreme
// auto levels result), with the handle pinned at the end.

// ---- Mouse (values are handle positions) ----------------------------------
//
// Qt moves the handle to the press point straight away, and emits moved on
// every press, even one that moved nothing. Taken as it comes, a click a few
// pixels off the handle's centre would change the edit, and a click on a
// pinned handle would cut an edit past the end back to the end. So:
//  - a press beside the handle (on the track) jumps there at once, as usual;
//  - a press on the handle only grabs it. Where Qt put the handle then is
//    the grab point, and the press becomes a drag once the pointer has moved
//    DRAG_START_PX from there. A click that never gets that far leaves the
//    edit alone.

var DRAG_START_PX = 2

// Handle-position units per pixel of pointer travel.
function unitsPerPixel(handleWidth, availableWidth, from, to) {
    return (to - from) / Math.max(1, availableWidth - handleWidth)
}

// Whether a press landed on the handle (or beside a handle pinned at the
// end, which Qt cannot move further): Qt's jump to the press point stayed
// within half the handle plus a pixel. pressValue is the handle position
// before the press, value where Qt put it.
function pressOnHandle(pressValue, value, handleWidth, unitsPerPx) {
    var px = Math.max(3, handleWidth / 2 + 1)
    return Math.abs(value - pressValue) <= px * unitsPerPx
}

// Whether a press on the handle has become a drag.
function dragStarted(grabValue, value, unitsPerPx) {
    return Math.abs(value - grabValue) > DRAG_START_PX * unitsPerPx
}

// What one moved signal during a press means, until the press is a drag:
// "grab" (it landed on the handle: value is the grab point), "drag" (send
// value from now on) or "" (not a drag yet). grabbed says whether a grab
// point was taken, grabValue what it is.
function pressMove(pressValue, grabbed, grabValue, value, handleWidth, availableWidth, from, to) {
    var unitsPerPx = unitsPerPixel(handleWidth, availableWidth, from, to)
    if (!grabbed)
        return pressOnHandle(pressValue, value, handleWidth, unitsPerPx) ? "grab" : "drag"
    return dragStarted(grabValue, value, unitsPerPx) ? "drag" : ""
}

// ---- Keyboard (values are display units, the numbers the readout shows) ---
//
// Arrow keys step the edit itself rather than the handle, so an edit past
// the slider's end steps back toward the range instead of jumping to the end.
// A held key steps on from the value it reached. It stops doing that when
// the edit is no longer what the hold left it at: something else (auto
// levels, another key, a drag) changed it meanwhile.

// Below one key step: rounding, never a real change.
var SAME_EDIT = 0.5

// Whether a key hold's stepped value is still the edit's own. sentValue is
// the edit the hold last left in place; backendValue the edit now.
function keyHoldCurrent(sentValue, backendValue) {
    return Math.abs(backendValue - sentValue) < SAME_EDIT
}

// One key step from base, kept in minVal..maxVal. A base already outside
// that range may only step back toward it.
function keyStep(base, step, minVal, maxVal) {
    var lo = Math.min(minVal, base)
    var hi = Math.max(maxVal, base)
    return Math.max(lo, Math.min(hi, base + step))
}
