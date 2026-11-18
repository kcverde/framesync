import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

FrameDialog {
    id: dialog
    required property var backend
    readonly property var settings: backend ? backend.frameSettings : null
    readonly property bool busy: !backend || backend.state.busy
    readonly property var options: settings ? settings.state : ({})
    readonly property var mattes: options.mattes || ({ styles: [], colours: [] })
    property string cid: ""
    property string artworkTitle: ""
    property var metadata: ({})
    property string section: "favourite"

    function showArtwork(contentId, name, details) {
        cid = contentId
        artworkTitle = name
        metadata = details || {}
        section = "favourite"
        open()
    }
    function selectSection(name) {
        section = name
        if (name !== "favourite") settings.load(name)
    }

    parent: Overlay.overlay
    anchors.centerIn: parent
    modal: true
    width: Math.min(490, parent.width - 32)
    title: "Artwork settings"
    standardButtons: Dialog.Close
    contentItem: ColumnLayout {
        spacing: 12
        Label {
            Layout.fillWidth: true
            font.bold: true
            wrapMode: Text.Wrap
            text: dialog.artworkTitle
            textFormat: Text.PlainText
        }
        RowLayout {
            FrameButton { text: "Favourite"; checked: dialog.section === "favourite"; enabled: !dialog.busy; onClicked: dialog.selectSection("favourite") }
            FrameButton { text: "Matte"; checked: dialog.section === "mattes"; enabled: !dialog.busy; onClicked: dialog.selectSection("mattes") }
            FrameButton { text: "Photo filter"; checked: dialog.section === "filters"; enabled: !dialog.busy; onClicked: dialog.selectSection("filters") }
        }
        Label {
            readonly property string value: String(dialog.metadata.favorite ?? dialog.metadata.favourite ?? "").toLowerCase()
            visible: dialog.section === "favourite" && ["on", "off", "true", "false"].includes(value)
            text: ["on", "true"].includes(value) ? "In TV favourites" : "Outside TV favourites"
        }
        RowLayout {
            visible: dialog.section === "favourite"
            enabled: !dialog.busy
            FrameButton { text: "Add to favourites"; onClicked: dialog.settings.setFavourite(dialog.cid, true) }
            FrameButton { text: "Remove from favourites"; onClicked: dialog.settings.setFavourite(dialog.cid, false) }
        }
        ColumnLayout {
            Layout.fillWidth: true
            visible: dialog.section === "mattes" && dialog.options.mattes !== undefined
            enabled: !dialog.busy
            Label { visible: !!dialog.metadata.matte_id; text: "Current matte  ·  " + (dialog.metadata.matte_id || "") }
            Label { visible: !dialog.mattes.styles.length; text: "This Frame reported no matte styles." }
            RowLayout {
                visible: !!dialog.mattes.styles.length
                FrameComboBox {
                    id: style
                    model: dialog.mattes.styles
                    Accessible.name: "Matte style"
                    currentIndex: {
                        let current = dialog.metadata.matte_id || ""
                        let i = model.findIndex(s => current === s || current.startsWith(s + "_"))
                        return Math.max(0, i)
                    }
                }
                FrameComboBox {
                    id: colour
                    visible: !!dialog.mattes.colours.length && style.currentText !== "none"
                    model: dialog.mattes.colours
                    currentIndex: Math.max(0, model.findIndex(c => (dialog.metadata.matte_id || "").endsWith("_" + c)))
                    Accessible.name: "Matte colour"
                }
            }
            FrameButton {
                visible: !!dialog.mattes.styles.length
                text: "Apply matte"
                onClicked: dialog.settings.setMatte(dialog.cid, style.currentText, colour.currentText)
            }
        }
        RowLayout {
            visible: dialog.section === "filters" && dialog.options.filters !== undefined
            enabled: !dialog.busy
            Label { visible: !filterBox.count; text: "This Frame reported no photo filters." }
            FrameComboBox {
                id: filterBox
                visible: count > 0
                model: dialog.options.filters || []
                currentIndex: Math.max(0, model.indexOf(dialog.metadata.filter_id))
                Accessible.name: "Photo filter"
            }
            FrameButton {
                visible: filterBox.count > 0
                text: "Apply filter"
                onClicked: dialog.settings.setFilter(dialog.cid, filterBox.currentText)
            }
        }
        Label { Layout.fillWidth: true; textFormat: Text.PlainText; text: dialog.backend ? dialog.backend.state.message : ""; wrapMode: Text.WordWrap }
    }
    Connections {
        target: dialog.settings
        function onArtworkChanged(cid) {
            if (cid === dialog.cid) dialog.close()
        }
    }
}
