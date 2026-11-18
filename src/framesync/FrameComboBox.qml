import QtQuick
import QtQuick.Controls.Basic

ComboBox {
    id: control
    padding: 8
    spacing: 8
    leftPadding: 11
    rightPadding: 32
    implicitHeight: Math.max(36, contentItem.implicitHeight + 10)
    palette: FramePalette {}
    delegate: ItemDelegate {
        id: option
        required property var model
        required property int index
        width: ListView.view.width
        text: model[control.textRole]
        highlighted: index === control.highlightedIndex
        font.weight: index === control.currentIndex ? Font.DemiBold : Font.Normal
        contentItem: Text {
            text: option.text
            font: option.font
            color: option.highlighted ? FrameTheme.violet : FrameTheme.ink
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            color: option.highlighted || option.hovered ? FrameTheme.violetBg : FrameTheme.panel
        }
    }
    contentItem: Text {
        text: control.displayText
        font: control.font
        color: control.enabled ? FrameTheme.ink : FrameTheme.muted
        textFormat: Text.PlainText
        elide: Text.ElideRight
        verticalAlignment: Text.AlignVCenter
    }
    background: Rectangle {
        implicitWidth: 115
        radius: 8
        color: FrameTheme.field
        border.width: control.visualFocus ? 2 : 1
        border.color: control.visualFocus || control.popup.visible ? FrameTheme.violet : FrameTheme.strongLine
    }
}
