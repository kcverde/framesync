import QtQuick
import QtQuick.Controls.Basic

Dialog {
    id: dialog
    padding: 18
    palette: FramePalette {}
    header: Label {
        visible: !!dialog.title
        leftPadding: dialog.padding
        rightPadding: dialog.padding
        topPadding: dialog.padding
        text: dialog.title
        font.family: "Asul"
        font.pixelSize: 24
        wrapMode: Text.WordWrap
    }
    background: Rectangle {
        color: FrameTheme.panel
        border.color: FrameTheme.strongLine
        radius: 14
    }
    footer: DialogButtonBox {
        visible: count > 0
        padding: dialog.padding
        topPadding: 0
        spacing: 8
        delegate: FrameButton {}
        background: Item {}
    }
}
