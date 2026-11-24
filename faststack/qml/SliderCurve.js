.pragma library

// Handle position <-> value for the editors' sliders.
//
// Every slider's handle runs over positions -100..+100 with 0 in the middle.
// A side whose value range is the usual 100 maps one to one. A side that
// reaches further (Whites up to +300, Blacks down to -300, so auto levels
// results fit) stays one to one out to position 50, so everyday edits feel
// exactly as before, then speeds up smoothly (matching slope at 50) to reach
// its full extent at the end:
//
//     value = pos + a * (pos - 50)^3   for pos > 50,  a = (extent - 100) / 50^3
//
// With extent 300, +100 sits at position 75.

var KNEE = 50

function _coef(extent) {
    return (extent - 100) / (KNEE * KNEE * KNEE)
}

// Value for a handle position on one side (pos >= 0, extent >= 100).
function _sideValue(pos, extent) {
    if (extent <= 100 || pos <= KNEE) return pos
    var x = pos - KNEE
    return pos + _coef(extent) * x * x * x
}

// Handle position for a value on one side (value >= 0). Solves the cubic
// a*x^3 + x - (value - KNEE) = 0 for x = pos - KNEE; it has one real root
// because the curve is strictly increasing. Values past the extent go past
// 100 here and are clamped by the slider.
function _sidePos(value, extent) {
    if (extent <= 100 || value <= KNEE) return value
    var a = _coef(extent)
    var p = 1 / a
    var q = -(value - KNEE) / a
    var d = Math.sqrt(q * q / 4 + p * p * p / 27)
    return KNEE + Math.cbrt(-q / 2 + d) + Math.cbrt(-q / 2 - d)
}

// minVal / maxVal are the slider's value range (e.g. -100, 300).
function valueForPosition(pos, minVal, maxVal) {
    return pos >= 0 ? _sideValue(pos, maxVal) : -_sideValue(-pos, -minVal)
}

function positionForValue(value, minVal, maxVal) {
    return value >= 0 ? _sidePos(value, maxVal) : -_sidePos(-value, -minVal)
}
