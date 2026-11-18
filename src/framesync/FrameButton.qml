import QtQuick
import QtQuick.Controls.Basic

Button {
    id: button
    property bool primary: false
    property bool quiet: false
    property bool alignLeft: false
    implicitHeight: Math.max(36, contentItem.implicitHeight + 12)
    horizontalPadding: quiet ? 8 : 13
    hoverEnabled: true
    font.weight: primary || checked ? Font.DemiBold : Font.Medium
    contentItem: Text {
        text: button.text
        font: button.font
        color: !button.enabled ? FrameTheme.muted : button.primary ? FrameTheme.amberInk
               : button.checked || button.hovered || button.quiet ? FrameTheme.violet : FrameTheme.softInk
        horizontalAlignment: button.alignLeft ? Text.AlignLeft : Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        textFormat: Text.PlainText
    }
    background: Rectangle {
        radius: 9
        color: !button.enabled ? FrameTheme.softPanel : button.primary
               ? (button.hovered || button.down ? FrameTheme.amberHover : FrameTheme.amber)
               : button.checked || button.hovered || button.down ? FrameTheme.violetBg
               : button.quiet ? "transparent" : FrameTheme.softPanel
        border.width: button.visualFocus ? 2 : 1
        border.color: button.visualFocus ? FrameTheme.violet : button.checked || button.hovered
                      ? FrameTheme.violetLine : button.quiet || button.primary && button.enabled
                      ? "transparent" : FrameTheme.strongLine
    }
}
