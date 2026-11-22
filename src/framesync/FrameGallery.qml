import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ColumnLayout {
    id: page
    required property var backend
    readonly property var gallery: backend ? backend.frameGallery : null
    readonly property var session: backend ? backend.state : ({})
    readonly property var info: gallery ? gallery.state : ({})
    readonly property bool connected: !!session.paired
    readonly property bool active: connected && !session.busy && !session.galleryLoading
    spacing: 12

    function loadIfNeeded() {
        if (visible && active && gallery)
            gallery.openPage()
    }
    onVisibleChanged: Qt.callLater(loadIfNeeded)
    onConnectedChanged: Qt.callLater(loadIfNeeded)
    Connections {
        target: page.backend ? page.backend.frameSettings : null
        function onArtworkChanged(cid) {
            Qt.callLater(page.loadIfNeeded)
        }
    }
    FrameSettingsDialog { id: frameOptions; backend: page.backend }
    ArtworkSettingsDialog { id: artOptions; backend: page.backend }
    FrameDialog {
        id: confirm
        objectName: "frameRemoveConfirmation"
        property var artwork: ({})
        modal: true
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(410, parent.width - 32)
        title: "Remove from Frame?"
        contentItem: ColumnLayout {
            Image {
                Layout.fillWidth: true
                Layout.preferredHeight: 115
                visible: !!confirm.artwork.thumb
                source: confirm.artwork.thumb || ""
                fillMode: Image.PreserveAspectFit
            }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                text: "Remove “" + (confirm.artwork.name || "") + "” from " + (page.session.host || "") + "?"
            }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                visible: !!confirm.artwork.digest
                text: "Your Mac keeps its copy. Use Send selected to put this version back on this Frame."
            }
        }
        footer: DialogButtonBox {
            FrameButton { text: "Cancel"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            FrameButton { text: "Remove from Frame"; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole }
        }
        onAccepted: page.gallery.remove(artwork.cid, artwork.identity)
    }

    Label { text: "On the Frame"; font.family: FrameTheme.headingFont; font.pixelSize: 36 }
    RowLayout {
        Layout.fillWidth: true
        Label {
            Layout.fillWidth: true
            text: page.connected ? page.session.host : "Connect your TV to see its artwork."
            color: FrameTheme.muted
            elide: Text.ElideRight
        }
        FrameButton { text: "Cancel"; visible: !!page.session.galleryLoading; onClicked: page.backend.cancelGallery() }
        FrameButton { text: "Refresh"; enabled: page.active; onClicked: page.gallery.refresh() }
        FrameButton { text: "Frame settings…"; enabled: page.active; onClicked: frameOptions.showSettings() }
    }
    ProgressBar {
        objectName: "galleryLoadProgress"
        Layout.fillWidth: true
        visible: !!page.session.galleryLoading
        indeterminate: true
        Accessible.name: "Loading gallery page"
    }
    GridView {
        id: grid
        Layout.fillWidth: true
        Layout.fillHeight: true
        clip: true
        model: page.gallery ? page.gallery.rows : []
        cellWidth: width / Math.max(1, Math.floor(width / 215))
        cellHeight: cellWidth * 9 / 16 + 99
        ScrollBar.vertical: ScrollBar {}
        delegate: Item {
            id: card
            required property var modelData
            width: grid.cellWidth
            height: grid.cellHeight
            Rectangle {
                anchors.fill: parent
                anchors.margins: 4
                radius: 10
                color: FrameTheme.panel
                border.color: card.modelData.current ? FrameTheme.violet : FrameTheme.line
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 7
                    spacing: 4
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        color: FrameTheme.line
                        Image { anchors.fill: parent; source: card.modelData.thumb; fillMode: Image.PreserveAspectFit }
                        Label {
                            anchors.centerIn: parent
                            visible: !card.modelData.thumb
                            text: "Preview unavailable"
                            color: FrameTheme.muted
                        }
                        Label {
                            anchors.left: parent.left
                            anchors.bottom: parent.bottom
                            anchors.margins: 5
                            visible: card.modelData.current
                            text: "Now showing"
                            padding: 4
                            background: Rectangle { radius: 4; color: FrameTheme.violetBg }
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: card.modelData.name
                        textFormat: Text.PlainText
                        elide: Text.ElideMiddle
                        font.bold: true
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FrameButton {
                            text: "Show on Frame"
                            enabled: page.active && !card.modelData.current
                            onClicked: page.gallery.show(card.modelData.cid)
                        }
                        Item { Layout.fillWidth: true }
                        FrameButton {
                            quiet: true
                            id: more
                            text: "•••"
                            Accessible.name: "Options for " + card.modelData.name
                            enabled: page.active
                            onClicked: menu.popup()
                            Menu {
                                palette: FramePalette {}
                                id: menu
                                MenuItem {
                                    text: "Artwork settings…"
                                    onTriggered: artOptions.showArtwork(card.modelData.cid, card.modelData.name, card.modelData.metadata)
                                }
                                MenuItem {
                                    text: "Remove from Frame…"
                                    enabled: !!card.modelData.identity
                                    onTriggered: {
                                        confirm.artwork = card.modelData
                                        confirm.open()
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        Label {
            anchors.centerIn: parent
            visible: page.gallery && page.gallery.loaded && !page.info.count
            text: "No artwork on this Frame."
            color: FrameTheme.muted
        }
    }
    RowLayout {
        visible: page.info.pages > 1
        FrameButton { text: "Previous"; enabled: page.active && page.info.page > 0; onClicked: page.gallery.goToPage(page.info.page - 1) }
        Label { text: (page.info.page + 1) + " / " + page.info.pages }
        FrameButton { text: "Next"; enabled: page.active && page.info.page + 1 < page.info.pages; onClicked: page.gallery.goToPage(page.info.page + 1) }
    }
    Label {
        Layout.fillWidth: true
        visible: !!text
        text: page.session.galleryMessage || page.session.message || page.info.message || ""
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
    }
}
