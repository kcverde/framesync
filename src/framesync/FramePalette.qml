import QtQuick

Palette {
    window: FrameTheme.panel
    windowText: FrameTheme.ink
    base: FrameTheme.field
    alternateBase: FrameTheme.softPanel
    text: FrameTheme.ink
    placeholderText: FrameTheme.muted
    button: FrameTheme.softPanel
    buttonText: FrameTheme.softInk
    brightText: FrameTheme.ink
    highlight: FrameTheme.violet
    highlightedText: FrameTheme.dark ? FrameTheme.canvas : "#ffffff"
    accent: FrameTheme.violet
    dark: FrameTheme.violet
    mid: FrameTheme.strongLine
    midlight: FrameTheme.line
    light: FrameTheme.violetBg
    link: FrameTheme.violet
    toolTipBase: FrameTheme.panel
    toolTipText: FrameTheme.ink
    shadow: FrameTheme.dark ? "#080714" : "#33263f"
    disabled.text: FrameTheme.muted
    disabled.windowText: FrameTheme.muted
    disabled.buttonText: FrameTheme.muted
}
