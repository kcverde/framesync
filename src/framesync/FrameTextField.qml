import QtQuick
import QtQuick.Controls.Basic

TextField {
    id: field
    leftPadding: 11
    rightPadding: 11
    implicitHeight: Math.max(36, contentHeight + 12)
    background: Rectangle {
        implicitWidth: 140
        radius: 8
        color: FrameTheme.field
        border.width: field.activeFocus ? 2 : 1
        border.color: field.activeFocus ? FrameTheme.violet : FrameTheme.strongLine
    }
}
